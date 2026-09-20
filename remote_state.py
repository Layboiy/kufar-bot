"""
Необязательное (но рекомендуемое при работе на бесплатном постоянном
хостинге) сохранение состояния бота в GitHub — нужно, чтобы прогресс
(одобренные лоты, статусы, история цен) не терялся, если хостинг
перезапустит процесс бота (например, после обновления кода). Использует
тот же GitHub-репозиторий, что и сам код бота, через обычный REST API
(Contents API) — коммитит файл data/state.json от имени владельца токена.

Без переменной окружения GITHUB_TOKEN эта часть просто не работает — бот
использует только локальный файл data/state.json внутри контейнера
хостинга. Такой файл переживает обычную работу процесса (пока хостинг его
не перезапускает), но обнуляется при редеплое нового кода — поэтому для
постоянно работающего бота рекомендуется всё же настроить GITHUB_TOKEN.
"""

from __future__ import annotations

import base64
import json

import requests

from config import GITHUB_BRANCH, GITHUB_REPO, GITHUB_STATE_PATH, GITHUB_TOKEN

API_ROOT = "https://api.github.com"
_TIMEOUT = 20

# Кэшируем sha последней известной версии файла — GitHub Contents API требует
# его при обновлении существующего файла (чтобы не затирать чужие изменения
# вслепую).
_last_sha: str | None = None


def available() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch() -> dict | None:
    """Пытается загрузить состояние из GitHub. Возвращает None, если
    GITHUB_TOKEN не задан, файла ещё нет или запрос не удался — тогда
    вызывающий код должен упасть обратно на локальный load_state()."""
    global _last_sha
    if not available():
        return None
    url = f"{API_ROOT}/repos/{GITHUB_REPO}/contents/{GITHUB_STATE_PATH}"
    try:
        resp = requests.get(url, headers=_headers(), params={"ref": GITHUB_BRANCH}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[warn] не удалось получить состояние из GitHub: {exc}")
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"[warn] GitHub вернул {resp.status_code} при чтении состояния")
        return None
    data = resp.json()
    _last_sha = data.get("sha")
    try:
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content)
    except Exception as exc:
        print(f"[warn] не удалось разобрать состояние из GitHub: {exc}")
        return None


def push(state: dict) -> bool:
    """Коммитит текущее состояние в GitHub. Возвращает True при успехе."""
    global _last_sha
    if not available():
        return False
    url = f"{API_ROOT}/repos/{GITHUB_REPO}/contents/{GITHUB_STATE_PATH}"
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    payload = {
        "message": "Обновление состояния бота [skip ci]",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if _last_sha:
        payload["sha"] = _last_sha
    try:
        resp = requests.put(url, headers=_headers(), json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[warn] не удалось сохранить состояние в GitHub: {exc}")
        return False
    if resp.status_code in (200, 201):
        try:
            _last_sha = resp.json().get("content", {}).get("sha")
        except Exception:
            pass
        return True
    if resp.status_code == 409:
        # Кто-то/что-то другое обновило файл — перечитаем sha и попробуем на
        # следующем цикле, чтобы не затереть чужие изменения вслепую.
        fetch()
        return False
    print(f"[warn] GitHub вернул {resp.status_code} при сохранении состояния: {resp.text[:200]}")
    return False
