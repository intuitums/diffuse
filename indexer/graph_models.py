"""Provider-neutral symbol graph records emitted by language adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeSymbol:
    stable_key: str
    file_path: str
    language: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    content_hash: str


@dataclass(frozen=True)
class CodeRelationship:
    source_symbol_key: str
    target_symbol_key: str | None
    target_qualified_name: str
    kind: str
    line: int | None


@dataclass(frozen=True)
class GraphDiagnostic:
    file_path: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class FileGraph:
    symbols: tuple[CodeSymbol, ...]
    relationships: tuple[CodeRelationship, ...]
    diagnostics: tuple[GraphDiagnostic, ...] = ()


@dataclass(frozen=True)
class RepositoryGraph:
    symbols: tuple[CodeSymbol, ...]
    relationships: tuple[CodeRelationship, ...]
    diagnostics: tuple[GraphDiagnostic, ...] = ()
