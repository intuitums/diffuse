from diffuse.review.diff import pack_diff_files, parse_unified_diff

DIFF = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +20,4 @@
-old_value = load()
+new_value = load()
 keep = True
+audit(new_value)
 return keep
"""


def test_parser_tracks_only_exact_changed_lines_on_each_side():
    parsed = parse_unified_diff(DIFF)

    assert len(parsed.files) == 1
    assert parsed.is_commentable("app.py", "LEFT", 10)
    assert parsed.is_commentable("app.py", "RIGHT", 20)
    assert parsed.is_commentable("app.py", "RIGHT", 22)
    assert not parsed.is_commentable("app.py", "RIGHT", 21)
    assert not parsed.is_commentable("app.py", "LEFT", 11)
    assert "+audit(new_value)" in parsed.snippet("app.py", "RIGHT", 22)


def test_parser_handles_added_deleted_and_renamed_files():
    parsed = parse_unified_diff(
        """\
diff --git a/dev/null b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+created = True
diff --git a/old.py b/dev/null
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -4 +0,0 @@
-removed = True
diff --git a/before.py b/after.py
similarity index 90%
rename from before.py
rename to after.py
--- a/before.py
+++ b/after.py
@@ -1 +1 @@
-before()
+after()
"""
    )

    assert [file.comment_path for file in parsed.files] == [
        "new.py",
        "old.py",
        "after.py",
    ]
    assert parsed.is_commentable("new.py", "RIGHT", 1)
    assert parsed.is_commentable("old.py", "LEFT", 4)
    assert parsed.is_commentable("after.py", "LEFT", 1)


def test_diff_packing_obeys_chunk_and_character_budgets():
    parsed = parse_unified_diff(DIFF)

    chunks, reviewed_paths = pack_diff_files(parsed, max_chars=60, max_chunks=1)

    assert len(chunks) == 1
    assert len(chunks[0]) <= 60
    assert reviewed_paths == {"app.py"}
