"""
Тонкая обёртка над Telegram Bot API поверх обычных HTTP-запросов.

Бот работает не как постоянно запущенный процесс (long polling), а как
короткий скрипт, который GitHub Actions запускает раз в N минут:
- один раз спрашивает getUpdates (с offset) — забирает всё, что накопилось
  с прошлого запуска (нажатия на кнопки, текстовые ответы, команды);
- обрабатывает это;
- рассылает новые уведомления;
- завершается.

Telegram копит обновления на своей стороне, пока их не заберут через
getUpdates — так что ничего не теряется, просто реакция бота приходит не
мгновенно, а с задержкой до одного интервала запуска (обычно 10 минут).
"""

from __future__ import annotations

import time
from typing import Any

import requests

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Пустой BOT_TOKEN")
        self.token = token

    def _call(self, method: str, **params: Any) -> Any:
        url = API_ROOT.format(token=self.token, method=method)
        files = params.pop("_files", None)
        for attempt in range(3):
            try:
                if files:
                    resp = requests.post(url, data=params, files=files, timeout=TIMEOUT)
                else:
                    resp = requests.post(url, data=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TelegramError(f"Сеть: {method} не удался: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                retry_after = 3
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                except Exception:
                    pass
                time.sleep(retry_after + 1)
                continue
            data = resp.json()
            if not data.get("ok"):
                raise TelegramError(f"{method} -> {data}")
            return data["result"]
        raise TelegramError(f"{method}: не удалось выполнить после повторов")

    # ---- методы, которые реально нужны боту ----

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        params["allowed_updates"] = '["message","callback_query"]'
        return self._call("getUpdates", **params)

    def send_message(self, chat_id: int | str, text: str, reply_markup: str | None = None,
                      parse_mode: str = "HTML", disable_web_page_preview: bool = True) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("sendMessage", **params)

    def send_photo(self, chat_id: int | str, photo_url: str, caption: str,
                    reply_markup: str | None = None, parse_mode: str = "HTML") -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            return self._call("sendPhoto", **params)
        except TelegramError:
            # Telegram иногда не может сам скачать конкретную ссылку на фото
            # (например, из-за заголовков сайта-источника) — не теряем лот,
            # просто присылаем текстом со ссылкой на фото.
            text = caption + f"\n\n(фото не загрузилось, ссылка: {photo_url})"
            return self.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str,
                           reply_markup: str | None = None, parse_mode: str = "HTML") -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("editMessageText", **params)

    def edit_message_caption(self, chat_id: int | str, message_id: int, caption: str,
                              reply_markup: str | None = None, parse_mode: str = "HTML") -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("editMessageCaption", **params)

    def edit_message_reply_markup(self, chat_id: int | str, message_id: int,
                                   reply_markup: str | None) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("editMessageReplyMarkup", **params)

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> dict:
        return self._call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert,
        )
