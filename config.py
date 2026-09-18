from __future__ import annotations

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")

# Сколько НОВЫХ (ранее не встреченных) объявлений детально проверять за один
# запуск. Ограничение защищает от перегрузки в первый запуск, когда "новыми"
# окажутся сразу все объявления на странице категории. Бот теперь смотрит
# на все бренды часов, а не только Casio, так что новых объявлений в целом
# будет больше — при желании увеличьте это число.
MAX_NEW_ITEMS_PER_RUN = int(os.environ.get("MAX_NEW_ITEMS_PER_RUN", "30"))

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
