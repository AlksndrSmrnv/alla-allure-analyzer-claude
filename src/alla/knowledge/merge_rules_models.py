"""Pydantic-модели для правил объединения кластеров."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class MergeRule(BaseModel):
    """Правило объединения двух кластеров по их стабильным сигнатурам."""

    rule_id: int
    project_id: int
    signature_hash_a: str = Field(min_length=64, max_length=64)
    signature_hash_b: str = Field(min_length=64, max_length=64)
    audit_text_a: str = ""
    audit_text_b: str = ""
    launch_id: int | None = None
    created_at: datetime | None = None


class MergeRulePair(BaseModel):
    """Пара сигнатур для создания правила объединения."""

    signature_hash_a: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    signature_hash_b: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[a-f0-9]{64}$",
    )
    audit_text_a: str = Field(default="", max_length=2000)
    audit_text_b: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate_distinct_hashes(self) -> "MergeRulePair":
        if self.signature_hash_a == self.signature_hash_b:
            raise ValueError("signature_hash_a and signature_hash_b must differ")
        return self


class MergeRulesRequest(BaseModel):
    """Тело POST /api/v1/merge-rules."""

    project_id: int
    launch_id: int | None = Field(default=None, description="ID запуска (аудит)")
    pairs: list[MergeRulePair] = Field(min_length=1)


class MergeRulesResponse(BaseModel):
    """Ответ POST /api/v1/merge-rules."""

    rules: list[MergeRule]
    created_count: int
    updated_count: int


class MergeRulesListResponse(BaseModel):
    """Ответ GET /api/v1/merge-rules."""

    rules: list[MergeRule] = Field(default_factory=list)


class MergeRuleDeleteResponse(BaseModel):
    """Ответ DELETE /api/v1/merge-rules/{rule_id}."""

    rule_id: int
    deleted: bool
