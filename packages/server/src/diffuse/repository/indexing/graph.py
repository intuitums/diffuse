"""Repository graph orchestration and the native Python AST adapter."""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .chunker import iter_repository_files
from .graph_models import (
    CodeRelationship,
    CodeSymbol,
    FileGraph,
    GraphDiagnostic,
    RepositoryGraph,
)
from .tree_sitter_graph import adapter_for_path


def _stable_key(file_path: str, kind: str, qualified_name: str, occurrence: int) -> str:
    identity = f"{file_path}\0{kind}\0{qualified_name}\0{occurrence}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _module_name(file_path: str) -> str:
    path = Path(file_path)
    without_suffix = path.with_suffix("")
    parts = list(without_suffix.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value)
    return None


class _PythonGraphVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str, source: str):
        self.file_path = file_path
        self.source = source
        self.lines = source.splitlines()
        self.module_name = _module_name(file_path)
        self.symbols: list[CodeSymbol] = []
        self.relationships: list[CodeRelationship] = []
        self.scope: list[CodeSymbol] = []
        self.import_aliases: dict[str, str] = {}
        self.top_level_names: set[str] = set()
        self._identity_occurrences: Counter[tuple[str, str]] = Counter()

        module = self._make_symbol(
            kind="module",
            name=Path(file_path).stem,
            qualified_name=self.module_name,
            start_line=1,
            end_line=max(1, len(self.lines)),
            signature=None,
            docstring=None,
            content=source,
        )
        self.symbols.append(module)
        self.scope.append(module)

    @property
    def current_symbol(self) -> CodeSymbol:
        return self.scope[-1]

    def _make_symbol(
        self,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        start_line: int,
        end_line: int,
        signature: str | None,
        docstring: str | None,
        content: str,
    ) -> CodeSymbol:
        identity = (kind, qualified_name)
        self._identity_occurrences[identity] += 1
        return CodeSymbol(
            stable_key=_stable_key(
                self.file_path,
                kind,
                qualified_name,
                self._identity_occurrences[identity],
            ),
            file_path=self.file_path,
            language="python",
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            content_hash=_content_hash(content),
        )

    def _qualified_child_name(self, name: str) -> str:
        return f"{self.current_symbol.qualified_name}.{name}"

    def _source_for(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or ""

    def _add_symbol(
        self,
        node: ast.AST,
        *,
        name: str,
        kind: str,
        signature: str | None = None,
        docstring: str | None = None,
    ) -> CodeSymbol:
        start_line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", start_line)
        symbol = self._make_symbol(
            kind=kind,
            name=name,
            qualified_name=self._qualified_child_name(name),
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            docstring=docstring,
            content=self._source_for(node),
        )
        self.symbols.append(symbol)
        self.relationships.append(
            CodeRelationship(
                source_symbol_key=self.current_symbol.stable_key,
                target_symbol_key=symbol.stable_key,
                target_qualified_name=symbol.qualified_name,
                kind="contains",
                line=start_line,
            )
        )
        if len(self.scope) == 1:
            self.top_level_names.add(name)
        return symbol

    def _resolve_reference(self, name: str) -> str:
        if not name:
            return name

        first, separator, remainder = name.partition(".")
        if first in self.import_aliases:
            mapped = self.import_aliases[first]
            return mapped + (separator + remainder if separator else "")

        if name.startswith("self."):
            class_symbol = next(
                (symbol for symbol in reversed(self.scope) if symbol.kind == "class"),
                None,
            )
            if class_symbol:
                return f"{class_symbol.qualified_name}.{name.removeprefix('self.')}"

        if "." not in name and name in self.top_level_names:
            return f"{self.module_name}.{name}"
        return name

    def _add_relationship(
        self,
        *,
        target_name: str,
        kind: str,
        line: int | None,
    ) -> None:
        self.relationships.append(
            CodeRelationship(
                source_symbol_key=self.current_symbol.stable_key,
                target_symbol_key=None,
                target_qualified_name=self._resolve_reference(target_name),
                kind=kind,
                line=line,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            if len(self.scope) == 1:
                self.import_aliases[local_name] = (
                    alias.name if alias.asname else local_name
                )
            self._add_relationship(
                target_name=alias.name,
                kind="imports",
                line=node.lineno,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            package_parts = self.module_name.split(".")
            if Path(self.file_path).name != "__init__.py":
                package_parts.pop()
            levels_up = node.level - 1
            if levels_up:
                package_parts = package_parts[:-levels_up]
            if node.module:
                package_parts.extend(node.module.split("."))
            prefix = ".".join(package_parts)
        else:
            prefix = node.module or ""

        for alias in node.names:
            qualified_name = ".".join(part for part in (prefix, alias.name) if part)
            local_name = alias.asname or alias.name
            if len(self.scope) == 1:
                self.import_aliases[local_name] = qualified_name
            self._add_relationship(
                target_name=qualified_name,
                kind="imports",
                line=node.lineno,
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self._add_symbol(
            node,
            name=node.name,
            kind="class",
            signature=f"class {node.name}",
            docstring=ast.get_docstring(node, clean=False),
        )
        self.scope.append(symbol)
        for base in node.bases:
            base_name = _expression_name(base)
            if base_name:
                self._add_relationship(
                    target_name=base_name,
                    kind="inherits",
                    line=node.lineno,
                )
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        kind = "method" if self.current_symbol.kind == "class" else "function"
        prefix = "async def" if is_async else "def"
        symbol = self._add_symbol(
            node,
            name=node.name,
            kind=kind,
            signature=f"{prefix} {node.name}({ast.unparse(node.args)})",
            docstring=ast.get_docstring(node, clean=False),
        )
        self.scope.append(symbol)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _add_variable(self, node: ast.AST, name: str) -> None:
        if self.current_symbol.kind not in {"module", "class"}:
            return
        self._add_symbol(
            node,
            name=name,
            kind="variable",
            signature=None,
            docstring=None,
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._add_variable(node, target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._add_variable(node, node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target_name = _expression_name(node.func)
        if target_name:
            self._add_relationship(
                target_name=target_name,
                kind="calls",
                line=getattr(node, "lineno", None),
            )
        self.generic_visit(node)


def extract_python_file_graph(path: Path, repo_root: Path) -> FileGraph:
    file_path = path.relative_to(repo_root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
    except (OSError, UnicodeError, SyntaxError) as error:
        return FileGraph(
            symbols=(),
            relationships=(),
            diagnostics=(
                GraphDiagnostic(
                    file_path=file_path,
                    message=str(error),
                    line=getattr(error, "lineno", None),
                ),
            ),
        )

    visitor = _PythonGraphVisitor(file_path, source)
    visitor.top_level_names.update(
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    )
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            visitor.top_level_names.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
    visitor.visit(tree)
    return FileGraph(
        symbols=tuple(visitor.symbols),
        relationships=tuple(visitor.relationships),
    )


def extract_file_graph(path: Path, repo_root: Path) -> FileGraph:
    try:
        if path.suffix.lower() == ".py":
            return extract_python_file_graph(path, repo_root)
        if adapter := adapter_for_path(path):
            return adapter.extract(path, repo_root)
    except RecursionError as error:
        # Deeply nested untrusted source exhausts the interpreter stack inside the
        # recursive parsers and visitors; one hostile file must not end an index run.
        return FileGraph(
            symbols=(),
            relationships=(),
            diagnostics=(
                GraphDiagnostic(
                    file_path=path.relative_to(repo_root).as_posix(),
                    message=f"file skipped: {error}",
                ),
            ),
        )
    return FileGraph(symbols=(), relationships=())


def extract_repository_graph(repo_root: Path) -> RepositoryGraph:
    symbols: list[CodeSymbol] = []
    relationships: list[CodeRelationship] = []
    diagnostics: list[GraphDiagnostic] = []

    for path in iter_repository_files(repo_root):
        file_graph = extract_file_graph(path, repo_root)
        symbols.extend(file_graph.symbols)
        relationships.extend(file_graph.relationships)
        diagnostics.extend(file_graph.diagnostics)

    symbols_by_qualified_name: dict[str, list[CodeSymbol]] = {}
    symbols_by_name: dict[str, list[CodeSymbol]] = {}
    symbols_by_key: dict[str, CodeSymbol] = {}
    for symbol in symbols:
        symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)
        symbols_by_name.setdefault(symbol.name, []).append(symbol)
        symbols_by_key[symbol.stable_key] = symbol

    linked_relationships: list[CodeRelationship] = []
    for relationship in relationships:
        if relationship.target_symbol_key is not None:
            linked_relationships.append(relationship)
            continue
        target = relationship.target_qualified_name
        matches = symbols_by_qualified_name.get(target, [])
        if not matches:
            suffix = f".{target}"
            matches = [
                symbol
                for symbol in symbols
                if symbol.qualified_name.endswith(suffix)
            ]
        if not matches and "." not in target:
            matches = symbols_by_name.get(target, [])
        source = symbols_by_key.get(relationship.source_symbol_key)
        same_language_matches = (
            [symbol for symbol in matches if symbol.language == source.language]
            if source
            else []
        )
        if len(same_language_matches) == 1:
            matches = same_language_matches
        linked_relationships.append(
            replace(
                relationship,
                target_symbol_key=matches[0].stable_key if len(matches) == 1 else None,
            )
        )

    return RepositoryGraph(
        symbols=tuple(symbols),
        relationships=tuple(linked_relationships),
        diagnostics=tuple(diagnostics),
    )
