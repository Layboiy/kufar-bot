from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    """os.environ.get, но пустая строка считается "не задано" — так GitHub
    Actions подставляет неустановленные vars.*/secrets.* в env, и это не
    должно перебивать разумные значения по умолчанию."""
    value = os.environ.get(name)
    return value if value else default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")

# Сколько НОВЫХ (ранее не встреченных) объявлений детально проверять за один
# запуск. Ограничение защищает от перегрузки в первый запуск, когда "новыми"
# окажутся сразу все объявления на странице категории. Бот теперь смотрит
# на все бренды часов, а не только Casio, так что новых объявлений в целом
# будет больше — при желании увеличьте это число.
MAX_NEW_ITEMS_PER_RUN = int(_env("MAX_NEW_ITEMS_PER_RUN", "30"))

# Лоты с расчётным риском выше этого порога вообще не присылаются —
# отсеиваются автоматически, чтобы не засорять чат сомнительными вариантами.
MAX_RISK_TO_SUGGEST = int(_env("MAX_RISK_TO_SUGGEST", "30"))

# Опциональная интеграция с Anthropic API (Claude) — если ключ не задан,
# бот работает как раньше: без автопроверки подлинности по фото и без
# ИИ-описаний объявлений (только шаблонные). Смотрите README про добавление
# секрета ANTHROPIC_API_KEY. Актуальный список моделей и их идентификаторы —
# на docs.claude.com; если модель по умолчанию устареет/будет недоступна,
# задайте свою через секрет/переменную ANTHROPIC_MODEL.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")

# Ценовые категории для группировки лотов в сообщениях/списках.
# (верхняя граница включительно, подпись). Последняя категория — "всё, что
# выше" — ловит любую цену больше предыдущего порога.
PRICE_CATEGORIES: list[tuple[float, str]] = [
    (50, "💸 Бюджет (до 50 р.)"),
    (200, "🙂 Средний (50–200 р.)"),
    (500, "💎 Выше среднего (200–500 р.)"),
    (float("inf"), "👑 Премиум (500+ р.)"),
]

STATUS_LABELS = {
    "watching": "Наблюдаю",
    "negotiating": "Торгуюсь",
    "bought": "Куплено",
    "listed": "Выставлено на продажу",
    "sold": "Продано",
}

STATUS_ORDER = ["watching", "negotiating", "bought", "listed", "sold"]

ORIGINALITY_LABELS = {
    "unverified": "Не проверено",
    "likely_original": "Похоже на оригинал",
    "uncertain": "Есть сомнения",
    "likely_fake": "Похоже на подделку",
}
