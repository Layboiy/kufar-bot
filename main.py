"""
Точка входа. Запускается GitHub Actions раз в N минут (см. .github/workflows/bot.yml).

За один запуск:
1. Забирает накопившиеся апдейты Telegram (нажатия кнопок, ответы, команды).
2. Сканирует категории часов на kufar.by, ищет новые объявления с Casio/G-Shock.
3. Присылает владельцу карточки с предложениями на одобрение.
4. Сохраняет состояние в data/state.json (коммитится обратно workflow-ом).
"""

from __future__ import annotations

import sys

import formatting
import scoring
from config import BOT_TOKEN, MAX_NEW_ITEMS_PER_RUN, OWNER_CHAT_ID
from state import load_state, remember_price, remember_seen, save_state
from telegram_api import TelegramClient, TelegramError

import scraper

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/queue — показать текущие предложения на одобрение заново\n"
    "/approved — список одобренных лотов в работе\n"
    "/stats — статистика (в обороте, прибыль, продажи)\n"
    "/models — список известных моделей с ориентиром цены\n"
    "/addmodel ключ цена [high|medium|low] — задать ориентир цены продажи "
    "для модели, например: /addmodel ga-2100 250 high\n"
    "/delmodel ключ — удалить модель\n\n"
    "Бот сам проверяет категории наручных часов на Kufar (все бренды) на "
    "новые объявления и присылает их сюда на одобрение, отсеивая только "
    "явный мусор вроде ремешков и запчастей. Кнопками под карточкой лота "
    "можно одобрить/отклонить, указать цену продажи и двигать статус "
    "(торгуюсь → куплено → выставлено → продано)."
)


def main() -> None:
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        print("Не заданы переменные окружения BOT_TOKEN / OWNER_CHAT_ID — выхожу.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    tg = TelegramClient(BOT_TOKEN)

    try:
        _process_updates(state, tg)
    except TelegramError as exc:
        print(f"[warn] обработка апдейтов: {exc}", file=sys.stderr)

    try:
        _run_scan(state, tg)
    except Exception as exc:  # сеть/парсинг не должны ронять весь запуск
        print(f"[warn] сканирование Kufar: {exc}", file=sys.stderr)

    save_state(state)


# ---------------------------------------------------------------------------
# Обработка входящих апдейтов
# ---------------------------------------------------------------------------

def _process_updates(state: dict, tg: TelegramClient) -> None:
    offset = (state["last_update_id"] + 1) if state["last_update_id"] else None
    updates = tg.get_updates(offset=offset)
    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        if "callback_query" in upd:
            _handle_callback(state, tg, upd["callback_query"])
        elif "message" in upd:
            _handle_message(state, tg, upd["message"])


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
    else:
        tg.answer_callback_query(cq["id"])


def _handle_message(state: dict, tg: TelegramClient, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if not _is_owner(chat_id):
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        _handle_command(state, tg, chat_id, text)
        return

    awaiting = state.setdefault("awaiting", {})
    ctx = awaiting.get(str(chat_id))
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
        pending = [l for l in state["lots"].values() if l["status"] == "suggested"]
        if not pending:
            tg.send_message(chat_id, "Пока нет новых предложений на рассмотрение.")
        for lot in pending:
            _send_suggestion(state, tg, lot)

    elif cmd == "/approved":
        active = [l for l in state["lots"].values() if l["status"] not in ("suggested", "rejected", "sold")]
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

    else:
        tg.send_message(chat_id, "Не знаю такую команду. /help — список команд.")


# ---------------------------------------------------------------------------
# Сканирование Kufar
# ---------------------------------------------------------------------------

def _run_scan(state: dict, tg: TelegramClient) -> None:
    session = scraper._session()
    ids = scraper.discover_item_ids(session)
    seen = set(state["seen_ad_ids"])
    new_ids = [i for i in ids if i not in seen][:MAX_NEW_ITEMS_PER_RUN]

    for ad_id in new_ids:
        details = scraper.fetch_item_details(session, ad_id)
        remember_seen(state, ad_id)

        if not details.raw_ok:
            continue

        if not scraper.passes_filters(details.title, details.description):
            continue

        brand = scraper.extract_brand(details.title)
        if details.price:
            remember_price(state, details.price, brand)

        lot = _build_lot(state, details)
        state["lots"][str(ad_id)] = lot
        _send_suggestion(state, tg, lot)


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
    reference, _source = _resolve_reference(state, details.title)

    resale = known["resale_price"] if known else 0.0
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
        "actual_sale": None,
        "liquidity": liquidity,
        "complete": complete,
        "seller": seller,
        "pricevsmarket": pricevsmarket,
        "resalevsmarket": resalevsmarket,
        "originality": "unverified",
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

    note = None
    if not lot.get("resale"):
        if source == "none":
            note = "Данных для ориентира цены пока нет — ожидаемая продажа не задана, укажите сами."
        else:
            note = "Эталонной цены для этой модели нет — ожидаемая продажа не задана."
    elif source.startswith("brand:"):
        brand = source.split(":", 1)[1]
        note = f"Ориентир — медиана недавних цен на «{brand}» на Kufar (≈{reference:.0f} р.), это грубая оценка."
    elif source == "global":
        note = f"Ориентир — медиана цен по всем недавно виденным часам (≈{reference:.0f} р.), очень грубая оценка."

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
