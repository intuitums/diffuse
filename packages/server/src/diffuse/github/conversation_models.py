"""Structured records for grounded review-thread conversations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from diffuse.repository.policy.models import validate_repo_path


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConversationReference(_StrictModel):
    file_path: Annotated[str, Field(min_length=1, max_length=1024)]
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    explanation: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("file_path")
    @classmethod
    def valid_file_path(cls, value: str) -> str:
        return validate_repo_path(value)

    def model_post_init(self, __context: object) -> None:
        if self.end_line < self.start_line:
            raise ValueError("Conversation reference end_line precedes start_line")


class ConversationResponse(_StrictModel):
    answer: Annotated[str, Field(min_length=1, max_length=6000)]
    references: list[ConversationReference] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class ConversationTurn:
    author: str
    question: str
    answer: str


@dataclass(frozen=True)
class GeneratedConversationAnswer:
    answer: str
    references: tuple[ConversationReference, ...]
    prompt_tokens: int
    completion_tokens: int
