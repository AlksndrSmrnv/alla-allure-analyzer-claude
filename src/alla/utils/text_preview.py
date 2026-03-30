"""Утилиты для компактных однострочных превью в отладочных логах."""

def preview_head(text: str, max_chars: int) -> str:
    """Возвращает однострочное превью с начала текста."""
    return text[:max_chars].replace("\n", " ")


def preview_tail(text: str, max_chars: int) -> str:
    """Возвращает однострочное превью с конца текста."""
    if len(text) <= max_chars:
        return text.replace("\n", " ")
    return text[-max_chars:].replace("\n", " ")
