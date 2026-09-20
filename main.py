"""
Точка входа. Бот теперь работает как ПОСТОЯННО запущенный процесс (не как
разовый скрипт по расписанию через GitHub Actions) — это нужно для
мгновенной реакции на кнопки и команды. Хостится на сервисе с постоянно
работающими процессами (например Render, см. README), а не на GitHub
Actions cron.

Внутри процесса параллельно работают:
1. Long polling Telegram в основном потоке — реагирует на кнопки/сообщения
   сразу, как только они приходят (без ожидания расписания).
2. Фоновый поток, который каждые SCAN_INTERVAL_SECONDS сканирует категории
   часов на kufar.by и присылает новые находки.
3. Фоновый поток с крошечным HTTP-сервером — хостинг видит процесс как
   "живой" веб-сервис, а внешний пинг-сервис (см. README) не даёт ему
   "заснуть" от бездействия.
4. Фоновый поток, который периодически сохраняет состояние (кто одобрен,
   статусы, цены) в GitHub (если настроено — см. remote_state.py), чтобы
   прогресс не терялся при перезапуске процесса хостингом.
"""

from __future__ import annotations

import copy
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai
import formatting
import remote_state
import scoring
from config import (
    BOT_TOKEN,
    MAX_NEW_ITEMS_PER_RUN,
    MAX_RISK_TO_SUGGEST,
    OWNER_CHAT_ID,
    PORT,
    SCAN_INTERVAL_SECONDS,
    STATE_PUSH_INTERVAL_SECONDS,
)
from state import load_state, remember_price, remember_seen, save_state, with_defaults
from telegram_api import TelegramClient, TelegramError

import scraper

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/queue — показать текущие предложения на одобрение заново\n"
    "/approved — список одобренных лотов в работе (по ценовым категориям)\n"
    "/stats — статистика (в обороте, прибыль, продажи)\n"
    "/models — список известных моделей с ориентиром цены\n"
    "/addmodel ключ цена [high|medium|low] — задать ориентир цены продажи "
    "для модели, например: /addmodel ga-2100 250 high\n"
    "/delmodel ключ — удалить модель\n"
    "/done — закончить присылать фото для объявления и получить готовый текст\n\n"
    "Бот сам проверяет категории наручных часов на Kufar (все бренды) на "
    "новые объявления каждые "
    f"{max(1, SCAN_INTERVAL_SECONDS // 60)} мин. и присылает их сюда на "
    "одобрение, отсеивая явный мусор (ремешки, запчасти) и лоты с расчётным "
    f"риском выше {MAX_RISK_TO_SUGGEST}/100. Кнопками под карточкой лота "
    "можно одобрить/отклонить, указать цену продажи и двигать статус "
    "(торгуюсь → куплено → выставлено → продано) — реакция на кнопки и "
    "команды мгновенная, бот работает постоянно. После одобрения кнопка "
    "«📸 Собрать фото для объявления» переводит в режим приёма фото — "
    "присылайте фото уже купленной вещи по одному, затем /done — бот "
    "пришлёт готовый текст объявления для Kufar (скопировать и вставить "
    "самостоятельно)."
    + ("" if ai.available() else "\n\nПодсказка: подлинность по фото и более "
       "живой текст объявления заработают, если добавить секрет "
       "ANTHROPIC_API_KEY — сейчас используются только эвристики и шаблоны.")
)

# Один лок на всё состояние — бот однопользовательский и низконагруженный,
# поэтому простой RLock проще и надёжнее, чем гранулярная блокировка по
# отдельным ключам. Держим его коротко: сетевые вызовы (Telegram, Kufar, ИИ)
# стараемся делать ДО или ПОСЛЕ захвата лока, а не во время.
STATE_LOCK = threading.RLock()

_shutdown = threading.Event()
_dirty = threading.Event()


def _mark_dirty() -> None:
    _dirty.set()


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    print(f"[info] получен сигнал {signum}, останавливаюсь...", file=sys.stderr)
    _shutdown.set()


class _HealthHandler(BaseHTTPRequestHandler):
    """Крошечный HTTP-эндпоинт: хостингу нужен слушающий порт, а внешнему
    пинг-сервису — что-то, что можно дёргать каждые несколько минут, чтобы
    бесплатный инстанс не "засыпал" от бездействия."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("kufar-bot is running\n".encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # не засоряем логи хостинга служебными пинг-запросами


def _start_health_server(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()


def main() -> None:
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        print("Не заданы переменные окружения BOT_TOKEN / OWNER_CHAT_ID — выхожу.", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = None
    if remote_state.available():
        state = remote_state.fetch()
        if state is not None:
            print("[info] состояние загружено из GitHub.", file=sys.stderr)
            state = with_defaults(state)
    if state is None:
        state = load_state()

    tg = TelegramClient(BOT_TOKEN)

    _start_health_server(PORT)

    scan_thread = threading.Thread(target=_scan_loop, args=(state, tg), name="scan", daemon=True)
    scan_thread.start()

    persistence_thread = threading.Thread(
        target=_persistence_loop, args=(state,), name="persistence", daemon=True
    )
    persistence_thread.start()

    print("[info] Kufar bot запущен, жду обновления Telegram...", file=sys.stderr)
    _poll_loop(state, tg)

    scan_thread.join(timeout=5)
    persistence_thread.join(timeout=15)
    print("[info] остановлен.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Постоянный long polling Telegram (мгновенная реакция)
# ---------------------------------------------------------------------------

def _poll_loop(state: dict, tg: TelegramClient) -> None:
    while not _shutdown.is_set():
        try:
            with STATE_LOCK:
                offset = (state["last_update_id"] + 1) if state["last_update_id"] else None
            updates = tg.get_updates(offset=offset, timeout=25)
        except TelegramError as exc:
            print(f"[warn] getUpdates: {exc}", file=sys.stderr)
            time.sleep(5)
            continue
        except Exception as exc:  # сеть не должна ронять процесс
            print(f"[warn] getUpdates неожиданная ошибка: {exc}", file=sys.stderr)
            time.sleep(5)
            continue

        for upd in updates:
            with STATE_LOCK:
                state["last_update_id"] = upd["update_id"]
                try:
                    if "callback_query" in upd:
                        _handle_callback(state, tg, upd["callback_query"])
                    elif "message" in upd:
                        _handle_message(state, tg, upd["message"])
                except Exception as exc:
                    print(f"[warn] обработка апдейта: {exc}", file=sys.stderr)
            _mark_dirty()


# ---------------------------------------------------------------------------
# Периодическое сохранение состояния (локально + опционально в GitHub)
# ---------------------------------------------------------------------------

def _persistence_loop(state: dict) -> None:
    while not _shutdown.wait(STATE_PUSH_INTERVAL_SECONDS):
        if _dirty.is_set():
            _dirty.clear()
            _flush(state)
    # Финальное сохранение при остановке, даже если ничего не "грязное" —
    # дешевле лишний раз сохранить, чем потерять последние изменения.
    _flush(state)


def _flush(state: dict) -> None:
    with STATE_LOCK:
        snapshot = copy.deepcopy(state)
    try:
        save_state(snapshot)
    except Exception as exc:
        print(f"[warn] не удалось сохранить состояние локально: {exc}", file=sys.stderr)
    if remote_state.available():
        remote_state.push(snapshot)


def _is_owner(chat_id) -> bool:
    return str(chat_id) == str(OWNER_CHAT_ID)


def _handle_callback(state: dict, tg: TelegramClient, cq: dict) -> None:
    chat_id = cq["message"]["chat"]["id"]
    if not _is_owner(chat_id):
        tg.answer_callback_query(cq["id"], "Это чужой бот.")
        return

    data = cq.get("data", "")
    parts = data.split(":")
    action = parts[0] if parts else ""
    ad_id = parts[1] if len(parts) > 1 else None
    lot = state["lots"].get(ad_id) if ad_id else None

    if lot is None:
        tg.answer_callback_query(cq["id"], "Лот не найден (возможно, уже обработан).")
        return

    lot["chat_id"] = chat_id
    lot["message_id"] = cq["message"]["message_id"]

    if action == "appr":
        lot["status"] = "watching"
        _refresh_lot_message(state, tg, ad_id)
        tg.answer_callback_query(cq["id"], "Одобрено ✅")
    elif action == "rej":
        lot["status"] = "rejected"
        _refresh_lot_message(state, tg, ad_id)
        tg.answer_callback_query(cq["id"], "Отклонено")
    elif action == "askprice":
        state.setdefault("awaiting", {})[str(chat_id)] = {"ad_id": ad_id, "field": "resale"}
        tg.answer_callback_query(
            cq["id"], "Пришлите ожидаемую цену продажи числом (в BYN) следующим сообщением.", show_alert=True
        )
    elif action == "sold":
        state.setdefault("awaiting", {})[str(chat_id)] = {"ad_id": ad_id, "field": "actual_sale"}
        tg.answer_callback_query(cq["id"], "Пришлите фактическую цену продажи числом.", show_alert=True)
    elif action == "adv":
        new_status = parts[2] if len(parts) > 2 else "watching"
        lot["status"] = new_status
        _refresh_lot_message(state, tg, ad_id)
        tg.answer_callback_query(cq["id"], "Статус обновлён")
    elif action == "del":
        state["lots"].pop(ad_id, None)
        try:
            tg.edit_message_reply_markup(chat_id, cq["message"]["message_id"], None)
        except TelegramError:
            pass
        tg.answer_callback_query(cq["id"], "Удалено")
    elif action == "listing":
        lot.setdefault("listing_photo_file_ids", [])
        state.setdefault("awaiting", {})[str(chat_id)] = {"ad_id": ad_id, "field": "listing_photos"}
        tg.answer_callback_query(
            cq["id"],
            "Присылайте фото уже купленной вещи по одному (можно несколько), "
            "затем напишите /done — пришлю готовый текст объявления.",
            show_alert=True,
        )
    else:
        tg.answer_callback_query(cq["id"])


def _handle_message(state: dict, tg: TelegramClient, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if not _is_owner(chat_id):
        return

    awaiting = state.setdefault("awaiting", {})
    ctx = awaiting.get(str(chat_id))

    # Фото в режиме сбора для объявления — обрабатываем до текстовой ветки,
    # у фото-сообщений обычно нет текста (разве что подпись).
    if msg.get("photo") and ctx and ctx.get("field") == "listing_photos":
        ad_id = ctx["ad_id"]
        lot = state["lots"].get(ad_id)
        if not lot:
            del awaiting[str(chat_id)]
            return
        largest = msg["photo"][-1]  # Telegram присылает варианты по возрастанию размера
        lot.setdefault("listing_photo_file_ids", []).append(largest["file_id"])
        count = len(lot["listing_photo_file_ids"])
        tg.send_message(chat_id, f"📸 Фото добавлено ({count}). Присылайте ещё или напишите /done, когда готово.")
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        _handle_command(state, tg, chat_id, text)
        return

    if not ctx:
        return  # обычное сообщение без контекста ожидания — просто игнорируем

    value = _parse_number(text)
    if value is None:
        tg.send_message(chat_id, "Не понял число. Пришлите, например: 250 или 250.50")
        return

    ad_id = ctx["ad_id"]
    field = ctx["field"]
    lot = state["lots"].get(ad_id)
    if not lot:
        del awaiting[str(chat_id)]
        return

    if field == "resale":
        lot["resale"] = value
        reference = scoring.robust_reference_price(state["recent_prices"])
        lot["resalevsmarket"] = scoring.resale_vs_reference(value, reference)
    elif field == "actual_sale":
        lot["actual_sale"] = value
        lot["status"] = "sold"

    del awaiting[str(chat_id)]
    _refresh_lot_message(state, tg, ad_id)
    tg.send_message(chat_id, "Готово, обновил карточку выше.")


def _handle_command(state: dict, tg: TelegramClient, chat_id, text: str) -> None:
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        tg.send_message(chat_id, HELP_TEXT)

    elif cmd == "/stats":
        tg.send_message(chat_id, _build_stats_text(state))

    elif cmd == "/queue":
        pending = sorted(
            (l for l in state["lots"].values() if l["status"] == "suggested"),
            key=lambda l: l.get("price") or 0,
        )
        if not pending:
            tg.send_message(chat_id, "Пока нет новых предложений на рассмотрение.")
        for lot in pending:
            _send_suggestion(state, tg, lot)

    elif cmd == "/approved":
        active = sorted(
            (l for l in state["lots"].values() if l["status"] not in ("suggested", "rejected", "sold")),
            key=lambda l: l.get("price") or 0,
        )
        if not active:
            tg.send_message(chat_id, "Одобренных лотов в работе пока нет.")
        for lot in active:
            sc = _score_lot(lot)
            tg.send_message(
                chat_id,
                formatting.approved_text(lot, sc),
                reply_markup=formatting.approved_keyboard(lot["ad_id"], lot["status"]),
            )

    elif cmd == "/models":
        models = state["known_models"]
        if not models:
            tg.send_message(chat_id, "Пока нет ни одной сохранённой модели. Пример: /addmodel ga-2100 250 high")
        else:
            lines = ["<b>Известные модели (ориентир цены продажи):</b>"]
            for kw, info in models.items():
                lines.append(f"• {kw}: {info['resale_price']} р., ликвидность {info['liquidity']}")
            tg.send_message(chat_id, "\n".join(lines))

    elif cmd == "/addmodel":
        if len(parts) < 3:
            tg.send_message(
                chat_id,
                "Формат: /addmodel ключевое_слово цена_продажи [high|medium|low]\n"
                "Например: /addmodel ga-2100 250 high",
            )
            return
        keyword = parts[1].lower()
        price = _parse_number(parts[2])
        liquidity = parts[3].lower() if len(parts) > 3 else "medium"
        if price is None or liquidity not in ("high", "medium", "low"):
            tg.send_message(chat_id, "Не понял цену или ликвидность (high/medium/low).")
            return
        state["known_models"][keyword] = {"resale_price": price, "liquidity": liquidity}
        tg.send_message(chat_id, f"Сохранил: «{keyword}» → {price} р., ликвидность {liquidity}")

    elif cmd == "/delmodel":
        if len(parts) < 2:
            tg.send_message(chat_id, "Формат: /delmodel ключевое_слово")
            return
        keyword = parts[1].lower()
        if state["known_models"].pop(keyword, None) is not None:
            tg.send_message(chat_id, f"Удалил модель «{keyword}».")
        else:
            tg.send_message(chat_id, f"Модели «{keyword}» не было в списке.")

    elif cmd == "/done":
        awaiting = state.setdefault("awaiting", {})
        ctx = awaiting.get(str(chat_id))
        if not ctx or ctx.get("field") != "listing_photos":
            tg.send_message(
                chat_id,
                "Сейчас не жду фото для объявления — сначала нажмите «📸 Собрать "
                "фото для объявления» под нужным одобренным лотом.",
            )
            return
        ad_id = ctx["ad_id"]
        del awaiting[str(chat_id)]
        _finish_listing(state, tg, chat_id, ad_id)

    else:
        tg.send_message(chat_id, "Не знаю такую команду. /help — список команд.")


# ---------------------------------------------------------------------------
# Сканирование Kufar (фоновый поток, раз в SCAN_INTERVAL_SECONDS)
# ---------------------------------------------------------------------------

def _scan_loop(state: dict, tg: TelegramClient) -> None:
    while not _shutdown.is_set():
        try:
            _run_scan(state, tg)
        except Exception as exc:  # сеть/парсинг не должны ронять весь процесс
            print(f"[warn] сканирование Kufar: {exc}", file=sys.stderr)
        if _shutdown.wait(SCAN_INTERVAL_SECONDS):
            break


def _run_scan(state: dict, tg: TelegramClient) -> None:
    session = scraper._session()
    ids = scraper.discover_item_ids(session)
    with STATE_LOCK:
        seen = set(state["seen_ad_ids"])
    new_ids = [i for i in ids if i not in seen][:MAX_NEW_ITEMS_PER_RUN]

    for ad_id in new_ids:
        details = scraper.fetch_item_details(session, ad_id)
        with STATE_LOCK:
            remember_seen(state, ad_id)

        if not details.raw_ok:
            continue

        if not scraper.passes_filters(details.title, details.description):
            continue

        brand = scraper.extract_brand(details.title)
        if details.price:
            with STATE_LOCK:
                remember_price(state, details.price, brand)

        with STATE_LOCK:
            lot = _build_lot(state, details)

        # Автопроверка подлинности по фото — только если задан ANTHROPIC_API_KEY,
        # иначе originality остаётся "unverified" (см. ai.py).
        if ai.available() and lot.get("image_url"):
            result = ai.check_authenticity(lot["image_url"], lot.get("title"))
            if result:
                lot["originality"], lot["originality_note"] = result

        sc = _score_lot(lot)
        if sc["risk"] > MAX_RISK_TO_SUGGEST:
            continue  # риск выше допустимого — не показываем и не храним

        with STATE_LOCK:
            state["lots"][str(ad_id)] = lot
        _send_suggestion(state, tg, lot)
        _mark_dirty()


def _match_known_model(known_models: dict, title: str) -> dict | None:
    t = (title or "").lower()
    for kw, info in known_models.items():
        if kw in t:
            return info
    return None


MIN_BRAND_SAMPLES = 3


def _resolve_reference(state: dict, title: str | None) -> tuple[float | None, str]:
    """Приоритет ориентира цены: заданная вами модель (/addmodel) > медиана
    цен по такому же "бренду", извлечённому из названия (если накопилось
    достаточно образцов) > медиана вообще по всем часам (очень грубо) >
    ничего. Возвращает (цена_или_None, источник)."""
    known = _match_known_model(state["known_models"], title or "")
    if known:
        return known["resale_price"], "known"

    brand = scraper.extract_brand(title)
    if brand:
        bucket = state.get("recent_prices_by_brand", {}).get(brand, [])
        if len(bucket) >= MIN_BRAND_SAMPLES:
            ref = scoring.robust_reference_price(bucket)
            if ref:
                return ref, f"brand:{brand}"

    global_ref = scoring.robust_reference_price(state["recent_prices"])
    if global_ref:
        return global_ref, "global"

    return None, "none"


def _build_lot(state: dict, details) -> dict:
    known = _match_known_model(state["known_models"], details.title or "")
    reference, source = _resolve_reference(state, details.title)

    # Раньше ожидаемая цена продажи (resale) заполнялась ТОЛЬКО для моделей,
    # заданных вручную через /addmodel — для всего остального маржа не
    # показывалась вообще ("не задана"), и по карточке нельзя было на глаз
    # понять, выгодное это предложение или нет. Теперь, если точной модели
    # нет, используется тот же грубый автоматический ориентир (медиана по
    # бренду или по всем часам), что и раньше показывался только в сноске —
    # так маржа/выгода видна сразу почти по любому лоту, просто с пометкой,
    # что это оценка, а не точная цифра (см. _send_suggestion).
    resale = reference if reference is not None else 0.0
    liquidity = known["liquidity"] if known else "medium"
    complete = scoring.guess_completeness(details.description or "")
    seller = scoring.guess_seller(details.seller_name or "", details.seller_listing_count, details.description or "")

    pricevsmarket = scoring.price_vs_reference(details.price or 0.0, reference)
    resalevsmarket = scoring.resale_vs_reference(resale, reference) if resale else "realistic"

    return {
        "ad_id": details.ad_id,
        "status": "suggested",
        "title": details.title,
        "link": details.link,
        "image_url": details.image_url,
        "condition": details.condition,
        "price": details.price or 0.0,
        "shipping": 0.0,
        "resale": resale,
        "resale_source": source,  # known | brand:<x> | global | none — для пометки в карточке
        "actual_sale": None,
        "liquidity": liquidity,
        "complete": complete,
        "seller": seller,
        "pricevsmarket": pricevsmarket,
        "resalevsmarket": resalevsmarket,
        "originality": "unverified",
        "originality_note": None,
        "listing_photo_file_ids": [],
        "chat_id": None,
        "message_id": None,
        "is_photo": False,
    }


def _score_lot(lot: dict) -> dict:
    inputs = scoring.LotInputs(
        price=lot.get("price") or 0.0,
        shipping=lot.get("shipping") or 0.0,
        resale=lot.get("resale") or 0.0,
        liquidity=lot.get("liquidity", "medium"),
        complete=lot.get("complete", "unverified"),
        seller=lot.get("seller", "new"),
        pricevsmarket=lot.get("pricevsmarket", "normal"),
        resalevsmarket=lot.get("resalevsmarket", "realistic"),
        originality=lot.get("originality", "unverified"),
    )
    return scoring.score(inputs)


def _send_suggestion(state: dict, tg: TelegramClient, lot: dict) -> None:
    sc = _score_lot(lot)
    reference, source = _resolve_reference(state, lot.get("title"))

    # Маржа в карточке теперь показывается почти всегда (см. _build_lot) —
    # эта сноска поясняет, откуда взялась ожидаемая цена продажи, чтобы не
    # выдавать грубую автооценку за точный расчёт.
    note = None
    if source == "known":
        pass  # заданная вами модель (/addmodel) — точная цифра, пояснять нечего
    elif source.startswith("brand:"):
        brand = source.split(":", 1)[1]
        note = (
            f"Ожидаемая цена продажи — грубая автооценка (медиана недавних цен "
            f"на «{brand}» на Kufar, ≈{reference:.0f} р.), не точная модель."
        )
    elif source == "global":
        note = (
            f"Ожидаемая цена продажи — очень грубая автооценка (медиана по всем "
            f"недавно виденным часам, ≈{reference:.0f} р.)."
        )
    else:
        note = "Данных для ориентира цены пока нет — ожидаемая продажа не задана, укажите сами кнопкой «✏️ Указать цену»."

    text = formatting.suggestion_text(lot, sc, note)
    keyboard = formatting.suggestion_keyboard(lot["ad_id"])

    if lot.get("image_url"):
        result = tg.send_photo(OWNER_CHAT_ID, lot["image_url"], text, reply_markup=keyboard)
        lot["is_photo"] = "photo" in result
    else:
        result = tg.send_message(OWNER_CHAT_ID, text, reply_markup=keyboard)
        lot["is_photo"] = False

    lot["chat_id"] = result["chat"]["id"]
    lot["message_id"] = result["message_id"]


def _refresh_lot_message(state: dict, tg: TelegramClient, ad_id: str) -> None:
    lot = state["lots"].get(ad_id)
    if not lot or not lot.get("chat_id") or not lot.get("message_id"):
        return

    sc = _score_lot(lot)
    if lot["status"] == "suggested":
        text = formatting.suggestion_text(lot, sc, None)
        keyboard = formatting.suggestion_keyboard(int(ad_id))
    elif lot["status"] == "rejected":
        text = "❌ Отклонено."
        keyboard = None
    else:
        text = formatting.approved_text(lot, sc)
        keyboard = formatting.approved_keyboard(int(ad_id), lot["status"])

    try:
        if lot.get("is_photo"):
            tg.edit_message_caption(lot["chat_id"], lot["message_id"], text, reply_markup=keyboard)
        else:
            tg.edit_message_text(lot["chat_id"], lot["message_id"], text, reply_markup=keyboard)
    except TelegramError:
        pass  # сообщение могли удалить вручную — не критично


def _finish_listing(state: dict, tg: TelegramClient, chat_id, ad_id: str) -> None:
    lot = state["lots"].get(ad_id)
    if not lot:
        tg.send_message(chat_id, "Лот не найден (возможно, был удалён).")
        return

    file_ids = lot.get("listing_photo_file_ids") or []
    text = None
    if file_ids and ai.available():
        photo_bytes = [b for b in (tg.download_file(fid) for fid in file_ids) if b]
        if photo_bytes:
            text = ai.generate_listing_text(lot, photo_bytes)

    if not text:
        text = formatting.listing_template(lot)
        if not ai.available():
            text += (
                "\n\n(Это шаблонный текст без ИИ — добавьте секрет ANTHROPIC_API_KEY, "
                "чтобы получать более живое описание по вашим фото.)"
            )
        elif not file_ids:
            text += "\n\n(Фото не присылали — использован шаблон по данным объявления.)"

    tg.send_message(chat_id, "<b>Готовый текст для Kufar</b> (скопируйте и вставьте сами):")
    tg.send_message(chat_id, text, parse_mode=None)
    lot["listing_photo_file_ids"] = []


def _build_stats_text(state: dict) -> str:
    lots = list(state["lots"].values())
    capital = sum(
        (l.get("price") or 0) + (l.get("shipping") or 0) for l in lots if l["status"] in ("bought", "listed")
    )
    sold = [l for l in lots if l["status"] == "sold"]
    realized = sum((l.get("actual_sale") or 0) - (l.get("price") or 0) - (l.get("shipping") or 0) for l in sold)
    wins = sum(
        1
        for l in sold
        if ((l.get("actual_sale") or 0) - (l.get("price") or 0) - (l.get("shipping") or 0)) > 0
    )
    win_rate = f"{round(wins / len(sold) * 100)}%" if sold else "—"
    return formatting.stats_text(capital, realized, len(sold), win_rate)


def _parse_number(text: str) -> float | None:
    cleaned = text.strip().replace(",", ".").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


if __name__ == "__main__":
    main()
