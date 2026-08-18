"""Versioned local Tree-sitter language adapters for Diffuse's code graph."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_php
import tree_sitter_ruby
import tree_sitter_rust
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from .graph_models import (
    CodeRelationship,
    CodeSymbol,
    FileGraph,
    GraphDiagnostic,
)
from .index_version import LANGUAGE_ADAPTER_SCHEMA_VERSION

TREE_SITTER_ADAPTER_VERSION = LANGUAGE_ADAPTER_SCHEMA_VERSION
MAX_SIGNATURE_CHARS = 500
MAX_DOCSTRING_CHARS = 4000


class LanguageAdapter(Protocol):
    language: str
    extensions: frozenset[str]
    version: str

    def extract(self, path: Path, repo_root: Path) -> FileGraph: ...


@dataclass(frozen=True)
class _Profile:
    language: str
    extensions: frozenset[str]
    language_factory: Callable[[], object]


@dataclass(frozen=True)
class _Definition:
    name: str
    kind: str
    child_prefix: str | None = None
    qualified_name: str | None = None


@dataclass(frozen=True)
class _Scope:
    symbol: CodeSymbol
    child_prefix: str


IDENTIFIER_TYPES = frozenset(
    {
        "constant",
        "destructor_name",
        "field_identifier",
        "identifier",
        "name",
        "namespace_identifier",
        "operator_name",
        "package_identifier",
        "property_identifier",
        "type_identifier",
    }
)
COMMENT_TYPES = frozenset(
    {
        "block_comment",
        "comment",
        "doc_comment",
        "line_comment",
    }
)
FUNCTION_VALUE_TYPES = frozenset(
    {
        "arrow_function",
        "function_expression",
        "generator_function",
    }
)
TYPESCRIPT_BASE_TYPE_NODES = frozenset(
    {
        "identifier",
        "member_expression",
        "nested_identifier",
        "nested_type_identifier",
        "type_identifier",
    }
)
# Generic base types wrap the base name one level down; unwrap to drop the type arguments.
TYPESCRIPT_GENERIC_BASE_FIELDS = {
    "generic_type": "name",
    "instantiation_expression": "function",
}

DIRECT_DEFINITIONS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "javascript": {
        "class_declaration": ("class", ("name",)),
        "field_definition": ("variable", ("property", "name")),
        "function_declaration": ("function", ("name",)),
        "generator_function_declaration": ("function", ("name",)),
        "method_definition": ("method", ("name",)),
    },
    "typescript": {
        "abstract_class_declaration": ("class", ("name",)),
        "class_declaration": ("class", ("name",)),
        "enum_declaration": ("enum", ("name",)),
        "function_declaration": ("function", ("name",)),
        "generator_function_declaration": ("function", ("name",)),
        "interface_declaration": ("interface", ("name",)),
        "method_definition": ("method", ("name",)),
        "method_signature": ("method", ("name",)),
        "public_field_definition": ("variable", ("name",)),
        "property_signature": ("variable", ("name",)),
        "type_alias_declaration": ("type", ("name",)),
    },
    "go": {
        "function_declaration": ("function", ("name",)),
        "method_declaration": ("method", ("name",)),
    },
    "java": {
        "annotation_type_declaration": ("interface", ("name",)),
        "class_declaration": ("class", ("name",)),
        "compact_constructor_declaration": ("method", ("name",)),
        "constructor_declaration": ("method", ("name",)),
        "enum_declaration": ("enum", ("name",)),
        "interface_declaration": ("interface", ("name",)),
        "method_declaration": ("method", ("name",)),
        "record_declaration": ("class", ("name",)),
    },
    "ruby": {
        "class": ("class", ("name",)),
        "method": ("method", ("name",)),
        "module": ("namespace", ("name",)),
        "singleton_method": ("method", ("name",)),
    },
    "rust": {
        "const_item": ("variable", ("name",)),
        "enum_item": ("enum", ("name",)),
        "function_item": ("function", ("name",)),
        "function_signature_item": ("method", ("name",)),
        "field_declaration": ("variable", ("name",)),
        "mod_item": ("namespace", ("name",)),
        "static_item": ("variable", ("name",)),
        "struct_item": ("class", ("name",)),
        "trait_item": ("interface", ("name",)),
        "type_item": ("type", ("name",)),
        "union_item": ("class", ("name",)),
    },
    "php": {
        "class_declaration": ("class", ("name",)),
        "enum_declaration": ("enum", ("name",)),
        "function_definition": ("function", ("name",)),
        "interface_declaration": ("interface", ("name",)),
        "method_declaration": ("method", ("name",)),
        "trait_declaration": ("trait", ("name",)),
    },
    "c": {
        "enum_specifier": ("enum", ("name",)),
        "struct_specifier": ("class", ("name",)),
        "union_specifier": ("class", ("name",)),
    },
    "cpp": {
        "class_specifier": ("class", ("name",)),
        "concept_definition": ("type", ("name",)),
        "enum_specifier": ("enum", ("name",)),
        "namespace_definition": ("namespace", ("name",)),
        "struct_specifier": ("class", ("name",)),
        "union_specifier": ("class", ("name",)),
    },
}


def _stable_key(file_path: str, kind: str, qualified_name: str, occurrence: int) -> str:
    identity = f"{file_path}\0{kind}\0{qualified_name}\0{occurrence}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _module_name(file_path: str) -> str:
    path = PurePosixPath(file_path)
    without_suffix = path.with_suffix("")
    return ".".join(without_suffix.parts) or path.stem


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _field_text(source: bytes, node: Node, *field_names: str) -> str:
    for field_name in field_names:
        value = node.child_by_field_name(field_name)
        if value is not None:
            text = _text(source, value).strip()
            if text:
                return text
    return ""


def _first_descendant(node: Node, types: frozenset[str]) -> Node | None:
    if node.type in types:
        return node
    for child in node.named_children:
        if match := _first_descendant(child, types):
            return match
    return None


def _descendants(node: Node, *types: str) -> list[Node]:
    accepted = frozenset(types)
    return [candidate for candidate in _walk(node) if candidate.type in accepted]


def _typescript_heritage(node: Node) -> list[tuple[str, Node]]:
    """Yield ``(kind, base_type_node)`` pairs for a TypeScript type declaration.

    The TypeScript grammar spells the two heritage forms differently:

    * ``interface A extends B, C`` hangs an ``extends_type_clause`` directly off the
      declaration, and its named children are already the base types.
    * ``class A extends B implements C`` hangs a ``class_heritage`` off the declaration,
      which in turn wraps an ``extends_clause`` and/or an ``implements_clause``. The base
      types live one level further down, so the clause node itself must never be handed to
      the resolver -- its text still contains the ``extends``/``implements`` keyword.

    Only direct children are inspected so that a class nested inside a method body does
    not leak its own heritage onto the enclosing declaration.

    Base types are restricted to name-shaped nodes and stripped of type arguments, so
    ``class A extends B<T>`` and ``interface A extends B<T>`` both resolve to ``B`` while
    ``class A extends ns.B`` resolves to ``ns.B``. Mixin call expressions such as ``class A
    extends mixin(Base)`` name no single base type and are deliberately left unresolved
    rather than emitting a target that can never match a symbol.
    """
    results: list[tuple[str, Node]] = []

    def collect(kind: str, clause: Node) -> None:
        for child in clause.named_children:
            if child.type in TYPESCRIPT_GENERIC_BASE_FIELDS:
                child = child.child_by_field_name(TYPESCRIPT_GENERIC_BASE_FIELDS[child.type])
            if child is not None and child.type in TYPESCRIPT_BASE_TYPE_NODES:
                results.append((kind, child))

    for clause in node.named_children:
        if clause.type == "extends_type_clause":
            collect("inherits", clause)
        elif clause.type == "class_heritage":
            for inner in clause.named_children:
                if inner.type == "extends_clause":
                    collect("inherits", inner)
                elif inner.type == "implements_clause":
                    collect("implements", inner)
    return results


def _declarator_name(source: bytes, node: Node) -> str:
    current = node.child_by_field_name("declarator")
    if current is not None:
        return _declarator_name(source, current)
    direct = _field_text(source, node, "name")
    if direct:
        return direct
    identifier = _first_descendant(node, IDENTIFIER_TYPES)
    return _text(source, identifier).strip()


def _clean_name(value: str) -> str:
    return value.strip().lstrip("$").replace("::", ".").replace("\\", ".")


def _normalize_reference(value: str) -> str:
    normalized = value.strip().strip("\"'`<>")
    normalized = normalized.replace("::", ".").replace("\\", ".")
    normalized = normalized.replace("->", ".")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"<[^<>]*>", "", normalized)
    return normalized.strip(".")


def _node_end_line(node: Node) -> int:
    row, column = node.end_point
    if row > node.start_point.row and column == 0:
        return row
    return row + 1


def _strip_comment(value: str) -> str:
    lines = value.strip().splitlines()
    cleaned: list[str] = []
    for line in lines:
        line = re.sub(r"^\s*(?://[/!]?|#|/\*+|\*+|<!--)\s?", "", line)
        line = re.sub(r"\s*(?:\*/|-->)\s*$", "", line)
        cleaned.append(line)
    return "\n".join(cleaned).strip()[:MAX_DOCSTRING_CHARS]


def _leading_doc(source: bytes, node: Node) -> str | None:
    comments: list[str] = []
    previous = node.prev_named_sibling
    expected_row = node.start_point.row
    while previous is not None and previous.type in COMMENT_TYPES:
        if previous.end_point.row + 1 < expected_row:
            break
        comments.append(_text(source, previous))
        expected_row = previous.start_point.row
        previous = previous.prev_named_sibling
    value = _strip_comment("\n".join(reversed(comments)))
    return value or None


def _signature(source: bytes, node: Node) -> str | None:
    value = _text(source, node).strip()
    if not value:
        return None
    header = value.split("{", 1)[0].strip()
    if node.type in {"method", "module"}:
        header = header.splitlines()[0].strip()
    return header[:MAX_SIGNATURE_CHARS] or None


def _relative_module(file_path: str, imported: str) -> str:
    imported = imported.strip().strip("\"'`")
    if not imported.startswith("."):
        return _normalize_reference(imported.replace("/", "."))
    current = PurePosixPath(file_path).parent
    candidate = current / imported
    parts: list[str] = []
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return _module_name(PurePosixPath(*parts).as_posix())


class _TreeSitterExtractor:
    def __init__(self, profile: _Profile, file_path: str, source: bytes):
        self.profile = profile
        self.file_path = file_path
        self.source = source
        self.source_text = source.decode("utf-8", errors="replace")
        self.symbols: list[CodeSymbol] = []
        self.relationships: list[CodeRelationship] = []
        self.aliases: dict[str, str] = {}
        self._identity_occurrences: Counter[tuple[str, str]] = Counter()
        self.parser = Parser(Language(profile.language_factory()))
        self.tree = self.parser.parse(source)
        module_qualified_name, child_prefix = self._module_context()
        module = self._make_symbol(
            kind="module",
            name=Path(file_path).stem,
            qualified_name=module_qualified_name,
            node=self.tree.root_node,
            signature=None,
            docstring=None,
        )
        self.scope = [_Scope(module, child_prefix)]
        self.symbols.append(module)
        self._collect_aliases()

    @property
    def current_scope(self) -> _Scope:
        return self.scope[-1]

    def _module_context(self) -> tuple[str, str]:
        module = _module_name(self.file_path)
        language = self.profile.language
        root = self.tree.root_node
        if language == "java":
            package = next(
                (node for node in root.named_children if node.type == "package_declaration"),
                None,
            )
            if package:
                name = _normalize_reference(
                    re.sub(r"^\s*package\s+|\s*;\s*$", "", _text(self.source, package))
                )
                if name:
                    return f"{name}.__file__.{Path(self.file_path).stem}", name
        elif language == "go":
            package = next(
                (node for node in root.named_children if node.type == "package_clause"),
                None,
            )
            if package:
                name = _clean_name(
                    _text(
                        self.source,
                        _first_descendant(package, frozenset({"package_identifier"})),
                    )
                )
                if name:
                    return f"{name}.__file__.{Path(self.file_path).stem}", name
        elif language == "php":
            namespace = next(
                (node for node in root.named_children if node.type == "namespace_definition"),
                None,
            )
            if namespace:
                name = _normalize_reference(_field_text(self.source, namespace, "name"))
                if name:
                    return f"{name}.__file__.{Path(self.file_path).stem}", name
        elif language in {"cpp", "ruby"}:
            return f"__file__.{module}", module
        return module, module

    def _make_symbol(
        self,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        node: Node,
        signature: str | None,
        docstring: str | None,
    ) -> CodeSymbol:
        identity = (kind, qualified_name)
        self._identity_occurrences[identity] += 1
        content = _text(self.source, node)
        return CodeSymbol(
            stable_key=_stable_key(
                self.file_path,
                kind,
                qualified_name,
                self._identity_occurrences[identity],
            ),
            file_path=self.file_path,
            language=self.profile.language,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=node.start_point.row + 1,
            end_line=max(node.start_point.row + 1, _node_end_line(node)),
            signature=signature,
            docstring=docstring,
            content_hash=_content_hash(content),
        )

    def _definition(self, node: Node) -> _Definition | None:
        language = self.profile.language
        if language in {"javascript", "typescript"} and node.type == "variable_declarator":
            name = _clean_name(_field_text(self.source, node, "name"))
            value = node.child_by_field_name("value")
            if name and value is not None and value.type in FUNCTION_VALUE_TYPES:
                return _Definition(name=name, kind="function")
            if name and self.current_scope.symbol.kind in {"module", "class", "namespace"}:
                return _Definition(name=name, kind="variable")
            return None
        if language == "go" and node.type == "type_spec":
            name = _clean_name(_field_text(self.source, node, "name"))
            value = node.child_by_field_name("type")
            kind = "interface" if value is not None and value.type == "interface_type" else "class"
            return _Definition(name=name, kind=kind) if name else None
        if language == "go" and node.type == "field_declaration":
            if self.current_scope.symbol.kind not in {"class", "interface"}:
                return None
            name = _clean_name(_field_text(self.source, node, "name"))
            return _Definition(name=name, kind="variable") if name else None
        if language == "java" and node.type == "variable_declarator":
            if self.current_scope.symbol.kind not in {"class", "enum", "interface"}:
                return None
            name = _clean_name(_field_text(self.source, node, "name"))
            return _Definition(name=name, kind="variable") if name else None
        if language == "rust" and node.type == "impl_item":
            target = _clean_name(_field_text(self.source, node, "type"))
            trait = _clean_name(_field_text(self.source, node, "trait"))
            if not target:
                return None
            label = f"impl {trait} for {target}" if trait else f"impl {target}"
            prefix = f"{self.current_scope.child_prefix}.{target}"
            return _Definition(
                name=label,
                kind="implementation",
                child_prefix=prefix,
                qualified_name=f"{prefix}.__impl__{trait or 'inherent'}",
            )
        if language in {"c", "cpp"} and node.type == "function_definition":
            name = _clean_name(_declarator_name(self.source, node))
            kind = (
                "method"
                if self.current_scope.symbol.kind in {"class", "implementation"}
                else "function"
            )
            return _Definition(name=name, kind=kind) if name else None
        if language in {"c", "cpp"} and node.type == "type_definition":
            declared_type = node.child_by_field_name("type")
            if declared_type is not None and declared_type.type in {
                "enum_specifier",
                "struct_specifier",
                "union_specifier",
            }:
                return None
            declarator = node.child_by_field_name("declarator")
            name = _clean_name(_text(self.source, declarator))
            return _Definition(name=name, kind="type") if name else None
        if language in {"c", "cpp"} and node.type == "field_declaration":
            if self.current_scope.symbol.kind != "class":
                return None
            name = _clean_name(_declarator_name(self.source, node))
            return _Definition(name=name, kind="variable") if name else None
        if language == "php" and node.type == "property_element":
            if self.current_scope.symbol.kind not in {"class", "trait"}:
                return None
            name = _clean_name(_field_text(self.source, node, "name"))
            return _Definition(name=name, kind="variable") if name else None

        rule = DIRECT_DEFINITIONS.get(language, {}).get(node.type)
        if not rule:
            return None
        kind, fields = rule
        name = _clean_name(_field_text(self.source, node, *fields))
        if not name:
            name = _clean_name(_declarator_name(self.source, node))
        if not name:
            return None

        if language == "go" and node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            receiver_type = ""
            if receiver is not None:
                type_node = _first_descendant(receiver, frozenset({"type_identifier"}))
                receiver_type = _clean_name(_text(self.source, type_node))
            if receiver_type:
                return _Definition(
                    name=name,
                    kind=kind,
                    child_prefix=f"{self.current_scope.child_prefix}.{receiver_type}.{name}",
                )
        if (
            language == "rust"
            and node.type == "function_item"
            and self.current_scope.symbol.kind == "implementation"
        ):
            kind = "method"
        return _Definition(name=name, kind=kind)

    def _definition_qualified_name(self, definition: _Definition, node: Node) -> str:
        if definition.qualified_name:
            return definition.qualified_name
        if self.profile.language == "cpp" and node.type == "namespace_definition":
            if self.current_scope.symbol.kind == "namespace":
                return f"{self.current_scope.child_prefix}.{definition.name}"
            return definition.name
        if self.profile.language == "ruby" and node.type in {"class", "module"}:
            if self.current_scope.symbol.kind == "module":
                return definition.name
            return f"{self.current_scope.child_prefix}.{definition.name}"
        if self.profile.language == "go" and node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            receiver_type = (
                _clean_name(
                    _text(
                        self.source,
                        _first_descendant(receiver, frozenset({"type_identifier"})),
                    )
                )
                if receiver is not None
                else ""
            )
            if receiver_type:
                return f"{self.current_scope.child_prefix}.{receiver_type}.{definition.name}"
        if self.profile.language == "rust" and self.current_scope.symbol.kind == "implementation":
            return f"{self.current_scope.child_prefix}.{definition.name}"
        return f"{self.current_scope.child_prefix}.{definition.name}"

    def _collect_aliases(self) -> None:
        for node in _walk(self.tree.root_node):
            language = self.profile.language
            if language in {"javascript", "typescript"} and node.type == "import_statement":
                source_node = node.child_by_field_name("source")
                imported_module = _relative_module(self.file_path, _text(self.source, source_node))
                for specifier in _descendants(node, "import_specifier"):
                    imported_name = _clean_name(_field_text(self.source, specifier, "name"))
                    local_name = _clean_name(
                        _field_text(self.source, specifier, "alias") or imported_name
                    )
                    if local_name and imported_name:
                        self.aliases[local_name] = f"{imported_module}.{imported_name}"
                clause = next(
                    (child for child in node.named_children if child.type == "import_clause"),
                    None,
                )
                if clause:
                    for child in clause.named_children:
                        if child.type == "identifier":
                            self.aliases[_text(self.source, child)] = f"{imported_module}.default"
                        elif child.type == "namespace_import":
                            alias = _first_descendant(child, frozenset({"identifier"}))
                            if alias:
                                self.aliases[_text(self.source, alias)] = imported_module
            elif language == "go" and node.type == "import_spec":
                path_node = node.child_by_field_name("path")
                imported = _normalize_reference(_text(self.source, path_node).replace("/", "."))
                alias_node = node.child_by_field_name("name")
                alias = _clean_name(_text(self.source, alias_node)) if alias_node else ""
                if not alias and imported:
                    alias = imported.rsplit(".", 1)[-1]
                if alias and alias not in {"_", "."}:
                    self.aliases[alias] = imported
            elif language == "java" and node.type == "import_declaration":
                imported = _normalize_reference(
                    re.sub(
                        r"^\s*import\s+(?:static\s+)?|\s*;\s*$",
                        "",
                        _text(self.source, node),
                    )
                )
                if imported:
                    self.aliases[imported.rsplit(".", 1)[-1]] = imported
            elif language == "php" and node.type == "namespace_use_clause":
                qualified = next(
                    (
                        candidate
                        for candidate in node.named_children
                        if candidate.type == "qualified_name"
                    ),
                    None,
                )
                imported = _normalize_reference(_text(self.source, qualified))
                alias = _clean_name(_field_text(self.source, node, "alias"))
                alias = alias or imported.rsplit(".", 1)[-1]
                if alias:
                    self.aliases[alias] = imported
            elif language == "rust" and node.type == "use_declaration":
                for alias, imported in self._rust_use_bindings(node):
                    if alias:
                        self.aliases[alias] = imported

    def _rust_use_bindings(self, node: Node) -> list[tuple[str, str]]:
        argument = node.child_by_field_name("argument")
        if argument is None:
            return []
        if argument.type == "use_as_clause":
            imported = _normalize_reference(_field_text(self.source, argument, "path"))
            alias = _clean_name(_field_text(self.source, argument, "alias"))
            return [(alias, imported)] if imported else []
        if argument.type == "scoped_use_list":
            prefix = _normalize_reference(_field_text(self.source, argument, "path"))
            use_list = argument.child_by_field_name("list")
            bindings: list[tuple[str, str]] = []
            if use_list is None:
                return bindings
            for child in use_list.named_children:
                if child.type == "use_as_clause":
                    name = _normalize_reference(_field_text(self.source, child, "path"))
                    alias = _clean_name(_field_text(self.source, child, "alias"))
                else:
                    name = _normalize_reference(_text(self.source, child))
                    alias = name.rsplit(".", 1)[-1]
                imported = ".".join(part for part in (prefix, name) if part)
                if imported and alias:
                    bindings.append((alias, imported))
            return bindings
        imported = _normalize_reference(_text(self.source, argument))
        return [(imported.rsplit(".", 1)[-1], imported)] if imported else []

    def _resolve_reference(self, value: str) -> str:
        target = _normalize_reference(value)
        if not target:
            return ""
        first, separator, remainder = target.partition(".")
        if first in self.aliases:
            mapped = self.aliases[first]
            return mapped + (separator + remainder if separator else "")
        if first in {"self", "this"}:
            class_scope = next(
                (
                    scope
                    for scope in reversed(self.scope)
                    if scope.symbol.kind in {"class", "implementation", "trait"}
                ),
                None,
            )
            if class_scope and remainder:
                return f"{class_scope.child_prefix}.{remainder}"
        return target

    def _imports(self, node: Node) -> list[str]:
        language = self.profile.language
        if language in {"javascript", "typescript"} and node.type == "import_statement":
            return [_relative_module(self.file_path, _field_text(self.source, node, "source"))]
        if language == "go" and node.type == "import_spec":
            return [
                _normalize_reference(_field_text(self.source, node, "path").replace("/", "."))
            ]
        if language == "java" and node.type == "import_declaration":
            return [
                _normalize_reference(
                    re.sub(
                        r"^\s*import\s+(?:static\s+)?|\s*;\s*$",
                        "",
                        _text(self.source, node),
                    )
                )
            ]
        if language == "ruby" and node.type == "call":
            method = _field_text(self.source, node, "method")
            if method in {"require", "require_relative"}:
                string = _first_descendant(node, frozenset({"string_content"}))
                return [_normalize_reference(_text(self.source, string).replace("/", "."))]
        if language == "rust" and node.type == "use_declaration":
            return [target for _alias, target in self._rust_use_bindings(node)]
        if language == "php" and node.type == "namespace_use_clause":
            qualified = next(
                (
                    candidate
                    for candidate in node.named_children
                    if candidate.type == "qualified_name"
                ),
                None,
            )
            return [_normalize_reference(_text(self.source, qualified))]
        if language in {"c", "cpp"} and node.type == "preproc_include":
            return [_normalize_reference(_field_text(self.source, node, "path"))]
        return []

    def _calls(self, node: Node) -> list[str]:
        language = self.profile.language
        if language in {"javascript", "typescript", "go", "rust", "c", "cpp"}:
            if node.type == "call_expression":
                return [self._resolve_reference(_field_text(self.source, node, "function"))]
            if language == "rust" and node.type == "macro_invocation":
                return [self._resolve_reference(_field_text(self.source, node, "macro"))]
        elif language == "java" and node.type == "method_invocation":
            object_name = _field_text(self.source, node, "object")
            method_name = _field_text(self.source, node, "name")
            return [
                self._resolve_reference(
                    f"{object_name}.{method_name}" if object_name else method_name
                )
            ]
        elif language == "ruby" and node.type == "call":
            method_name = _field_text(self.source, node, "method")
            if method_name in {"require", "require_relative"}:
                return []
            receiver = _field_text(self.source, node, "receiver")
            return [
                self._resolve_reference(
                    f"{receiver}.{method_name}" if receiver else method_name
                )
            ]
        elif language == "php":
            if node.type == "function_call_expression":
                return [self._resolve_reference(_field_text(self.source, node, "function"))]
            if node.type == "member_call_expression":
                return [
                    self._resolve_reference(
                        f"{_field_text(self.source, node, 'object')}."
                        f"{_field_text(self.source, node, 'name')}"
                    )
                ]
            if node.type == "scoped_call_expression":
                return [
                    self._resolve_reference(
                        f"{_field_text(self.source, node, 'scope')}."
                        f"{_field_text(self.source, node, 'name')}"
                    )
                ]
        return []

    def _inheritances(self, node: Node) -> list[tuple[str, str]]:
        language = self.profile.language
        results: list[tuple[str, str]] = []
        if language == "javascript" and node.type == "class_declaration":
            for heritage in _descendants(node, "class_heritage"):
                for child in heritage.named_children:
                    results.append(("inherits", self._resolve_reference(_text(self.source, child))))
        elif language == "typescript" and node.type in {
            "abstract_class_declaration",
            "class_declaration",
            "interface_declaration",
        }:
            for kind, base in _typescript_heritage(node):
                results.append((kind, self._resolve_reference(_text(self.source, base))))
        elif language == "java" and node.type in {
            "annotation_type_declaration",
            "class_declaration",
            "enum_declaration",
            "interface_declaration",
            "record_declaration",
        }:
            superclass = node.child_by_field_name("superclass")
            if superclass:
                for child in superclass.named_children:
                    results.append(("inherits", self._resolve_reference(_text(self.source, child))))
            interfaces = node.child_by_field_name("interfaces")
            if interfaces:
                for candidate in _walk(interfaces):
                    if candidate.type in {"type_identifier", "scoped_type_identifier"}:
                        results.append(
                            ("implements", self._resolve_reference(_text(self.source, candidate)))
                        )
        elif language == "ruby" and node.type == "class":
            superclass = node.child_by_field_name("superclass")
            if superclass:
                results.append(
                    ("inherits", self._resolve_reference(_text(self.source, superclass)))
                )
        elif language == "rust" and node.type == "impl_item":
            trait = node.child_by_field_name("trait")
            if trait:
                results.append(("implements", self._resolve_reference(_text(self.source, trait))))
        elif language == "php" and node.type in {
            "class_declaration",
            "interface_declaration",
        }:
            for clause in _descendants(node, "base_clause"):
                for child in clause.named_children:
                    results.append(("inherits", self._resolve_reference(_text(self.source, child))))
            for clause in _descendants(node, "class_interface_clause"):
                for child in clause.named_children:
                    results.append(
                        ("implements", self._resolve_reference(_text(self.source, child)))
                    )
        elif language == "cpp" and node.type in {
            "class_specifier",
            "struct_specifier",
        }:
            for clause in _descendants(node, "base_class_clause"):
                for child in clause.named_children:
                    if child.type != "access_specifier":
                        results.append(
                            ("inherits", self._resolve_reference(_text(self.source, child)))
                        )
        return [(kind, target) for kind, target in results if target]

    def _add_relationship(self, target: str, kind: str, line: int | None) -> None:
        if not target:
            return
        self.relationships.append(
            CodeRelationship(
                source_symbol_key=self.current_scope.symbol.stable_key,
                target_symbol_key=None,
                target_qualified_name=target,
                kind=kind,
                line=line,
            )
        )

    def _visit(self, node: Node) -> None:
        definition = self._definition(node)
        pushed = False
        if definition:
            qualified_name = self._definition_qualified_name(definition, node)
            symbol = self._make_symbol(
                kind=definition.kind,
                name=definition.name,
                qualified_name=qualified_name,
                node=node,
                signature=_signature(self.source, node),
                docstring=_leading_doc(self.source, node),
            )
            self.symbols.append(symbol)
            self.relationships.append(
                CodeRelationship(
                    source_symbol_key=self.current_scope.symbol.stable_key,
                    target_symbol_key=symbol.stable_key,
                    target_qualified_name=symbol.qualified_name,
                    kind="contains",
                    line=symbol.start_line,
                )
            )
            child_prefix = definition.child_prefix or qualified_name
            self.scope.append(_Scope(symbol, child_prefix))
            pushed = True
            for kind, target in self._inheritances(node):
                self._add_relationship(target, kind, symbol.start_line)

        line = node.start_point.row + 1
        for target in self._imports(node):
            self._add_relationship(target, "imports", line)
        for target in self._calls(node):
            self._add_relationship(target, "calls", line)

        for child in node.named_children:
            self._visit(child)
        if pushed:
            self.scope.pop()

    def extract(self) -> FileGraph:
        self._visit(self.tree.root_node)
        symbols_by_name: dict[str, list[CodeSymbol]] = {}
        for symbol in self.symbols:
            symbols_by_name.setdefault(symbol.name, []).append(symbol)
        relationships = []
        for relationship in self.relationships:
            target = relationship.target_qualified_name
            if (
                relationship.target_symbol_key is None
                and "." not in target
                and len(symbols_by_name.get(target, [])) == 1
            ):
                target = symbols_by_name[target][0].qualified_name
            relationships.append(replace(relationship, target_qualified_name=target))

        diagnostics: tuple[GraphDiagnostic, ...] = ()
        if self.tree.root_node.has_error:
            error = next(
                (
                    node
                    for node in _walk(self.tree.root_node)
                    if node.type == "ERROR" or node.is_missing
                ),
                self.tree.root_node,
            )
            diagnostics = (
                GraphDiagnostic(
                    file_path=self.file_path,
                    message=(
                        f"{self.profile.language} parser recovered from invalid or "
                        "incomplete syntax"
                    ),
                    line=error.start_point.row + 1,
                ),
            )
        return FileGraph(
            symbols=tuple(self.symbols),
            relationships=tuple(relationships),
            diagnostics=diagnostics,
        )


@dataclass(frozen=True)
class TreeSitterAdapter:
    profile: _Profile
    version: str = TREE_SITTER_ADAPTER_VERSION

    @property
    def language(self) -> str:
        return self.profile.language

    @property
    def extensions(self) -> frozenset[str]:
        return self.profile.extensions

    def extract(self, path: Path, repo_root: Path) -> FileGraph:
        file_path = path.relative_to(repo_root).as_posix()
        try:
            source = path.read_bytes()
            if b"\0" in source:
                raise ValueError("binary file is not parseable source")
            return _TreeSitterExtractor(self.profile, file_path, source).extract()
        except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as error:
            return FileGraph(
                symbols=(),
                relationships=(),
                diagnostics=(
                    GraphDiagnostic(
                        file_path=file_path,
                        message=str(error),
                    ),
                ),
            )


ADAPTERS = (
    TreeSitterAdapter(
        _Profile(
            "javascript",
            frozenset({".cjs", ".js", ".jsx", ".mjs"}),
            tree_sitter_javascript.language,
        )
    ),
    TreeSitterAdapter(
        _Profile(
            "typescript",
            frozenset({".cts", ".mts", ".ts"}),
            tree_sitter_typescript.language_typescript,
        )
    ),
    TreeSitterAdapter(
        _Profile(
            "typescript",
            frozenset({".tsx"}),
            tree_sitter_typescript.language_tsx,
        )
    ),
    TreeSitterAdapter(
        _Profile("go", frozenset({".go"}), tree_sitter_go.language)
    ),
    TreeSitterAdapter(
        _Profile("java", frozenset({".java"}), tree_sitter_java.language)
    ),
    TreeSitterAdapter(
        _Profile("ruby", frozenset({".rb"}), tree_sitter_ruby.language)
    ),
    TreeSitterAdapter(
        _Profile("rust", frozenset({".rs"}), tree_sitter_rust.language)
    ),
    TreeSitterAdapter(
        _Profile("php", frozenset({".php"}), tree_sitter_php.language_php)
    ),
    TreeSitterAdapter(
        _Profile("c", frozenset({".c", ".h"}), tree_sitter_c.language)
    ),
    TreeSitterAdapter(
        _Profile(
            "cpp",
            frozenset({".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"}),
            tree_sitter_cpp.language,
        )
    ),
)

ADAPTER_BY_EXTENSION = {
    extension: adapter
    for adapter in ADAPTERS
    for extension in adapter.extensions
}


def adapter_for_path(path: Path) -> LanguageAdapter | None:
    return ADAPTER_BY_EXTENSION.get(path.suffix.lower())
