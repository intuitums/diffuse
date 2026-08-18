"""Strict model output for citation-grounded repository questions."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from diffuse.repository.policy.models import validate_repo_path
from diffuse.repository.scm import validate_repository_name


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CodeQueryCitation(_StrictModel):
    repository_name: Annotated[str, Field(min_length=3, max_length=512)]
    file_path: Annotated[str, Field(min_length=1, max_length=1024)]
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    explanation: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("repository_name")
    @classmethod
    def valid_repository_name(cls, value: str) -> str:
        return validate_repository_name(value)

    @field_validator("file_path")
    @classmethod
    def valid_file_path(cls, value: str) -> str:
        return validate_repo_path(value)

    @model_validator(mode="after")
    def valid_range(self) -> CodeQueryCitation:
        if self.end_line < self.start_line:
            raise ValueError("Citation end_line precedes start_line")
        return self


class CodeQueryClaim(_StrictModel):
    statement: Annotated[str, Field(min_length=1, max_length=2000)]
    citations: list[CodeQueryCitation] = Field(min_length=1, max_length=4)


class CodeQueryModelResponse(_StrictModel):
    insufficient_evidence: bool
    claims: list[CodeQueryClaim] = Field(default_factory=list, max_length=8)
