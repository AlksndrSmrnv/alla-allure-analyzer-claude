"""Strict contract for agent-authored results; no LLM calls in Python."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1, max_length=4000)]
SummaryLine = Annotated[str, Field(min_length=1, max_length=300, pattern=r"^[^\r\n]+$")]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True
    )


class Evidence(Contract):
    source: Text
    quote: Text


class ClusterAnalysis(Contract):
    run_id: Text
    cluster_id: Text
    symptom: Text
    cause: Text
    category: Literal["продукт", "автотест", "окружение", "данные", "неизвестно"]
    confidence: Literal["низкая", "средняя", "высокая"]
    evidence: list[Evidence]
    limitations: list[Text]
    contradictions: list[Text]
    next_action: Text
    code_alignment: Literal["совпадает", "не совпадает", "неизвестно"]
    code_alignment_reason: Text


class Finding(Contract):
    text: Text
    cluster_ids: Annotated[list[Text], Field(min_length=1)]
    status: Literal["подтверждено", "гипотеза"]
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class LaunchAnalysis(Contract):
    run_id: Text
    summary_lines: Annotated[list[SummaryLine], Field(min_length=1, max_length=15)]
    priority_actions: Annotated[list[Text], Field(max_length=5)]
    findings: list[Finding]
