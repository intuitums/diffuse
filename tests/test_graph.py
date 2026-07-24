from pathlib import Path

from indexer.graph import extract_python_file_graph, extract_repository_graph


def _symbol_by_name(graph, qualified_name):
    return next(symbol for symbol in graph.symbols if symbol.qualified_name == qualified_name)


def test_python_graph_extracts_symbols_and_relationships(tmp_path: Path):
    package = tmp_path / "app"
    package.mkdir()
    source = package / "service.py"
    source.write_text(
        """\
from app.utils import normalize

DEFAULT_LIMIT: int = 10

class BaseService:
    pass

class UserService(BaseService):
    \"\"\"Coordinates user updates.\"\"\"

    def update(self, value: str) -> str:
        return self.persist(normalize(value))

    def persist(self, value: str) -> str:
        return value
"""
    )

    graph = extract_python_file_graph(source, tmp_path)

    assert not graph.diagnostics
    assert _symbol_by_name(graph, "app.service").kind == "module"
    assert _symbol_by_name(graph, "app.service.DEFAULT_LIMIT").kind == "variable"
    user_service = _symbol_by_name(graph, "app.service.UserService")
    update = _symbol_by_name(graph, "app.service.UserService.update")
    persist = _symbol_by_name(graph, "app.service.UserService.persist")
    assert user_service.docstring == "Coordinates user updates."
    assert update.kind == "method"

    edges = {
        (
            relationship.source_symbol_key,
            relationship.target_qualified_name,
            relationship.kind,
        )
        for relationship in graph.relationships
    }
    assert (user_service.stable_key, "app.service.BaseService", "inherits") in edges
    assert (update.stable_key, "app.utils.normalize", "calls") in edges
    assert (update.stable_key, "app.service.UserService.persist", "calls") in edges
    assert any(
        relationship.kind == "contains" and relationship.target_symbol_key == persist.stable_key
        for relationship in graph.relationships
    )


def test_repository_graph_links_cross_file_imports_and_calls(tmp_path: Path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "utils.py").write_text(
        """\
def normalize(value: str) -> str:
    return value.strip()
"""
    )
    (package / "service.py").write_text(
        """\
from app.utils import normalize

def update(value: str) -> str:
    return normalize(value)
"""
    )

    graph = extract_repository_graph(tmp_path)
    normalize = _symbol_by_name(graph, "app.utils.normalize")
    call = next(
        relationship
        for relationship in graph.relationships
        if relationship.kind == "calls"
        and relationship.target_qualified_name == "app.utils.normalize"
    )

    assert call.target_symbol_key == normalize.stable_key


def test_forward_and_relative_references_link(tmp_path: Path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "utils.py").write_text(
        """\
def normalize(value: str) -> str:
    return value.strip()
"""
    )
    (package / "service.py").write_text(
        """\
from .utils import normalize

def update(value: str) -> str:
    return later(normalize(value))

def later(value: str) -> str:
    return value
"""
    )

    graph = extract_repository_graph(tmp_path)
    normalize = _symbol_by_name(graph, "app.utils.normalize")
    later = _symbol_by_name(graph, "app.service.later")
    calls = [relationship for relationship in graph.relationships if relationship.kind == "calls"]

    assert any(
        relationship.target_qualified_name == "app.utils.normalize"
        and relationship.target_symbol_key == normalize.stable_key
        for relationship in calls
    )
    assert any(
        relationship.target_qualified_name == "app.service.later"
        and relationship.target_symbol_key == later.stable_key
        for relationship in calls
    )


def test_syntax_error_is_reported_without_aborting_repository(tmp_path: Path):
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n")

    graph = extract_python_file_graph(broken, tmp_path)

    assert graph.symbols == ()
    assert graph.relationships == ()
    assert len(graph.diagnostics) == 1
    assert graph.diagnostics[0].file_path == "broken.py"
    assert graph.diagnostics[0].line == 1
