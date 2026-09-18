"""
Сбор новых объявлений с kufar.by.

Важно про robots.txt: страницы поиска по ключевому слову
(www.kufar.by/l?query=...) ЗАПРЕЩЕНЫ для всех ботов файлом robots.txt сайта.
Поэтому здесь бот НЕ ходит по поисковым URL с query-параметрами. Вместо
этого он читает обычные страницы категорий без параметров запроса
(например /l/muzhskie-chasy) — они не входят в список запрещённых путей —
и сам фильтрует объявления по ключевым словам среди уже показанных на
странице моделей. Это не обход правил площадки, а работа только с тем,
что она явно разрешает ботам читать.

Так как на странице категории нет предсказуемых CSS-классов, на которые
можно опереться (вёрстка может меняться), самое устойчивое, что там есть —
это сама ссылка вида /item/<id> (структура URL меняется намного реже
вёрстки). Поэтому со страницы категории берутся только id объявлений, а
все детали (название, цена, фото, состояние) берутся с отдельной страницы
объявления — там есть надёжные og:* мета-теги, которые сайт сам
генерирует для соцсетей и заведомо не собирается ломать.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
DELAY_BETWEEN_REQUESTS = 1.5  # вежливая пауза между запросами, секунды

# Категории без query-параметров — разрешены robots.txt.
CATEGORY_URLS = [
    "https://www.kufar.by/l/muzhskie-chasy",
    "https://www.kufar.by/l/zhenskie-chasy",
]

# Бот проверяет ВСЕ бренды часов из категорий выше. Если когда-нибудь
# захочется сузить обратно до конкретных брендов/моделей — впишите сюда
# регулярки, и будут проходить только совпадения. Пусто = проходят все.
BRAND_FILTER: list[str] = []
BRAND_FILTER_RE = re.compile("|".join(BRAND_FILTER), re.IGNORECASE) if BRAND_FILTER else None

# Категории "наручные часы" на Kufar иногда содержат не сами часы, а
# ремешки/запчасти/ремонт — их отсеиваем всегда, независимо от BRAND_FILTER,
# т.к. это не то, что имеет смысл предлагать на перепродажу как часы.
JUNK_RE = re.compile(
    r"ремешок|ремень\s+для\s+час|браслет\s+для\s+час|батарейк|запчаст"
    r"|ремонт\s+час|стекло\s+для\s+час|инструмент\s+для\s+час|шкатулк|коробочк[аи]\s+для\s+час",
    re.IGNORECASE,
)

# Для грубого автоматического ориентира цены (см. scoring.robust_reference_price)
# полезно сравнивать похожие друг на друга часы, а не вообще все подряд —
# поэтому цены группируются по этому "бренду", извлечённому из названия.
BRAND_GUESS_RE = re.compile(r"час[ыи]\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\-]*)", re.IGNORECASE)

ITEM_ID_RE = re.compile(r"/item/(\d+)")
PRICE_IN_TITLE_RE = re.compile(r"цена\s+([\d\s]+(?:[.,]\d+)?)\s*р", re.IGNORECASE)
OG_TAG_RE_TMPL = r'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']'


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-BY,ru;q=0.9"})
    return s


def fetch(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def discover_item_ids(session: requests.Session, category_urls: list[str] | None = None) -> list[int]:
    """Возвращает id объявлений, увиденных сейчас на первых страницах категорий
    (без пагинации через query-параметры — она запрещена robots.txt)."""
    ids: set[int] = set()
    for url in category_urls or CATEGORY_URLS:
        html_text = fetch(session, url)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        if not html_text:
            continue
        for m in ITEM_ID_RE.finditer(html_text):
            ids.add(int(m.group(1)))
    return sorted(ids, reverse=True)  # сначала более новые (обычно больший id)


def _extract_og(html_text: str, prop: str) -> str | None:
    m = re.search(OG_TAG_RE_TMPL.format(prop=prop), html_text, re.IGNORECASE)
    if not m:
        return None
    return html.unescape(m.group(1)).strip()


def _extract_meta_description(html_text: str) -> str | None:
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        html_text,
        re.IGNORECASE,
    )
    if not m:
        return None
    return html.unescape(m.group(1)).strip()


@dataclass
class ItemDetails:
    ad_id: int
    link: str
    title: str | None
    price: float | None
    image_url: str | None
    description: str | None
    condition: str | None
    seller_name: str | None
    seller_listing_count: int | None
    raw_ok: bool


def fetch_item_details(session: requests.Session, ad_id: int) -> ItemDetails:
    link = f"https://www.kufar.by/item/{ad_id}"
    html_text = fetch(session, link)
    time.sleep(DELAY_BETWEEN_REQUESTS)
    if not html_text:
        return ItemDetails(ad_id, link, None, None, None, None, None, None, None, False)

    og_title = _extract_og(html_text, "og:title")
    og_image = _extract_og(html_text, "og:image")
    og_description = _extract_og(html_text, "og:description") or _extract_meta_description(html_text)

    price = None
    source_for_price = og_title or og_description or ""
    m = PRICE_IN_TITLE_RE.search(source_for_price)
    if m:
        digits = m.group(1).replace(" ", "").replace(",", ".")
        try:
            price = float(digits)
        except ValueError:
            price = None

    condition = None
    m = re.search(r"Состояние:\s*([^\n<]{2,20})", html_text, re.IGNORECASE)
    if m:
        condition = html.unescape(m.group(1)).strip()
    elif og_description and re.search(r"состояние:\s*новое", og_description, re.IGNORECASE):
        condition = "Новое"

    seller_name = None
    m = re.search(r'"sellerName"\s*:\s*"([^"]{2,80})"', html_text)
    if m:
        seller_name = html.unescape(m.group(1)).strip()

    seller_listing_count = None
    m = re.search(r"(\d+)\s*(?:объявлени[ея]|объявлений)", html_text, re.IGNORECASE)
    if m:
        try:
            seller_listing_count = int(m.group(1))
        except ValueError:
            pass

    return ItemDetails(
        ad_id=ad_id,
        link=link,
        title=og_title,
        price=price,
        image_url=og_image,
        description=og_description,
        condition=condition,
        seller_name=seller_name,
        seller_listing_count=seller_listing_count,
        raw_ok=True,
    )


def passes_filters(title: str | None, description: str | None) -> bool:
    """True, если объявление стоит предлагать: не мусор (ремешки/запчасти/
    ремонт), и — если задан BRAND_FILTER — совпадает с одним из брендов."""
    combined = f"{title or ''} {description or ''}"
    if JUNK_RE.search(combined):
        return False
    if BRAND_FILTER_RE is None:
        return True
    return bool(BRAND_FILTER_RE.search(combined))


def extract_brand(title: str | None) -> str | None:
    """Грубое извлечение "бренда" из названия для группировки цен при расчёте
    ориентира рынка — не претендует на точность, просто общий бакет для
    похожих друг на друга часов (см. scoring.robust_reference_price)."""
    if not title:
        return None
    m = BRAND_GUESS_RE.search(title)
    if not m:
        return None
    return m.group(1).strip().lower()
