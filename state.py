"""Хранение состояния бота в одном JSON-файле (data/state.json).

GitHub Actions запускает бота заново при каждом срабатывании по расписанию —
никакой процесс не живёт постоянно в памяти, поэтому всё, что должно
"помниться" между запусками (какие объявления уже видели, что одобрено,
статусы лотов, offset для getUpdates), хранится в этом файле, а после
каждого запуска workflow коммитит обновлённую версию обратно в репозиторий.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).parent / "data" / "state.json"

DEFAULT_STATE: dict[str, Any] = {
    "last_update_id": None,
    "seen_ad_ids": [],
    "recent_prices": [],
    "recent_prices_by_brand": {},
    "lots": {},
    "known_models": {},
    "awaiting": {},
}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    with open(STATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for key, value in DEFAULT_STATE.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_PATH)


def remember_seen(state: dict[str, Any], ad_id: int, cap: int = 5000) -> None:
    seen: list[int] = state["seen_ad_ids"]
    if ad_id not in seen:
        seen.append(ad_id)
        if len(seen) > cap:
            del seen[: len(seen) - cap]


def remember_price(state: dict[str, Any], price: float, brand: str | None = None,
                    cap: int = 300, brand_cap: int = 100) -> None:
    """Запоминает цену для общей статистики и, если известен "бренд"
    (см. scraper.extract_brand), отдельно в его бакете — так ориентир
    рынка сравнивает похожие часы друг с другом, а не Casio с Rolex."""
    if not price or price <= 0:
        return
    prices: list[float] = state["recent_prices"]
    prices.append(price)
    if len(prices) > cap:
        del prices[: len(prices) - cap]

    if brand:
        by_brand: dict[str, list[float]] = state.setdefault("recent_prices_by_brand", {})
        bucket = by_brand.setdefault(brand, [])
        bucket.append(price)
        if len(bucket) > brand_cap:
            del bucket[: len(bucket) - brand_cap]
