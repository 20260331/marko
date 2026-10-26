from __future__ import annotations

import json

import pytest

import marko
from marko import Markdown
from marko.semdiff import (
    ChangeSet,
    SemNode,
    render_semantic,
    semantic_diff,
    semanticize,
)
from marko.semdiff import main as semdiff_main
from tests.normalize import normalize_html

GFM = Markdown(extensions=["gfm"])
FOOTNOTES = Markdown(extensions=["footnote"])


# Markdown spellings that are semantically identical and must NOT be
# reported as changes.
EQUIVALENT_PAIRS = [
    pytest.param("- a\n- b\n", "* a\n* b\n", id="bullet-dash-star"),
    pytest.param("+ a\n+ b\n", "* a\n* b\n", id="bullet-plus-star"),
    pytest.param("1. a\n2. b\n", "1) a\n2) b\n", id="ordered-delimiter"),
    pytest.param("# Hi there\n", "Hi there\n==========\n", id="atx-setext-h1"),
    pytest.param("## Hi there\n", "Hi there\n----------\n", id="atx-setext-h2"),
    pytest.param(
        "See [x](http://a).\n",
        "See [x][ref].\n\n[ref]: http://a\n",
        id="inline-reference-link",
    ),
    pytest.param(
        '[x](a "t")',
        '[x][r]\n\n[r]: a "t"\n',
        id="reference-link-with-title",
    ),
    pytest.param("<http://a>", "[http://a](http://a)", id="autolink-explicit"),
    pytest.param("<a@b.c>", "[a@b.c](mailto:a@b.c)", id="email-autolink"),
    pytest.param(r"\*not emph\*", "&#42;not emph&#42;", id="escape-entity"),
    pytest.param(
        "```python\nx\n```\n",
        "~~~python\nx\n~~~\n",
        id="fence-char-and-length",
    ),
    pytest.param("```\nx\n```\n", "    x\n", id="fenced-indented-code"),
    pytest.param("a b c\n", "a\nb c\n", id="soft-line-wrap"),
    pytest.param(
        "[r]: http://a\n\n[x][r]\n", "[x](http://a)\n", id="link-ref-def-order"
    ),
    pytest.param("a    b\n", "a b\n", id="inline-whitespace-width"),
]


@pytest.mark.parametrize("old,new", EQUIVALENT_PAIRS)
def test_equivalent_formatting_is_ignored(old, new):
    change_set = semantic_diff(old, new)
    assert change_set.is_empty()
    assert semanticize(old) == semanticize(new)
    assert not change_set


def test_gfm_equivalent_formatting_is_ignored():
    pairs = [
        ("- [x] buy milk\n", "* [x] buy milk\n"),
        ("~strike~", "~~strike~~"),
        ("| a | b |\n| --- | --- |\n| 1 | 2 |\n", "|a|b|\n|------|---|\n|1|2|\n"),
        ("> [!NOTE]\n> hello\n", "> [!note]\n> hello\n"),
    ]
    for old, new in pairs:
        assert semantic_diff(old, new, markdown=GFM).is_empty()


def test_empty_documents():
    assert semantic_diff("", "\n\n\n").is_empty()
    # Link reference definitions alone leave no semantic trace.
    assert semantic_diff("[r]: http://a\n", "").is_empty()
    assert semantic_diff("", "# title\n").stats()["insert"] == 1


def test_footnote_extension_is_supported_generically():
    old = "Text with note[^a].\n\n[^a]: Old note.\n"
    new = "Text with note[^a].\n\n[^a]: New note.\n"
    change_set = semantic_diff(old, new, markdown=FOOTNOTES)
    assert _find_update(change_set.changes, "footnote_def") is not None
    assert change_set.old_document() == semanticize(old, markdown=FOOTNOTES)
    assert change_set.new_document() == semanticize(new, markdown=FOOTNOTES)


class TestRealChanges:
    def test_inline_text_edit(self):
        change_set = semantic_diff("Hello world\n", "Hello there\n")
        stats = change_set.stats()
        assert stats == {"insert": 0, "delete": 0, "update": 1, "text": 1}
        [entry] = change_set.changes
        assert entry["op"] == "update"
        assert entry["type"] == "paragraph"
        text_ops = [
            part
            for child in entry["changes"]
            if child["op"] == "text"
            for part in child["parts"]
        ]
        assert {"op": "delete", "text": "world"} in text_ops
        assert {"op": "insert", "text": "there"} in text_ops

    def test_link_destination_change_is_attribute(self):
        change_set = semantic_diff("[x](a)\n", "[x](b)\n")
        # The link node is matched: only its destination attribute changed.
        updates = list(_walk(change_set.changes))
        link_updates = [u for u in updates if u["type"] == "link"]
        assert len(link_updates) == 1
        assert link_updates[0]["attrs"] == [{"name": "dest", "old": "a", "new": "b"}]

    def test_list_item_inserted(self):
        change_set = semantic_diff("- a\n- b\n", "- a\n- b\n- c\n")
        assert change_set.stats()["insert"] == 1
        inserted = [e for e in _walk_entries(change_set.changes) if e["op"] == "insert"]
        assert len(inserted) == 1
        assert inserted[0]["node"].type == "list_item"

    def test_heading_level_change(self):
        change_set = semantic_diff("# Hi\n", "## Hi\n")
        [top] = change_set.changes
        assert top["op"] == "update"
        assert top["type"] == "heading"
        assert [c for c in top["attrs"] if c["old"] != c["new"]] == [
            {"name": "level", "old": 1, "new": 2}
        ]

    def test_tight_and_loose_lists_are_not_equivalent(self):
        # Loose lists render <p> tags inside items, so they differ.
        change_set = semantic_diff("- a\n- b\n", "- a\n\n- b\n")
        assert not change_set.is_empty()

    def test_inline_boundaries_are_preserved(self):
        change_set = semantic_diff(
            "read [the docs](http://x) now\n",
            "read [the docs](http://x) today now\n",
        )
        paragraph = _find_update(change_set.changes, "paragraph")
        ops = [entry["op"] for entry in paragraph["changes"]]
        # A pure insertion of "today " next to the link: no text rewritten.
        inserted = [
            entry["node"]
            for entry in paragraph["changes"]
            if entry["op"] == "insert" and isinstance(entry["node"], str)
        ]
        assert "today " in inserted
        # The link stays an untouched context node with full attributes.
        links = [
            entry["node"]
            for entry in paragraph["changes"]
            if entry["op"] == "context"
            and isinstance(entry["node"], SemNode)
            and entry["node"].type == "link"
        ]
        assert len(links) == 1
        assert links[0].attr("dest") == "http://x"
        assert ops.count("text") == 0

    def test_block_structure_is_preserved(self):
        change_set = semantic_diff(
            "# Title\n\n- one\n- two\n",
            "# Title\n\n- one\n- two and more\n\nA new paragraph.\n",
        )
        # Heading is a context entry at document level, the list is updated
        # and a brand new paragraph is inserted (never flattened to text).
        top_ops = [(e["op"], _entry_type(e)) for e in change_set.changes]
        assert ("context", "heading") in top_ops
        assert ("insert", "paragraph") in top_ops
        assert ("update", "list") in top_ops

    def test_gfm_task_checkbox_change(self):
        change_set = semantic_diff("- [ ] task\n", "- [x] task\n", markdown=GFM)
        update = _find_update(change_set.changes, "paragraph")
        assert [c for c in update["attrs"] if c["old"] != c["new"]] == [
            {"name": "checked", "old": False, "new": True}
        ]

    def test_gfm_table_cell_edit(self):
        old = "| Name | Age |\n| --- | --- |\n| Ann | 30 |\n"
        new = "| Name | Age |\n| --- | --- |\n| Ann | 31 |\n"
        change_set = semantic_diff(old, new, markdown=GFM)
        assert not change_set.is_empty()
        cell_update = _find_update(change_set.changes, "table_cell")
        assert cell_update is not None


class TestReconstruction:
    @pytest.mark.parametrize(
        "text",
        [
            "# Title\n\nA paragraph with *em*, **strong** and"
            " [link](http://x).\n\n- a\n- b\n\n1. first\n2. second\n",
            "> quoted text\n>\n> second para\n",
            "- item1\n\n- item2\n",
            "```python\n\nprint(1)\n```\n",
            "Hard  \nbreak\n",
            "a `code` and `` `back` `` and \\*star\\*\n",
        ],
    )
    def test_reconstruct_both_sides_from_change_set(self, text):
        change_set = semantic_diff(text, "# rewritten\n\nnew body\n")
        assert change_set.old_document() == semanticize(text)
        assert change_set.new_document() == semanticize("# rewritten\n\nnew body\n")

    def test_reconstruct_roundtrips_through_json(self):
        change_set = semantic_diff("Hello [x](a)\n", "Hello again [x](b)\n")
        restored = ChangeSet.from_dict(json.loads(change_set.to_json()))
        assert restored.old_document() == change_set.old_document()
        assert restored.new_document() == change_set.new_document()

    @pytest.mark.parametrize(
        "text",
        [
            "# h\n\np with [l](u) and ![i](p)\n\n- a\n- b\n",
            "> quote\n>\n> - nested\n> - list\n",
            "```\n<keep>\n```\n",
        ],
    )
    def test_rendered_semantic_result_is_html_equivalent(self, text):
        for side in (render_semantic(semanticize(text)),):
            assert normalize_html(marko.convert(side)) == normalize_html(
                marko.convert(text)
            )

    def test_change_set_reconstructs_after_json_roundtrip(self, tmp_path):
        old = "- one\n- two\n\nsee [x](u)\n"
        new = "- one\n- three\n\nsee [x](v)\n"
        change_set = semantic_diff(old, new)
        payload = change_set.to_json()
        assert payload == semantic_diff(old, new).to_json()  # deterministic
        restored = ChangeSet.from_dict(json.loads(payload))
        assert render_semantic(restored.old_document()).strip()
        assert restored.old_document() == semanticize(old)
        assert restored.new_document() == semanticize(new)


class TestReviewDocument:
    def test_empty_review(self):
        review = semantic_diff("- a\n", "* a\n").review()
        assert "No semantic changes" in review

    def test_review_marks_insert_delete_and_attr_change(self):
        review = semantic_diff("# a\n\n- x\n", "## a\n\n- x\n- y\n").review()
        assert "level: 1 -> 2" in review
        assert "+ y" in review
        assert "@heading" in review
        assert "@list" in review

    def test_review_marks_text_edits_with_boundaries(self):
        review = semantic_diff(
            "see [docs](u) now\n", "see guide [docs](u) now!\n"
        ).review()
        # Word-level marks on the same line as the intact link node.
        assert "[docs](u)" in review
        assert "{+guide +}" in review
        assert "{+!+}" in review


class TestSemNodeSerialization:
    def test_semnode_dict_roundtrip(self):
        node = semanticize("# Hi [x](u)\n")
        assert SemNode.from_dict(node.to_dict()) == node

    def test_change_set_json_is_stable(self):
        old, new = "a [x](u) b\n- q\n", "a [x](u) bc\n- q\n- r\n"
        first = semantic_diff(old, new).to_json()
        second = semantic_diff(old, new).to_json()
        assert json.loads(first) == json.loads(second)
        # JSON contains the full context required for reconstruction.
        assert '"op": "context"' in first


class TestCli:
    def test_cli_writes_review_and_json(self, tmp_path):
        old = tmp_path / "old.md"
        new = tmp_path / "new.md"
        review = tmp_path / "review.md"
        payload = tmp_path / "changes.json"
        old.write_text("# a\n", encoding="utf-8")
        new.write_text("## a\n", encoding="utf-8")

        semdiff_main([str(old), str(new), "-o", str(review)])
        assert "level: 1 -> 2" in review.read_text(encoding="utf-8")

        semdiff_main([str(old), str(new), "--json", "-o", str(payload)])
        data = json.loads(payload.read_text(encoding="utf-8"))
        assert data["version"] == 1
        restored = ChangeSet.from_dict(data)
        assert restored.old_document() == semanticize("# a\n")


# -- helpers ----------------------------------------------------------------


def _walk(entries):
    for entry in entries:
        if entry["op"] == "update":
            yield entry
            yield from _walk(entry["changes"])


def _walk_entries(entries):
    for entry in entries:
        yield entry
        if entry["op"] == "update":
            yield from _walk_entries(entry["changes"])


def _find_update(entries, node_type):
    for entry in _walk(entries):
        if entry["type"] == node_type:
            return entry
    return None


def _entry_type(entry):
    if entry["op"] in ("context", "insert", "delete"):
        node = entry["node"]
        return node.type if isinstance(node, SemNode) else None
    if entry["op"] == "update":
        return entry["type"]
    return None
