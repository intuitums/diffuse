from pathlib import Path

import pytest
from diffuse.repository.indexing.chunker import chunk_file
from diffuse.repository.indexing.graph import extract_file_graph, extract_repository_graph
from diffuse.repository.indexing.tree_sitter_graph import (
    TREE_SITTER_ADAPTER_VERSION,
    adapter_for_path,
)

LANGUAGE_CASES = [
    pytest.param(
        "src/user.js",
        """\
import { normalize as clean } from "./lib.js";
export class User extends Base {
  save(value) { return clean(value); }
}
export const run = (value) => clean(value);
""",
        "javascript",
        {
            ("class", "src.user.User"),
            ("method", "src.user.User.save"),
            ("function", "src.user.run"),
        },
        {
            ("imports", "src.lib"),
            ("inherits", "Base"),
            ("calls", "src.lib.normalize"),
        },
        id="javascript",
    ),
    pytest.param(
        "src/user.ts",
        """\
import { normalize } from "./lib";
interface Store extends Base { get(): string }
class User implements Store {
  get(): string { return normalize("value"); }
}
type UserID = string;
""",
        "typescript",
        {
            ("interface", "src.user.Store"),
            ("method", "src.user.Store.get"),
            ("class", "src.user.User"),
            ("method", "src.user.User.get"),
            ("type", "src.user.UserID"),
        },
        {
            ("inherits", "Base"),
            ("implements", "src.user.Store"),
            ("calls", "src.lib.normalize"),
        },
        id="typescript",
    ),
    pytest.param(
        "api/user.go",
        """\
package api
import "example/lib"
type User struct {
    Name string
}
func (user *User) Save(value string) string {
    return lib.Normalize(value)
}
func Run() {}
""",
        "go",
        {
            ("class", "api.User"),
            ("variable", "api.User.Name"),
            ("method", "api.User.Save"),
            ("function", "api.Run"),
        },
        {
            ("imports", "example.lib"),
            ("calls", "example.lib.Normalize"),
        },
        id="go",
    ),
    pytest.param(
        "src/User.java",
        """\
package app;
import lib.Util;
public class User extends Base implements Store {
    private String name;
    public String save(String value) {
        return Util.normalize(value);
    }
}
""",
        "java",
        {
            ("class", "app.User"),
            ("variable", "app.User.name"),
            ("method", "app.User.save"),
        },
        {
            ("imports", "lib.Util"),
            ("inherits", "Base"),
            ("implements", "Store"),
            ("calls", "lib.Util.normalize"),
        },
        id="java",
    ),
    pytest.param(
        "app/user.rb",
        """\
require "lib"
module App
  class User < Base
    def save(value)
      normalize(value)
    end
  end
end
""",
        "ruby",
        {
            ("namespace", "App"),
            ("class", "App.User"),
            ("method", "App.User.save"),
        },
        {
            ("imports", "lib"),
            ("inherits", "Base"),
            ("calls", "normalize"),
        },
        id="ruby",
    ),
    pytest.param(
        "src/user.rs",
        """\
use crate::lib::normalize as clean;
struct User { name: String }
trait Store { fn get(&self) -> String; }
impl Store for User {
    fn get(&self) -> String { clean(self.name) }
}
""",
        "rust",
        {
            ("class", "src.user.User"),
            ("variable", "src.user.User.name"),
            ("interface", "src.user.Store"),
            ("method", "src.user.Store.get"),
            ("implementation", "src.user.User.__impl__Store"),
            ("method", "src.user.User.get"),
        },
        {
            ("imports", "crate.lib.normalize"),
            ("implements", "src.user.Store"),
            ("calls", "crate.lib.normalize"),
        },
        id="rust",
    ),
    pytest.param(
        "src/User.php",
        """\
<?php
namespace App;
use Lib\\Util as Helper;
class User extends Base implements Store {
    private string $name;
    public function save(string $value): string {
        return Helper::normalize($value);
    }
}
function run(): void {}
""",
        "php",
        {
            ("class", "App.User"),
            ("variable", "App.User.name"),
            ("method", "App.User.save"),
            ("function", "App.run"),
        },
        {
            ("imports", "Lib.Util"),
            ("inherits", "Base"),
            ("implements", "Store"),
            ("calls", "Lib.Util.normalize"),
        },
        id="php",
    ),
    pytest.param(
        "src/user.c",
        """\
#include <stdio.h>
typedef struct User {
    int id;
} User;
int save(User *user) {
    return printf("%d", user->id);
}
""",
        "c",
        {
            ("class", "src.user.User"),
            ("variable", "src.user.User.id"),
            ("function", "src.user.save"),
        },
        {
            ("imports", "stdio.h"),
            ("calls", "printf"),
        },
        id="c",
    ),
    pytest.param(
        "src/user.cpp",
        """\
#include <string>
namespace app {
class User : public Base {
    int id;
    std::string save(std::string value) {
        return normalize(value);
    }
};
int run() { return 1; }
}
""",
        "cpp",
        {
            ("namespace", "app"),
            ("class", "app.User"),
            ("variable", "app.User.id"),
            ("method", "app.User.save"),
            ("function", "app.run"),
        },
        {
            ("imports", "string"),
            ("inherits", "Base"),
            ("calls", "normalize"),
        },
        id="cpp",
    ),
]


@pytest.mark.parametrize(
    ("relative_path", "source", "language", "expected_symbols", "expected_edges"),
    LANGUAGE_CASES,
)
def test_language_adapters_extract_symbols_and_relationships(
    tmp_path: Path,
    relative_path: str,
    source: str,
    language: str,
    expected_symbols: set[tuple[str, str]],
    expected_edges: set[tuple[str, str]],
):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)

    graph = extract_file_graph(path, tmp_path)

    assert not graph.diagnostics
    assert {symbol.language for symbol in graph.symbols} == {language}
    actual_symbols = {(symbol.kind, symbol.qualified_name) for symbol in graph.symbols}
    actual_edges = {
        (relationship.kind, relationship.target_qualified_name)
        for relationship in graph.relationships
    }
    assert expected_symbols <= actual_symbols
    assert expected_edges <= actual_edges


TYPESCRIPT_HERITAGE_SOURCE = """\
import { Base } from "./lib";
class Animal {}
class Dog extends Animal { bark(): void {} }
export class Puppy extends Dog {}
class Boxed extends Base<string> {}
class Scoped extends ns.Remote {}
abstract class Shelter extends Animal implements Store {}
interface Store extends Named {}
interface Boxes extends Base<string> {}
class Mixed extends mixin(Animal) {}
class Outer { make(): void { class Inner extends Animal {} } }
"""


@pytest.mark.parametrize("extension", [".ts", ".tsx"])
def test_typescript_class_extends_resolves_like_javascript(tmp_path: Path, extension: str):
    """``class X extends Y`` must resolve to Y, not to the raw ``extends Y`` clause text.

    The TypeScript grammar nests class base types inside ``class_heritage > extends_clause``
    while interfaces use a flat ``extends_type_clause``. Reading the clause node directly
    produced targets such as ``extendsAnimal`` that silently vanished downstream, so this
    asserts the exact edge set rather than a subset.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "lib.ts").write_text("export class Base<T> { value: T; }\n")
    path = source_dir / f"pets{extension}"
    path.write_text(TYPESCRIPT_HERITAGE_SOURCE)

    graph = extract_file_graph(path, tmp_path)
    names = {symbol.stable_key: symbol.qualified_name for symbol in graph.symbols}
    heritage = sorted(
        (
            names[relationship.source_symbol_key],
            relationship.kind,
            relationship.target_qualified_name,
        )
        for relationship in graph.relationships
        if relationship.kind in {"inherits", "implements"}
    )

    assert not graph.diagnostics
    assert heritage == [
        # ``class A extends B<T>`` resolves through the import alias, without ``<string>``.
        ("src.pets.Boxed", "inherits", "src.lib.Base"),
        # ``interface A extends B<T>`` keeps working and drops the type arguments.
        ("src.pets.Boxes", "inherits", "src.lib.Base"),
        ("src.pets.Dog", "inherits", "src.pets.Animal"),
        # A class nested in a method body owns its own edge and does not leak onto the
        # enclosing ``Outer``; ``class Mixed extends mixin(Animal)`` names no single base
        # type and is deliberately absent rather than emitting an unresolvable target.
        ("src.pets.Outer.make.Inner", "inherits", "src.pets.Animal"),
        # ``export class A extends B`` unwraps the export statement.
        ("src.pets.Puppy", "inherits", "src.pets.Dog"),
        # ``class A extends ns.B`` keeps the qualifier.
        ("src.pets.Scoped", "inherits", "ns.Remote"),
        # ``extends`` and ``implements`` on one class stay in their own buckets.
        ("src.pets.Shelter", "implements", "src.pets.Store"),
        ("src.pets.Shelter", "inherits", "src.pets.Animal"),
        ("src.pets.Store", "inherits", "Named"),
    ]


def test_javascript_cross_file_import_and_call_link_to_exact_symbol(tmp_path: Path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "lib.js").write_text(
        "export function normalize(value) { return value.trim(); }\n"
    )
    (source_dir / "service.js").write_text(
        'import { normalize as clean } from "./lib.js";\n'
        "export function save(value) { return clean(value); }\n"
    )

    graph = extract_repository_graph(tmp_path)
    normalize = next(
        symbol for symbol in graph.symbols if symbol.qualified_name == "src.lib.normalize"
    )
    call = next(
        relationship
        for relationship in graph.relationships
        if relationship.kind == "calls"
        and relationship.target_qualified_name == "src.lib.normalize"
    )

    assert call.target_symbol_key == normalize.stable_key


def test_adapter_stable_keys_do_not_change_when_only_body_content_changes(tmp_path: Path):
    path = tmp_path / "service.ts"
    path.write_text("export function calculate() { return 1; }\n")
    before = extract_file_graph(path, tmp_path)
    path.write_text("export function calculate() { return 2; }\n")
    after = extract_file_graph(path, tmp_path)

    before_symbol = next(symbol for symbol in before.symbols if symbol.name == "calculate")
    after_symbol = next(symbol for symbol in after.symbols if symbol.name == "calculate")

    assert before_symbol.stable_key == after_symbol.stable_key
    assert before_symbol.content_hash != after_symbol.content_hash


def test_parser_error_is_diagnostic_and_does_not_abort_other_files(tmp_path: Path):
    (tmp_path / "broken.ts").write_text("export function broken( {\n")
    (tmp_path / "healthy.go").write_text("package healthy\nfunc Ready() bool { return true }\n")

    graph = extract_repository_graph(tmp_path)

    assert any(diagnostic.file_path == "broken.ts" for diagnostic in graph.diagnostics)
    assert any(symbol.qualified_name == "healthy.Ready" for symbol in graph.symbols)


def test_parser_backed_chunking_uses_java_definition_boundaries(tmp_path: Path):
    path = tmp_path / "User.java"
    path.write_text(
        """\
package app;
import lib.Util;

class User {
    private String name;

    String save(String value) {
        return Util.normalize(value);
    }
}
"""
    )

    chunks = chunk_file(path, tmp_path)

    assert [(chunk.start_line, chunk.symbol_name) for chunk in chunks] == [
        (1, None),
        (4, "User"),
        (7, "save"),
    ]


def test_adapter_registry_is_versioned_and_covers_roadmap_extensions():
    expected = {
        ".c": "c",
        ".cpp": "cpp",
        ".go": "go",
        ".java": "java",
        ".js": "javascript",
        ".php": "php",
        ".rb": "ruby",
        ".rs": "rust",
        ".ts": "typescript",
        ".tsx": "typescript",
    }

    for extension, language in expected.items():
        adapter = adapter_for_path(Path(f"file{extension}"))
        assert adapter is not None
        assert adapter.language == language
        assert adapter.version == TREE_SITTER_ADAPTER_VERSION
