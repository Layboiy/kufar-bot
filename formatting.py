"""Сборка текстов сообщений и inline-клавиатур для Telegram."""

from __future__ import annotations

import json
from typing import Any

from config import ORIGINALITY_LABELS, PRICE_CATEGORIES, STATUS_LABELS
from scoring import price_category


def fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    v = round(v, 2)
    if v == int(v):
        v = int(v)
    return f"{v} р."


def kb(rows: list[list[tuple[str, str]]]) -> str:
    """rows: список строк кнопок, каждая кнопка — (текст, callback_data)."""
    return json.dumps(
        {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]},
        ensure_ascii=False,
    )


def suggestion_text(lot: dict[str, Any], sc: dict[str, Any], reference_note: str | None) -> str:
    title = lot.get("title") or f"Лот #{lot['ad_id']}"
    category = price_category(lot.get("price") or 0.0, PRICE_CATEGORIES)
    lines = [
        f"🕵️ Новая находка: <b>{_escape(title)}</b>",
        f"Категория: {category}",
        f"Цена: <b>{fmt_money(lot.get('price'))}</b>",
        f"Ссылка: {lot.get('link')}",
    ]
    if lot.get("condition"):
        lines.append(f"Состояние (по объявлению): {_escape(lot['condition'])}")
    if lot.get("resale"):
        amount = sc["margin_amount"]
        lines.append(
            f"Ожидаемая продажа: {fmt_money(lot.get('resale'))} "
            f"(маржа {fmt_money(amount)}, {sc['margin_pct']}%)"
        )
    else:
        lines.append("Ожидаемая цена продажи не задана — нажмите «✏️ Указать цену», чтобы уточнить.")
    lines.append(f"Риск: {sc['risk']}/100 · Шанс успеха: {sc['success']}%")
    lines.append(f"Ориентир срока продажи: {sc['days_min']}–{sc['days_max']} дн.")

    orig = lot.get("originality", "unverified")
    orig_line = f"Подлинность (по фото, ИИ-оценка): {ORIGINALITY_LABELS.get(orig, orig)}"
    if lot.get("originality_note"):
        orig_line += f"\n<i>{_escape(lot['originality_note'])}</i>"
    lines.append(orig_line)

    if reference_note:
        lines.append(f"<i>{_escape(reference_note)}</i>")
    lines.append(
        "\n<i>Это автоматическая эвристическая оценка (как в вашем веб-трекере лотов), "
        "не гарантия — финальное решение за вами.</i>"
    )
    return "\n".join(lines)


def suggestion_keyboard(ad_id: int) -> str:
    return kb(
        [
            [("✅ Одобрить", f"appr:{ad_id}"), ("❌ Отклонить", f"rej:{ad_id}")],
            [("✏️ Указать цену продажи", f"askprice:{ad_id}")],
        ]
    )


def approved_text(lot: dict[str, Any], sc: dict[str, Any]) -> str:
    title = lot.get("title") or f"Лот #{lot['ad_id']}"
    status_label = STATUS_LABELS.get(lot.get("status", "watching"), lot.get("status"))
    category = price_category(lot.get("price") or 0.0, PRICE_CATEGORIES)
    lines = [
        f"📌 <b>{_escape(title)}</b> — {status_label}",
        f"Категория: {category}",
        f"Покупка: {fmt_money(lot.get('price'))} · Ссылка: {lot.get('link')}",
    ]
    if lot.get("status") == "sold":
        actual = lot.get("actual_sale")
        real_margin = (actual or 0) - (lot.get("price") or 0) - (lot.get("shipping") or 0)
        lines.append(f"Продано за: {fmt_money(actual)} · Факт. прибыль: {fmt_money(real_margin)}")
    else:
        lines.append(
            f"Ожидаемая продажа: {fmt_money(lot.get('resale'))} "
            f"(маржа {fmt_money(sc['margin_amount'])}, {sc['margin_pct']}%)"
        )
        lines.append(f"Риск: {sc['risk']}/100 · Шанс успеха: {sc['success']}%")
    orig = lot.get("originality", "unverified")
    lines.append(f"Оригинальность: {ORIGINALITY_LABELS.get(orig, orig)}")
    return "\n".join(lines)


def approved_keyboard(ad_id: int, status: str) -> str:
    rows: list[list[tuple[str, str]]] = []
    if status != "negotiating":
        rows.append([("🔁 Торгуюсь", f"adv:{ad_id}:negotiating")])
    row2 = []
    if status not in ("bought", "listed", "sold"):
        row2.append(("💵 Куплено", f"adv:{ad_id}:bought"))
    if status not in ("listed", "sold"):
        row2.append(("📤 Выставлено", f"adv:{ad_id}:listed"))
    if row2:
        rows.append(row2)
    if status != "sold":
        rows.append([("💰 Продано", f"sold:{ad_id}")])
    rows.append([("📸 Собрать фото для объявления", f"listing:{ad_id}")])
    rows.append([("🗑 Удалить лот", f"del:{ad_id}")])
    return kb(rows)


_COMPLETE_PHRASES = {
    "full": "полный комплект (коробка, документы)",
    "partial": "без коробки, только сами часы",
    "unverified": "комплектность уточняется у покупателя",
}


def listing_template(lot: dict[str, Any]) -> str:
    """Запасной (бесплатный, без ИИ) вариант текста объявления — используется,
    когда не задан ANTHROPIC_API_KEY или ИИ не ответил. Не такой живой, как
    сгенерированный, но полностью рабочий текст для копирования на Kufar."""
    title = lot.get("title") or "Наручные часы"
    price = fmt_money(lot.get("resale") or lot.get("price"))
    condition = lot.get("condition") or "б/у, в рабочем состоянии"
    complete = _COMPLETE_PHRASES.get(lot.get("complete", "unverified"), "")
    lines = [
        f"{title}",
        "",
        f"Продаю часы {title}. Состояние: {condition}.",
    ]
    if complete:
        lines.append(f"Комплектность: {complete}.")
    lines.append(f"Цена: {price}.")
    lines.append("Пишите в сообщения — отвечу быстро, отправлю дополнительные фото по запросу.")
    return "\n".join(lines)


def stats_text(capital: float, realized: float, sold_count: int, win_rate: str) -> str:
    return (
        "<b>Статистика</b>\n"
        f"В обороте: {fmt_money(capital)}\n"
        f"Реализованная прибыль: {fmt_money(realized)}\n"
        f"Продано лотов: {sold_count}\n"
        f"Доля прибыльных сделок: {win_rate}"
    )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
