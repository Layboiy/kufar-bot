"""Локальные проверки без сети: формулы скоринга, состояние, сборка лота.

Запуск из корня репозитория: python tests/offline_selftest.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scoring
import state as state_mod
from scraper import ItemDetails

errors = []


def check(name, cond):
    print(("OK  " if cond else "FAIL") + " " + name)
    if not cond:
        errors.append(name)


# --- 1. Формулы скоринга: пример 1:1 из артефакта ---
# liquidity=medium(0), complete=partial(0), seller=new(+8), pricevsmarket=normal(0),
# resalevsmarket=realistic(0), originality=unverified(0) => risk = 45+8 = 53
lot = scoring.LotInputs(price=100, shipping=0, resale=150, liquidity="medium", complete="partial",
                          seller="new", pricevsmarket="normal", resalevsmarket="realistic",
                          originality="unverified")
risk = scoring.compute_risk(lot)
check("risk базовый (ожидание 53)", risk == 53)

amount, pct = scoring.compute_margin(lot)
check("margin amount = 50", amount == 50)
check("margin pct = 50.0", abs(pct - 50.0) < 1e-9)

success = scoring.compute_success(risk, pct)
# success = 100 - 53 + 50/12 = 47 + 4.1666 = 51.1666 -> round -> 51
check(f"success (ожидание 51, получили {success})", success == 51)

lo, hi = scoring.compute_time_to_sell(lot)
check(f"срок продажи medium/realistic (7,16), получили {(lo, hi)}", (lo, hi) == (7, 16))

# --- 2. Экстремальные случаи ликвидности/оригинальности ---
lot2 = scoring.LotInputs(price=100, shipping=0, resale=90, liquidity="high", complete="full",
                           seller="trusted", pricevsmarket="low", resalevsmarket="discount",
                           originality="likely_fake")
# risk = 45 -15 -10 -10 +14 -5 +40 = 59
risk2 = scoring.compute_risk(lot2)
check(f"risk с likely_fake (ожидание 59, получили {risk2})", risk2 == 59)

# --- 3. Клампинг риска в диапазон 5..95 ---
lot3 = scoring.LotInputs(price=100, shipping=0, resale=90, liquidity="low", complete="unverified",
                           seller="new", pricevsmarket="low", resalevsmarket="optimistic",
                           originality="likely_fake")
risk3 = scoring.compute_risk(lot3)
check(f"risk клампится к 95 (получили {risk3})", risk3 == 95)

# --- 4. Референсная цена и сравнение с рынком ---
prices = [200, 210, 190, 205, 1500, 195, 208]  # 1500 — явный выброс
ref = scoring.robust_reference_price(prices)
check(f"выброс 1500 отфильтрован из медианы (получили {ref})", ref is not None and ref < 300)

check("цена сильно ниже референса -> low", scoring.price_vs_reference(100, 200) == "low")
check("цена около референса -> normal", scoring.price_vs_reference(195, 200) == "normal")
check("цена выше референса -> high", scoring.price_vs_reference(230, 200) == "high")

# --- 5. Угадывание комплектности/продавца по тексту ---
check("полный комплект по описанию", scoring.guess_completeness("Продаю часы, полный комплект, коробка и документы") == "full")
check("без коробки по описанию", scoring.guess_completeness("Часы без коробки, только сами часы") == "partial")
check("неясно по описанию", scoring.guess_completeness("Хорошие часы, торг уместен") == "unverified")
check("доверенный продавец по числу отзывов", scoring.guess_seller("Иван", 12, "") == "trusted")
check("новый продавец без истории", scoring.guess_seller("Пётр", 0, "") == "new")

# --- 6. state.py: сохранение/загрузка ---
state_mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
s = state_mod.load_state()
s["known_models"]["ga-2100"] = {"resale_price": 250, "liquidity": "high"}
state_mod.remember_seen(s, 123456)
state_mod.remember_price(s, 199.99)
state_mod.save_state(s)
s2 = state_mod.load_state()
check("known_models сохранился", s2["known_models"].get("ga-2100", {}).get("resale_price") == 250)
check("seen_ad_ids сохранился", 123456 in s2["seen_ad_ids"])
check("recent_prices сохранился", 199.99 in s2["recent_prices"])

# откатываем тестовые данные, чтобы не тащить их в реальный state.json
import os
os.remove(state_mod.STATE_PATH)
with open(state_mod.STATE_PATH, "w", encoding="utf-8") as f:
    json.dump(state_mod.DEFAULT_STATE, f, ensure_ascii=False, indent=2, sort_keys=True)

# --- 7. Сборка карточки лота из "просканированных" данных (без сети) ---
sys.path.insert(0, ".")
import main as bot_main  # noqa: E402

fake_state = state_mod.load_state()
fake_state["known_models"]["ga-2100"] = {"resale_price": 250, "liquidity": "high"}
fake_state["recent_prices"] = [150, 160, 155, 158]

details_known = ItemDetails(
    ad_id=1, link="https://www.kufar.by/item/1", title="Casio GA-2100 новые, цена 120 р. купить в Минске",
    price=120.0, image_url="https://example.com/x.jpg",
    description="Новое, полный комплект, коробка и документы", condition="Новое",
    seller_name="Магазин часов", seller_listing_count=40, raw_ok=True,
)
built = bot_main._build_lot(fake_state, details_known)
check("известная модель даёт resale из known_models", built["resale"] == 250)
check("известная модель даёт liquidity=high", built["liquidity"] == "high")
check("полный комплект распознан", built["complete"] == "full")
check("доверенный продавец распознан", built["seller"] == "trusted")
sc = bot_main._score_lot(built)
check("скор посчитан (risk в диапазоне 5..95)", 5 <= sc["risk"] <= 95)
print(json.dumps(sc, ensure_ascii=False))

details_unknown = ItemDetails(
    ad_id=2, link="https://www.kufar.by/item/2", title="Casio Classic, цена 30 р. купить в Минске",
    price=30.0, image_url=None, description="Б/у, без коробки", condition="Б/у",
    seller_name="Пётр", seller_listing_count=0, raw_ok=True,
)
built2 = bot_main._build_lot(fake_state, details_unknown)
# Раньше без /addmodel resale всегда оставался 0 ("укажите сами"). Теперь,
# если есть хоть какая-то история цен (пусть даже не по этому бренду),
# используется грубый глобальный ориентир — маржа видна сразу, с пометкой.
check(
    f"неизвестная модель берёт грубый ориентир из истории цен (получили {built2['resale']})",
    built2["resale"] == scoring.robust_reference_price(fake_state["recent_prices"]),
)
check("источник ориентира помечен как global", built2["resale_source"] == "global")
check("неизвестная модель liquidity=medium по умолчанию", built2["liquidity"] == "medium")
check("без коробки распознано как partial", built2["complete"] == "partial")
check("новый продавец без отзывов -> new", built2["seller"] == "new")

details_no_history = ItemDetails(
    ad_id=3, link="https://www.kufar.by/item/3", title="Неизвестные часы, цена 40 р.",
    price=40.0, image_url=None, description="Б/у", condition="Б/у",
    seller_name="Аноним", seller_listing_count=0, raw_ok=True,
)
built3 = bot_main._build_lot(state_mod.load_state(), details_no_history)
check("совсем без истории цен resale=0 и источник none", built3["resale"] == 0.0 and built3["resale_source"] == "none")

# --- 8. Разбор HTML (офлайн, без сети): og:*-теги и id объявлений ---
import scraper

sample_item_html = """
<html><head>
<meta property="og:title" content="Наручные часы Casio Classic *Разные цвета*, цена 29.99 р. купить в Минске на Куфаре" />
<meta property="og:image" content="https://rms.kufar.by/v1/list_thumbs_2x/adim1/abc.jpg" />
<meta property="og:description" content="Обратите внимание на объявление: Наручные часы Casio Classic, цена 29.99 р.. Состояние: Новое." />
</head><body>
Состояние: Новое
55 объявлений на сайте
"sellerName":"Altin.by"
</body></html>
"""
title = scraper._extract_og(sample_item_html, "og:title")
check("og:title вытащен", title == "Наручные часы Casio Classic *Разные цвета*, цена 29.99 р. купить в Минске на Куфаре")
m = scraper.PRICE_IN_TITLE_RE.search(title)
check("цена из og:title = 29.99", m is not None and float(m.group(1).replace(" ", "").replace(",", ".")) == 29.99)
image = scraper._extract_og(sample_item_html, "og:image")
check("og:image вытащен", image == "https://rms.kufar.by/v1/list_thumbs_2x/adim1/abc.jpg")
check("любой бренд проходит фильтр (BRAND_FILTER пуст)", scraper.passes_filters(title, None))
check("пустое название тоже проходит (не мусор)", scraper.passes_filters(None, None))
check("ремешок отсеивается как мусор", not scraper.passes_filters("Ремешок для часов Casio, кожаный", None))
check("запчасти отсеиваются как мусор", not scraper.passes_filters("Запчасти для часов", None))
check("извлечён бренд Casio из названия", scraper.extract_brand(title) == "casio")
check("бренд не извлекается из пустого", scraper.extract_brand(None) is None)

sample_category_html = """
<a href="/item/1036330875">SEVENFRIDAY</a>
<a href="/item/1080152682?utm=1">LUMINOX</a>
<a href="/item/232895778">Casio Classic</a>
"""
ids = set()
for mm in scraper.ITEM_ID_RE.finditer(sample_category_html):
    ids.add(int(mm.group(1)))
check(f"id объявлений вытащены из категории (получили {sorted(ids)})", ids == {1036330875, 1080152682, 232895778})

# --- 9. Форматирование сообщений не падает и даёт валидный JSON клавиатуры ---
import formatting

sc_demo = bot_main._score_lot(built)
text_demo = formatting.suggestion_text(built, sc_demo, "тестовая заметка")
check("suggestion_text вернул непустую строку", isinstance(text_demo, str) and len(text_demo) > 10)
kb_demo = formatting.suggestion_keyboard(built["ad_id"])
check("suggestion_keyboard — валидный JSON", json.loads(kb_demo)["inline_keyboard"][0][0]["callback_data"] == f"appr:{built['ad_id']}")

built["status"] = "listed"
approved_txt = formatting.approved_text(built, sc_demo)
check("approved_text вернул непустую строку", isinstance(approved_txt, str) and len(approved_txt) > 10)
approved_kb = formatting.approved_keyboard(built["ad_id"], "listed")
check("approved_keyboard — валидный JSON", "inline_keyboard" in json.loads(approved_kb))

# --- 9b. Ценовые категории, ИИ-заглушка без ключа, шаблон объявления ---
import config as config_mod
check(
    "бюджетная категория",
    scoring.price_category(30, config_mod.PRICE_CATEGORIES) == config_mod.PRICE_CATEGORIES[0][1],
)
check(
    "премиум-категория (очень дорогой лот)",
    scoring.price_category(10_000, config_mod.PRICE_CATEGORIES) == config_mod.PRICE_CATEGORIES[-1][1],
)

import ai as ai_mod
check("без ANTHROPIC_API_KEY ai.available() == False", ai_mod.available() is False)
check("без ключа check_authenticity возвращает None", ai_mod.check_authenticity("http://example.com/x.jpg", "Casio") is None)
check("без ключа generate_listing_text возвращает None", ai_mod.generate_listing_text(built, []) is None)
check(
    "parse_verdict распознаёт «похоже на подделку»",
    ai_mod.parse_verdict("бла бла\nВердикт: похоже на подделку") == "likely_fake",
)
check(
    "parse_verdict по умолчанию — uncertain",
    ai_mod.parse_verdict("что-то без вердикта") == "uncertain",
)

template_text = formatting.listing_template(built)
check("listing_template вернул непустой текст", isinstance(template_text, str) and len(template_text) > 10)

built_with_verdict = dict(built)
built_with_verdict["originality"] = "likely_fake"
built_with_verdict["originality_note"] = "Логотип смазан, похоже на реплику."
sc_verdict = bot_main._score_lot(built_with_verdict)
verdict_text = formatting.suggestion_text(built_with_verdict, sc_verdict, None)
check(
    "suggestion_text показывает вердикт и заметку ИИ",
    "похоже на подделку".lower() in verdict_text.lower() and "смазан" in verdict_text,
)

# --- 10. Ориентир цены по бренду, когда конкретной модели в /addmodel нет ---
brand_state = state_mod.load_state()
for p in (140, 150, 160, 145):  # 4 образца — достаточно для брендового ориентира
    state_mod.remember_price(brand_state, p, brand="orient")

ref, source = bot_main._resolve_reference(brand_state, "Часы Orient классические")
check(f"ориентир по бренду сработал (получили {(ref, source)})", source == "brand:orient" and ref is not None)

ref_none, source_none = bot_main._resolve_reference(state_mod.load_state(), "Часы Vostok Amphibia")
check(
    f"без образцов и без /addmodel — ориентира нет (получили {(ref_none, source_none)})",
    source_none == "none" and ref_none is None,
)

# --- 11. Постоянный режим: настройки по умолчанию и remote_state без репозитория ---
check("SCAN_INTERVAL_SECONDS задан положительным числом", config_mod.SCAN_INTERVAL_SECONDS > 0)
check("PORT задан положительным числом", config_mod.PORT > 0)
check("GITHUB_REPO по умолчанию пуст (без явной настройки)", config_mod.GITHUB_REPO == "")

import remote_state

# available() требует и токен, и репозиторий — без GITHUB_REPO (как в этом
# тестовом окружении) должно быть False, даже если GITHUB_TOKEN случайно
# задан средой (например, самой песочницей для несвязанных целей).
check("без GITHUB_REPO remote_state.available() == False", remote_state.available() is False)
check("без репозитория fetch() возвращает None", remote_state.fetch() is None)
check("без репозитория push() возвращает False", remote_state.push({"x": 1}) is False)

print()
if errors:
    print(f"ИТОГО: {len(errors)} ошибок: {errors}")
    sys.exit(1)
else:
    print("ИТОГО: все проверки пройдены.")
