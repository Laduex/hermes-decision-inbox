"""Agent-facing request schema and validation helpers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WikiExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_kind: Literal["wiki_patch_v1"]
    target_path: str = Field(min_length=1, max_length=1024)
    operation: Literal["create", "replace_section", "append"]
    base_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    section_anchor: str | None = Field(default=None, max_length=500)
    proposed_content: str = Field(min_length=1, max_length=100_000)


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    label: str = Field(min_length=1, max_length=160)
    details: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=4000)
    execution: WikiExecution | None = None


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=8000)
    url: str | None = Field(default=None, max_length=2048)


class DecisionCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    details: str = Field(min_length=1, max_length=12_000)
    recommendation: DecisionOption
    alternatives: list[DecisionOption] = Field(default_factory=list, max_length=12)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=30)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    execution: WikiExecution | None = None

    @model_validator(mode="after")
    def unique_options(self) -> "DecisionCard":
        ids = [self.recommendation.option_id, *(option.option_id for option in self.alternatives)]
        if len(ids) != len(set(ids)):
            raise ValueError("option_id values must be unique within a card")
        return self


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="decision", min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=1000)
    details: str | None = Field(default=None, max_length=12_000)
    recommendation: DecisionOption | None = None
    alternatives: list[DecisionOption] = Field(default_factory=list, max_length=12)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=30)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    batch: list[DecisionCard] | None = Field(default=None, min_length=1, max_length=100)
    execution: WikiExecution | None = None

    @model_validator(mode="after")
    def single_or_batch(self) -> "DecisionRequest":
        if self.batch:
            if any(value is not None for value in (self.title, self.summary, self.details, self.recommendation)):
                raise ValueError("batch requests cannot also define single-card fields")
            executable = [card.execution is not None for card in self.batch]
            if any(executable) and not all(executable):
                raise ValueError("executable Wiki batches cannot mix executable and ordinary cards")
        elif not all((self.title, self.summary, self.details, self.recommendation)):
            raise ValueError("single decisions require title, summary, details, and recommendation")
        elif self.recommendation:
            ids = [self.recommendation.option_id, *(option.option_id for option in self.alternatives)]
            if len(ids) != len(set(ids)):
                raise ValueError("option_id values must be unique within a card")
        return self

    def cards(self) -> list[DecisionCard]:
        if self.batch:
            return self.batch
        return [DecisionCard(
            title=self.title or "",
            summary=self.summary or "",
            details=self.details or "",
            recommendation=self.recommendation,
            alternatives=self.alternatives,
            evidence=self.evidence,
            priority=self.priority,
            execution=self.execution,
        )]


class WeeklyWikiReviewRequest(BaseModel):
    """Agent-facing shape for the weekly-only publisher.

    The service accepts both single and batch decisions, but this plugin tool is
    intentionally batch-only. Keeping single-card fields out of its schema prevents
    models from mixing the two request modes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="weekly-memory-wiki-review", min_length=1, max_length=100)
    batch: list[DecisionCard] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def executable_batch(self) -> "WeeklyWikiReviewRequest":
        if any(card.execution is None for card in self.batch):
            raise ValueError("weekly review publication requires wiki_patch_v1 execution on every card")
        return self

    def cards(self) -> list[DecisionCard]:
        return self.batch


REQUEST_DECISION_SCHEMA: dict[str, Any] = {
    "name": "publish_weekly_wiki_review",
    "description": (
        "Publish the Sunday Memory Wiki review as a batch of exact, reviewable Wiki changes. "
        "Every card must include wiki_patch_v1 execution data. Top-level arguments are only "
        "name and batch; put title, summary, details, evidence, priority, and choices inside each "
        "batch card. This tool is not for ordinary decisions."
    ),
    "parameters": WeeklyWikiReviewRequest.model_json_schema(),
}
