"""Повторы запросов к TestOps при временных сбоях: какие статусы и какая пауза."""

# Временные сбои: перегрузка (429) и балансировщик или прокси без живого бэкенда (502–504).
# 500 не повторяем: ошибка самого TestOps на этот запрос обычно воспроизводится.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_BASE_DELAY_S = 1.0
RETRY_MAX_DELAY_S = 30.0


def header(resp: object, name: str) -> object:
    """Заголовок ответа или ``None`` (у заглушек ответа в тестах заголовков может не быть)."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return None


def retry_delay(retry_after: object, attempt: int) -> float:
    """Пауза перед повтором: ``Retry-After`` в секундах, иначе 1, 2, 4… с; не больше 30 с."""
    if isinstance(retry_after, str):
        try:
            seconds = float(retry_after.strip())
        except ValueError:
            seconds = -1.0  # дата HTTP — не разбираем, берём свою паузу
        if seconds >= 0:
            return min(seconds, RETRY_MAX_DELAY_S)
    return min(RETRY_BASE_DELAY_S * (1 << attempt), RETRY_MAX_DELAY_S)
