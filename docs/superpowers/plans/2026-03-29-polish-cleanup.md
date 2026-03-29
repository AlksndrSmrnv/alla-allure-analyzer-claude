# Polish Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить 5 мелких несоответствий в кодовой базе alla: добавить bounds-валидацию конфигурационным полям, вынести алгоритмические константы в `ClusteringConfig`, сделать лимиты LLM-промптов конфигурируемыми, убрать хардкод `size=50` в `find_launch_by_name`, удалить лишнее присвоение `kb_bypass_count=0`.

**Architecture:** Каждое изменение локально и изолировано. Нет структурных изменений — только расширение существующих `dataclass`/`Settings` полей и параметров функций с обратно-совместимыми дефолтами. Логика алгоритмов не меняется.

**Tech Stack:** Python 3.11+, Pydantic v2 (settings), pytest

---

### Task 1: Bounds-валидация для `clustering_threshold` и `logs_clustering_weight` в `config.py`

**Files:**
- Modify: `src/alla/config.py:60,96`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# В конец tests/test_config.py добавить:

import pytest
from pydantic import ValidationError

def test_clustering_threshold_rejects_out_of_range(monkeypatch, tmp_path) -> None:
    """clustering_threshold не принимает значения вне [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "tok")

    with pytest.raises(ValidationError):
        Settings(clustering_threshold=-0.1)

    with pytest.raises(ValidationError):
        Settings(clustering_threshold=1.1)


def test_logs_clustering_weight_rejects_out_of_range(monkeypatch, tmp_path) -> None:
    """logs_clustering_weight не принимает значения вне [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLURE_ENDPOINT", "https://allure.example.com")
    monkeypatch.setenv("ALLURE_TOKEN", "tok")

    with pytest.raises(ValidationError):
        Settings(logs_clustering_weight=-0.01)

    with pytest.raises(ValidationError):
        Settings(logs_clustering_weight=1.5)
```

- [ ] **Step 2: Убедиться, что тесты падают**

```
cd /path/to/worktree && python -m pytest tests/test_config.py::test_clustering_threshold_rejects_out_of_range tests/test_config.py::test_logs_clustering_weight_rejects_out_of_range -v
```

Ожидаем: FAILED (оба).

- [ ] **Step 3: Добавить `ge=0.0, le=1.0` к полям**

В `src/alla/config.py` изменить строку 60:

```python
    clustering_threshold: float = Field(
        default=0.60,
        ge=0.0, le=1.0,
        description="Порог схожести для группировки ошибок в кластеры (0.0-1.0)",
    )
```

И строки 96-99:

```python
    logs_clustering_weight: float = Field(
        default=0.15,
        ge=0.0, le=1.0,
        description="Вес лог-канала в кластеризации. Лог участвует в сравнении когда доступен; при отсутствии лога вес перераспределяется на message",
    )
```

- [ ] **Step 4: Запустить тесты**

```
python -m pytest tests/test_config.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Запустить весь suite**

```
python -m pytest -x -q
```

Ожидаем: все проходят.

- [ ] **Step 6: Коммит**

```bash
git add src/alla/config.py tests/test_config.py
git commit -m "fix: добавить ge=0.0, le=1.0 для clustering_threshold и logs_clustering_weight"
```

---

### Task 2: Перенести `_STEP_PATH_MISMATCH_PENALTY` и `_STEP_PATH_LOG_REDUCTION` в `ClusteringConfig`

**Files:**
- Modify: `src/alla/services/clustering_service.py:41-42,49-72,381-383`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_clustering_service.py` добавить в конец:

```python
def test_clustering_config_exposes_step_path_penalty_fields() -> None:
    """ClusteringConfig содержит поля step_path_mismatch_penalty и step_path_log_reduction."""
    config = ClusteringConfig()
    assert config.step_path_mismatch_penalty == 0.45
    assert config.step_path_log_reduction == 0.5

    custom = ClusteringConfig(step_path_mismatch_penalty=0.3, step_path_log_reduction=0.4)
    assert custom.step_path_mismatch_penalty == 0.3
    assert custom.step_path_log_reduction == 0.4
```

- [ ] **Step 2: Убедиться что тест падает**

```
python -m pytest tests/test_clustering_service.py::test_clustering_config_exposes_step_path_penalty_fields -v
```

Ожидаем: FAILED (`ClusteringConfig() got unexpected keyword argument` или `has no attribute`).

- [ ] **Step 3: Добавить поля в `ClusteringConfig` и убрать модульные константы**

В `src/alla/services/clustering_service.py`:

1. Удалить строки 41-42 (модульные константы):
```python
# УДАЛИТЬ эти две строки:
_STEP_PATH_MISMATCH_PENALTY = 0.45
_STEP_PATH_LOG_REDUCTION = 0.5
```

2. Добавить поля в `ClusteringConfig` (после `trace_snippet_lines: int = 5`):
```python
    step_path_mismatch_penalty: float = 0.45
    step_path_log_reduction: float = 0.5
```

3. Заменить использование в методе (строки ~381-383, теперь немного сдвинутые):
```python
# БЫЛО:
                if step_sim is not None and has_step[i] and has_step[j]:
                    step_penalty = _STEP_PATH_MISMATCH_PENALTY
                    if has_log[i] and has_log[j]:
                        step_penalty *= _STEP_PATH_LOG_REDUCTION

# СТАЛО:
                if step_sim is not None and has_step[i] and has_step[j]:
                    step_penalty = self._config.step_path_mismatch_penalty
                    if has_log[i] and has_log[j]:
                        step_penalty *= self._config.step_path_log_reduction
```

- [ ] **Step 4: Запустить тесты**

```
python -m pytest tests/test_clustering_service.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Запустить весь suite**

```
python -m pytest -x -q
```

Ожидаем: все проходят.

- [ ] **Step 6: Коммит**

```bash
git add src/alla/services/clustering_service.py tests/test_clustering_service.py
git commit -m "refactor: перенести step_path penalty-константы в ClusteringConfig"
```

---

### Task 3: Убрать хардкод `size=50` в `find_launch_by_name`

**Files:**
- Modify: `src/alla/clients/testops_client.py:54`
- Modify: `tests/test_setup_kb.py` (или добавить отдельный тест в `tests/test_triage_service.py`)

- [ ] **Step 1: Написать падающий тест**

В `tests/test_triage_service.py` найти или добавить секцию для `find_launch_by_name`. Добавить в конец файла:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from alla.clients.testops_client import AllureTestOpsClient


@pytest.mark.asyncio
async def test_find_launch_by_name_uses_configured_page_size(respx_mock) -> None:
    """find_launch_by_name использует self._page_size, а не хардкод 50."""
    from alla.config import Settings
    import respx, httpx

    settings = MagicMock(spec=Settings)
    settings.endpoint = "https://allure.example.com"
    settings.page_size = 25
    settings.max_pages = 10
    settings.request_timeout = 30
    settings.ssl_verify = True

    auth = MagicMock()
    auth.get_token = AsyncMock(return_value="tok")

    client = AllureTestOpsClient(settings, auth)

    respx_mock.get("https://allure.example.com/api/launch").mock(
        return_value=httpx.Response(
            200,
            json={"content": [{"id": 42, "name": "my-launch"}], "totalPages": 1},
        )
    )

    result = await client.find_launch_by_name("my-launch")

    assert result == 42
    request = respx_mock.calls[0].request
    assert "size=25" in str(request.url), f"Expected size=25 in URL, got: {request.url}"
    await client.close()
```

- [ ] **Step 2: Убедиться что тест падает**

```
python -m pytest tests/test_triage_service.py::test_find_launch_by_name_uses_configured_page_size -v
```

Ожидаем: FAILED (`size=50` в URL вместо `size=25`).

- [ ] **Step 3: Заменить хардкод в `testops_client.py`**

В `src/alla/clients/testops_client.py` изменить строку 54:

```python
# БЫЛО:
        params: dict[str, Any] = {"page": 0, "size": 50, "sort": "created_date,DESC"}

# СТАЛО:
        params: dict[str, Any] = {"page": 0, "size": self._page_size, "sort": "created_date,DESC"}
```

- [ ] **Step 4: Запустить тест**

```
python -m pytest tests/test_triage_service.py::test_find_launch_by_name_uses_configured_page_size -v
```

Ожидаем: PASSED.

- [ ] **Step 5: Запустить весь suite**

```
python -m pytest -x -q
```

Ожидаем: все проходят.

- [ ] **Step 6: Коммит**

```bash
git add src/alla/clients/testops_client.py tests/test_triage_service.py
git commit -m "fix: find_launch_by_name использует self._page_size вместо хардкода 50"
```

---

### Task 4: Сделать лимиты LLM-промптов конфигурируемыми

**Files:**
- Modify: `src/alla/config.py` (добавить 3 поля)
- Modify: `src/alla/services/llm_service.py` (расширить сигнатуры)
- Modify: `src/alla/orchestrator.py` (пробросить из settings)
- Modify: `tests/test_llm_service.py` (добавить тест)

Подход: добавить keyword-only параметры в `build_cluster_prompt` и `LLMService.__init__` с дефолтами = текущим константам, чтобы сохранить обратную совместимость.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_llm_service.py`:

```python
def test_build_cluster_prompt_respects_custom_limits() -> None:
    """build_cluster_prompt использует переданные message_max_chars/trace_max_chars."""
    cluster = make_failure_cluster(
        label="Timeout",
        example_message="A" * 100,
    )

    prompt_default = build_cluster_prompt(cluster)
    prompt_tight = build_cluster_prompt(cluster, message_max_chars=50)

    # При жёстком лимите длинное сообщение должно быть обрезано
    assert "...[обрезано]" in prompt_tight
    # При дефолтном лимите 100 символов не обрезается
    assert "...[обрезано]" not in prompt_default
```

- [ ] **Step 2: Убедиться что тест падает**

```
python -m pytest tests/test_llm_service.py::test_build_cluster_prompt_respects_custom_limits -v
```

Ожидаем: FAILED (`build_cluster_prompt() got an unexpected keyword argument 'message_max_chars'`).

- [ ] **Step 3: Расширить `build_cluster_prompt` — добавить keyword-only параметры**

В `src/alla/services/llm_service.py` изменить сигнатуру функции `build_cluster_prompt` (строка ~87):

```python
def build_cluster_prompt(
    cluster: FailureCluster,
    kb_matches: list[KBMatchResult] | None = None,
    log_snippet: str | None = None,
    full_trace: str | None = None,
    *,
    kb_query_provenance: tuple[int, int, int] | None = None,
    message_max_chars: int = _PROMPT_MESSAGE_MAX_CHARS,
    trace_max_chars: int = _PROMPT_TRACE_MAX_CHARS,
    log_max_chars: int = _PROMPT_LOG_MAX_CHARS,
) -> str:
```

Заменить использование констант внутри функции (строки ~201-222):

```python
# БЫЛО:
    if cluster.example_message:
        msg = _truncate_prompt_text(
            cluster.example_message,
            _PROMPT_MESSAGE_MAX_CHARS,
        )
        ...
    ...
    if trace_text:
        ...
        trace_text = _truncate_prompt_text(
            trace_text,
            _PROMPT_TRACE_MAX_CHARS,
        )
        ...
    if log_snippet:
        ...
        log_text = _truncate_prompt_text(
            log_text,
            _PROMPT_LOG_MAX_CHARS,
        )

# СТАЛО:
    if cluster.example_message:
        msg = _truncate_prompt_text(
            cluster.example_message,
            message_max_chars,
        )
        ...
    ...
    if trace_text:
        ...
        trace_text = _truncate_prompt_text(
            trace_text,
            trace_max_chars,
        )
        ...
    if log_snippet:
        ...
        log_text = _truncate_prompt_text(
            log_text,
            log_max_chars,
        )
```

- [ ] **Step 4: Запустить новый тест**

```
python -m pytest tests/test_llm_service.py::test_build_cluster_prompt_respects_custom_limits -v
```

Ожидаем: PASSED.

- [ ] **Step 5: Расширить `LLMService.__init__` и `analyze_one`**

В `src/alla/services/llm_service.py` изменить `LLMService.__init__`:

```python
    def __init__(
        self,
        langflow_client: LangflowClient,
        *,
        concurrency: int = 3,
        message_max_chars: int = _PROMPT_MESSAGE_MAX_CHARS,
        trace_max_chars: int = _PROMPT_TRACE_MAX_CHARS,
        log_max_chars: int = _PROMPT_LOG_MAX_CHARS,
    ) -> None:
        self._client = langflow_client
        self._concurrency = concurrency
        self._message_max_chars = message_max_chars
        self._trace_max_chars = trace_max_chars
        self._log_max_chars = log_max_chars
```

В `analyze_one` изменить вызов `build_cluster_prompt` (строка ~487):

```python
            prompt = build_cluster_prompt(
                cluster, kb_matches, log_snippet, full_trace,
                kb_query_provenance=provenance,
                message_max_chars=self._message_max_chars,
                trace_max_chars=self._trace_max_chars,
                log_max_chars=self._log_max_chars,
            )
```

- [ ] **Step 6: Добавить 3 поля в `config.py`**

После поля `llm_retry_base_delay` в `src/alla/config.py`:

```python
    llm_prompt_message_max_chars: int = Field(
        default=2000,
        ge=100,
        description="Макс. символов сообщения об ошибке в LLM-промпте (ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS)",
    )
    llm_prompt_trace_max_chars: int = Field(
        default=400,
        ge=50,
        description="Макс. символов стек-трейса в LLM-промпте (ALLURE_LLM_PROMPT_TRACE_MAX_CHARS)",
    )
    llm_prompt_log_max_chars: int = Field(
        default=8000,
        ge=100,
        description="Макс. символов лога в LLM-промпте (ALLURE_LLM_PROMPT_LOG_MAX_CHARS)",
    )
```

- [ ] **Step 7: Пробросить из `orchestrator.py`**

В `src/alla/orchestrator.py` изменить конструирование `LLMService` (строка ~352):

```python
        llm_service = LLMService(
            langflow,
            concurrency=settings.llm_concurrency,
            message_max_chars=settings.llm_prompt_message_max_chars,
            trace_max_chars=settings.llm_prompt_trace_max_chars,
            log_max_chars=settings.llm_prompt_log_max_chars,
        )
```

- [ ] **Step 8: Запустить весь suite**

```
python -m pytest -x -q
```

Ожидаем: все проходят.

- [ ] **Step 9: Коммит**

```bash
git add src/alla/config.py src/alla/services/llm_service.py src/alla/orchestrator.py tests/test_llm_service.py
git commit -m "feat: сделать лимиты LLM-промптов конфигурируемыми через ALLURE_LLM_PROMPT_*"
```

---

### Task 5: Убрать лишнее явное присвоение `kb_bypass_count=0`

**Files:**
- Modify: `src/alla/services/llm_service.py:533`

- [ ] **Step 1: Убрать лишнее присвоение**

В `src/alla/services/llm_service.py` строка ~533, убрать `kb_bypass_count=0,`:

```python
# БЫЛО:
        return LLMAnalysisResult(
            total_clusters=len(clustering_report.clusters),
            analyzed_count=analyzed,
            failed_count=failed,
            skipped_count=skipped,
            # Поле оставлено в API/CLI ответах для обратной совместимости.
            kb_bypass_count=0,
            cluster_analyses=analyses,
        )

# СТАЛО:
        return LLMAnalysisResult(
            total_clusters=len(clustering_report.clusters),
            analyzed_count=analyzed,
            failed_count=failed,
            skipped_count=skipped,
            cluster_analyses=analyses,
        )
```

Поле `kb_bypass_count: int = 0` остаётся в `models/llm.py` — это **намеренно** для обратной совместимости JSON API.

- [ ] **Step 2: Запустить весь suite**

```
python -m pytest -x -q
```

Ожидаем: все проходят.

- [ ] **Step 3: Коммит**

```bash
git add src/alla/services/llm_service.py
git commit -m "refactor: убрать лишнее явное присвоение kb_bypass_count=0 (дефолт уже 0)"
```

---

## Итог

После выполнения плана:
- `clustering_threshold` и `logs_clustering_weight` теперь валидируются pydantic (`ge=0.0, le=1.0`)
- `_STEP_PATH_MISMATCH_PENALTY` и `_STEP_PATH_LOG_REDUCTION` живут в `ClusteringConfig` рядом с остальными параметрами алгоритма
- Лимиты LLM-промптов конфигурируются через env vars `ALLURE_LLM_PROMPT_MESSAGE_MAX_CHARS`, `ALLURE_LLM_PROMPT_TRACE_MAX_CHARS`, `ALLURE_LLM_PROMPT_LOG_MAX_CHARS`
- `find_launch_by_name` использует `self._page_size` вместо хардкода
- Убрано лишнее явное присвоение `kb_bypass_count=0`
