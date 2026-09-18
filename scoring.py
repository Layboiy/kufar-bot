"""
Формулы риска/маржи/шанса успеха/срока продажи — перенесены один в один
из вашего веб-трекера (артефакт «Учёт лотов — Часы / Kufar»), чтобы бот
считал лоты точно так же, как считал он.

Дополнительно здесь же — функции, которые пытаются САМИ определить входные
параметры (ликвидность, комплектность, цена относительно рынка и т.д.) по
тексту объявления, без ручного заполнения формы. Это оценка по эвристикам,
не точная экспертиза — как и в исходном трекере.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field


@dataclass
class LotInputs:
    price: float = 0.0
    shipping: float = 0.0
    resale: float = 0.0
    liquidity: str = "medium"          # high | medium | low
    complete: str = "unverified"       # full | partial | unverified
    seller: str = "new"                # trusted | new
    pricevsmarket: str = "normal"      # low | normal | high
    resalevsmarket: str = "realistic"  # optimistic | realistic | discount
    originality: str = "unverified"    # unverified | likely_original | uncertain | likely_fake


def compute_risk(lot: LotInputs) -> int:
    risk = 45
    if lot.liquidity == "high":
        risk -= 15
    elif lot.liquidity == "low":
        risk += 18

    if lot.complete == "full":
        risk -= 10
    elif lot.complete == "unverified":
        risk += 12

    if lot.seller == "trusted":
        risk -= 10
    else:
        risk += 8

    if lot.pricevsmarket == "low":
        risk += 14
    elif lot.pricevsmarket == "high":
        risk -= 4

    if lot.resalevsmarket == "optimistic":
        risk += 10
    elif lot.resalevsmarket == "discount":
        risk -= 5

    if lot.originality == "likely_original":
        risk -= 12
    elif lot.originality == "uncertain":
        risk += 15
    elif lot.originality == "likely_fake":
        risk += 40

    return max(5, min(95, round(risk)))


def compute_margin(lot: LotInputs) -> tuple[float, float]:
    amount = lot.resale - lot.price - lot.shipping
    base = lot.price + lot.shipping
    pct = (amount / base) * 100 if base > 0 else 0.0
    return amount, pct


def compute_success(risk: int, margin_pct: float) -> int:
    success = 100 - risk + (margin_pct / 12)
    return max(5, min(95, round(success)))


def compute_time_to_sell(lot: LotInputs) -> tuple[int, int]:
    if lot.liquidity == "high":
        lo, hi = 3, 8
    elif lot.liquidity == "low":
        lo, hi = 16, 35
    else:
        lo, hi = 7, 16

    if lot.resalevsmarket == "optimistic":
        lo += 6
        hi += 14
    elif lot.resalevsmarket == "discount":
        lo = max(1, lo - 3)
        hi = max(lo + 1, hi - 5)

    return lo, hi


def score(lot: LotInputs) -> dict:
    risk = compute_risk(lot)
    amount, pct = compute_margin(lot)
    success = compute_success(risk, pct)
    lo, hi = compute_time_to_sell(lot)
    return {
        "risk": risk,
        "margin_amount": round(amount, 2),
        "margin_pct": round(pct, 1),
        "success": success,
        "days_min": lo,
        "days_max": hi,
    }


# ---------------------------------------------------------------------------
# Автоматический вывод входных параметров по тексту объявления.
# Ручных полей в веб-версии было много (это осознанный выбор — риск оценки
# лота реально зависит от кучи мелочей). Бот не может физически знать то,
# что не написано в объявлении, поэтому там, где данных нет, он честно
# ставит "unverified"/"medium" и не выдаёт это за точный расчёт.
# ---------------------------------------------------------------------------

_FULL_SET_RE = re.compile(
    r"(коробк|докум|гарант|бирк|с биркой|полный комплект|box\s*\+|full\s*set)", re.IGNORECASE
)
_NO_BOX_RE = re.compile(r"(без коробк|без документ|только часы)", re.IGNORECASE)

_TRUSTED_SELLER_HINTS = re.compile(r"(магазин|официальный дилер|проверенный продавец)", re.IGNORECASE)


def guess_completeness(description: str) -> str:
    text = description or ""
    # Проверяем отрицание ("без коробки") раньше, иначе оно ложно
    # сработает на положительный паттерн (в "без коробки" тоже есть "коробк").
    if _NO_BOX_RE.search(text):
        return "partial"
    if _FULL_SET_RE.search(text):
        return "full"
    return "unverified"


def guess_seller(seller_name: str, reviews_count: int | None, description: str) -> str:
    if reviews_count and reviews_count > 0:
        return "trusted"
    if seller_name and _TRUSTED_SELLER_HINTS.search(seller_name):
        return "trusted"
    if description and _TRUSTED_SELLER_HINTS.search(description):
        return "trusted"
    return "new"


def robust_reference_price(prices: list[float]) -> float | None:
    """
    Грубый автоматический ориентир "рыночной" цены: медиана по всем текущим
    объявлениям того же среза (например, всем найденным Casio за этот
    прогон), с отсечением явных выбросов через межквартильный размах.
    Это статистика по объявлениям о ПРОДАЖЕ, а не по реальным сделкам —
    трактовать как очень приблизительный ориентир, не как точную рыночную
    цену конкретной модели.
    """
    clean = sorted(p for p in prices if p and p > 0)
    if len(clean) < 4:
        return statistics.median(clean) if clean else None
    q1 = clean[len(clean) // 4]
    q3 = clean[(len(clean) * 3) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    trimmed = [p for p in clean if lo <= p <= hi] or clean
    return statistics.median(trimmed)


def price_vs_reference(price: float, reference: float | None) -> str:
    if not reference or reference <= 0:
        return "normal"
    ratio = price / reference
    if ratio < 0.7:
        return "low"
    if ratio > 1.05:
        return "high"
    return "normal"


def resale_vs_reference(resale: float, reference: float | None) -> str:
    if not reference or reference <= 0:
        return "realistic"
    ratio = resale / reference
    if ratio > 1.15:
        return "optimistic"
    if ratio < 0.85:
        return "discount"
    return "realistic"


def price_category(price: float, categories: list[tuple[float, str]]) -> str:
    """Возвращает подпись ценовой категории по списку (порог, подпись) —
    см. config.PRICE_CATEGORIES. Порог включительный, категории должны идти
    по возрастанию, последняя обычно с порогом float('inf')."""
    if not categories:
        return ""
    for threshold, label in categories:
        if price <= threshold:
            return label
    return categories[-1][1]
