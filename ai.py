"""
Опциональная интеграция с Anthropic API (Claude) для двух задач:

1. Автоматическая грубая оценка подлинности часов по фото из объявления —
   выполняется сразу при обнаружении нового лота.
2. Генерация текста объявления для перепродажи — выполняется после того,
   как вы одобрили лот и прислали боту свои фото уже купленной вещи.

Работает ТОЛЬКО если задан секрет ANTHROPIC_API_KEY (см. README). Без
него обе функции просто возвращают None, и вызывающий код в main.py
использует запасной вариант (originality остаётся "unverified", текст
объявления собирается из шаблона в formatting.listing_template) — бот
полностью работоспособен и без этого ключа, просто менее "умный".

Это платный API (у Anthropic), стоимость одного запроса с одной
небольшой фотографией — доли цента при использовании модели Haiku, но
это не бесплатно и требует вашего собственного API-ключа с сайта
console.anthropic.com.
"""

from __future__ import annotations

import base64
import re

import requests

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

API_URL = "https://api.anthropic.com/v1/messages"
REQUEST_TIMEOUT = 45


def available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _guess_media_type(url_or_hint: str) -> str:
    lower = (url_or_hint or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _image_block(image_bytes: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        },
    }


def download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.content


def _call_claude(content_blocks: list[dict], max_tokens: int = 500) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    parts = data.get("content", [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    return text.strip() or None


_VERDICT_RE = re.compile(r"вердикт:\s*(похоже на оригинал|есть сомнения|похоже на подделку)", re.IGNORECASE)
_VERDICT_TO_CODE = {
    "похоже на оригинал": "likely_original",
    "есть сомнения": "uncertain",
    "похоже на подделку": "likely_fake",
}


def parse_verdict(text: str) -> str:
    m = _VERDICT_RE.search(text or "")
    if not m:
        return "uncertain"
    return _VERDICT_TO_CODE.get(m.group(1).lower(), "uncertain")


def check_authenticity(image_url: str | None, title: str | None) -> tuple[str, str] | None:
    """Возвращает (код_вердикта, текст_объяснения) или None, если ИИ
    недоступен/фото не скачалось/запрос не удался — тогда вызывающий код
    просто оставляет оригинальность как "не проверено"."""
    if not available() or not image_url:
        return None
    img_bytes = download_image(image_url)
    if not img_bytes:
        return None

    prompt = (
        "Ты помогаешь оценить подлинность наручных часов по фотографии перед "
        f"покупкой на вторичном рынке в Беларуси. Объявление: \"{title or 'без названия'}\". "
        "Оцени видимые признаки оригинальности: чёткость логотипа и гравировки, "
        "качество шрифта, ровность деталей корпуса, типичные признаки реплик для "
        "этого бренда. Ответь кратко (3-4 предложения), простым языком, на русском. "
        "В конце ОБЯЗАТЕЛЬНО отдельной строкой напиши вердикт ровно в одном из "
        "форматов: \"Вердикт: похоже на оригинал\" ИЛИ \"Вердикт: есть сомнения\" "
        "ИЛИ \"Вердикт: похоже на подделку\". Если по фото невозможно определить — "
        "пиши \"Вердикт: есть сомнения\" и поясни, каких ракурсов не хватает."
    )
    content = [
        _image_block(img_bytes, _guess_media_type(image_url)),
        {"type": "text", "text": prompt},
    ]
    text = _call_claude(content, max_tokens=300)
    if not text:
        return None
    return parse_verdict(text), text


def generate_listing_text(lot: dict, photo_bytes_list: list[bytes]) -> str | None:
    """photo_bytes_list — реальные фото уже купленной вещи от пользователя
    (а не фото из чужого объявления). Возвращает готовый текст объявления
    или None, если ИИ недоступен/не ответил — тогда используется
    formatting.listing_template как запасной вариант."""
    if not available():
        return None

    details = (
        f"Модель/название: {lot.get('title') or 'не указано'}\n"
        f"Желаемая цена продажи: {lot.get('resale') or lot.get('price')} р.\n"
        f"Состояние по исходному объявлению продавца: {lot.get('condition') or 'не указано'}\n"
        f"Комплектность: {lot.get('complete')}\n"
    )
    prompt = (
        "Напиши текст объявления о продаже наручных часов для площадки Kufar.by "
        "на русском языке: короткий цепляющий заголовок и описание из 4-8 "
        "предложений честным, но продающим тоном — используй только данные ниже "
        "и то, что реально видно на приложенных фото, не выдумывай характеристики. "
        "В конце добавь короткий блок «Состояние:» и «Комплектность:». Не пиши, "
        "что текст сгенерирован ИИ.\n\n" + details
    )
    content: list[dict] = [_image_block(b, "image/jpeg") for b in photo_bytes_list]
    content.append({"type": "text", "text": prompt})
    return _call_claude(content, max_tokens=700)
