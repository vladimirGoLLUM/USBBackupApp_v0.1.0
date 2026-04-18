"""Formatting helpers for progress, ETA and transfer metrics."""

import math
from datetime import timedelta


class ProgressPresenter:
    """UI-agnostic formatter used by main window progress labels."""

    @staticmethod
    def format_seconds(seconds) -> str:
        """Форматирует длительность в виде ЧЧ:ММ:СС."""
        if seconds is None:
            return "--:--:--"
        return str(timedelta(seconds=max(0, int(math.ceil(seconds)))))

    @staticmethod
    def format_size(num_bytes: int, units: tuple[str, str, str, str]) -> str:
        """Форматирует размер с заданными подписями единиц (Б, КБ, МБ, ГБ)."""
        size = float(max(0, num_bytes))
        idx = 0
        while size >= 1024 and idx < len(units) - 1:
            size /= 1024
            idx += 1
        return f"{size:.1f} {units[idx]}"

    @staticmethod
    def format_speed_and_remaining(
        speed_bps: float,
        remaining_items: int,
        remaining_bytes: int,
        *,
        units: tuple[str, str, str, str],
        speed_template: str,
        items_template: str,
    ) -> str:
        """Скорость загрузки и остаток элементов/объёма (шаблоны из локализации)."""
        mbps = speed_bps / (1024 * 1024)
        size_str = ProgressPresenter.format_size(remaining_bytes, units)
        speed_part = speed_template.format(mbps=mbps)
        tail_part = items_template.format(items=max(0, remaining_items), size=size_str)
        return f"{speed_part} | {tail_part}"
