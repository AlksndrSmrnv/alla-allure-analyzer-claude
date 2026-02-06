"""Настройка логирования для приложения alla."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Настроить корневой логгер со структурированным форматом.

    Args:
        level: Имя уровня логирования (DEBUG, INFO, WARNING, ERROR).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Избежать дублирования обработчиков при повторных вызовах
    root.handlers.clear()
    root.addHandler(handler)

    # Приглушить шумные сторонние логгеры
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
