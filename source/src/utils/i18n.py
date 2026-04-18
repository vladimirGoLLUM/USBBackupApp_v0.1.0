"""Загрузка словарей переводов и переключение языка интерфейса."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.app_paths import PROJECT_ROOT, resource_path


def resolve_translations_path() -> Path:
    """Возвращает путь к translations.json (dev или PyInstaller bundle)."""
    dev = PROJECT_ROOT / "data" / "translations.json"
    if dev.is_file():
        return dev
    return resource_path("data/translations.json")


class LanguageManager:
    """Загружает JSON-переводы и отдаёт строки для текущего языка."""

    SUPPORTED: tuple[str, ...] = ("ru", "en")

    def __init__(self, *, initial_language: str = "ru") -> None:
        """Инициализирует менеджер: читает файл переводов и выставляет язык."""
        self._tables: dict[str, dict[str, str]] = self._load_tables()
        self._language = self._normalize_language(initial_language)

    def _normalize_language(self, code: str) -> str:
        """Приводит код языка к поддерживаемому значению."""
        c = (code or "ru").strip().lower()
        return c if c in self.SUPPORTED else "ru"

    def _load_tables(self) -> dict[str, dict[str, str]]:
        """Читает translations.json; при ошибке возвращает пустые таблицы."""
        path = resolve_translations_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {lang: {} for lang in self.SUPPORTED}
        out: dict[str, dict[str, str]] = {}
        for lang in self.SUPPORTED:
            block = raw.get(lang)
            if isinstance(block, dict):
                out[lang] = {str(k): str(v) for k, v in block.items()}
            else:
                out[lang] = {}
        return out

    @property
    def language(self) -> str:
        """Текущий код языка (ru или en)."""
        return self._language

    def set_language(self, code: str) -> None:
        """Устанавливает активный язык интерфейса."""
        self._language = self._normalize_language(code)

    def get(self, key: str, **kwargs: Any) -> str:
        """
        Возвращает перевод по ключу.

        Поддерживает подстановку плейсхолдеров через str.format.
        """
        table = self._tables.get(self._language, {})
        template = table.get(key)
        if template is None:
            template = self._tables.get("ru", {}).get(key, key)
        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, ValueError, IndexError):
            return template
