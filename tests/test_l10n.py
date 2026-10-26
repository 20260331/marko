from __future__ import annotations

import json

import pytest

import marko
from marko import Catalog, CatalogError, apply_translations, export_catalog
from marko.l10n import CatalogEntry
from marko.md_renderer import MarkdownRenderer


def _find(catalog: Catalog, source: str) -> CatalogEntry:
    for entry in catalog:
        if entry.source == source:
            return entry
    raise KeyError(source)


class TestCatalogExport:
    def test_export_separates_text_from_structure(self):
        text = (
            "Use `git status` to see **changes** and read "
            '[the docs](https://example.com/docs "Guide").\n'
        )
        catalog = Catalog.export(text)
        assert len(catalog) == 1
        entry = catalog["0"]
        assert entry.block == "paragraph"
        assert entry.level is None
        assert entry.source == "Use {0} to see {1} and read {2}."
        assert entry.translation == ""

        code, strong, link = entry.placeholders
        assert code.name == "0"
        assert code.type == "code_span"
        assert code.attrs == {"content": "git status"}
        assert code.segment is None

        assert strong.type == "strong_emphasis"
        assert strong.segment is not None
        assert strong.segment.source == "changes"
        assert strong.segment.placeholders == []

        assert link.type == "link"
        assert link.attrs == {
            "dest": "https://example.com/docs",
            "title": "Guide",
        }
        assert link.segment is not None
        assert link.segment.source == "the docs"

    def test_export_is_sentence_level_not_rawtext_level(self):
        # A single sentence containing mixed structure becomes ONE entry with
        # full sentence context, instead of several RawText fragments.
        catalog = Catalog.export("a `c` b *e* d\n")
        assert len(catalog) == 1
        assert catalog["0"].source == "a {0} b {1} d"

    def test_heading_and_setext_heading_levels(self):
        catalog = Catalog.export("### Deep heading\n\nHello\n=====\n")
        assert catalog["0"].block == "heading"
        assert catalog["0"].level == 3
        assert catalog["0"].source == "Deep heading"
        assert catalog["2"].block == "setext_heading"
        assert catalog["2"].level == 1
        assert catalog["2"].source == "Hello"

    def test_entry_paths_through_quotes_and_lists(self):
        text = "> q *a*\n>\n> 1. item `x`\n"
        catalog = Catalog.export(text)
        ids = {entry.id: entry for entry in catalog}
        assert "0.0" in ids  # quote -> paragraph
        assert ids["0.0"].source == "q {0}"
        assert "0.2.0.0" in ids  # quote -> list -> item -> paragraph
        assert ids["0.2.0.0"].source == "item {0}"

    def test_protected_blocks_are_not_exported(self):
        text = (
            "# Heading\n\n"
            "```python\ncode <b> *not text*\n```\n\n"
            "    indented code\n\n"
            "<div>html block</div>\n\n"
            "real paragraph\n"
        )
        catalog = Catalog.export(text)
        sources = [entry.source for entry in catalog]
        assert sources == ["Heading", "real paragraph"]

    def test_segments_without_translatable_text_are_skipped(self):
        # Only a code span: nothing translatable.
        assert Catalog.export("`only code`\n").entries == []
        # Empty document.
        assert Catalog.export("").entries == []
        # An image alone IS exported because its alt text is translatable.
        catalog = Catalog.export("![alt text](img.png)\n")
        assert len(catalog) == 1
        assert catalog["0"].source == "{0}"
        image = catalog["0"].placeholders[0]
        assert image.type == "image"
        assert image.segment.source == "alt text"

    def test_protected_inline_kinds(self):
        text = (
            "see <http://a.b>, [ref][r], \\*, a hard  \nbreak and <br> tag.\n\n"
            "[r]: http://ref.example\n"
        )
        entry = _find(
            Catalog.export(text), "see {0}, {1}, {2}, a hard{3}break and {4} tag."
        )
        kinds = [(p.type, p.attrs) for p in entry.placeholders]
        assert kinds == [
            ("auto_link", {"dest": "http://a.b"}),
            ("link", {"dest": "http://ref.example"}),
            ("literal", {"char": "*"}),
            ("line_break", {"soft": False}),
            ("inline_html", {}),
        ]
        # The reference link text remains a nested translatable segment.
        assert entry.placeholders[1].segment.source == "ref"
        # Auto links and code are fully protected.
        assert entry.placeholders[0].segment is None

    def test_literal_braces_are_escaped_in_source(self):
        catalog = Catalog.export("value is {x} *y*\n")
        assert catalog["0"].source == "value is {{x}} {0}"


class TestCatalogApply:
    def test_untranslated_catalog_matches_markdown_renderer(self):
        text = "# Title\n\nUse `code` and [link](http://x).\n"
        markdown = marko.Markdown(renderer=MarkdownRenderer)
        catalog = Catalog.export(text)
        assert catalog.apply(text) == markdown(text)

    def test_apply_translation_with_placeholder_reordering(self):
        text = (
            "Use `git status` to see **changes** and read "
            '[the docs](https://example.com/docs "Guide").\n'
        )
        catalog = Catalog.export(text)
        entry = catalog["0"]
        # Reorder all three placeholders, different word order.
        entry.translation = "请阅读{2}，用{0}查看{1}。"
        entry.placeholders[1].segment.translation = "更改"
        entry.placeholders[2].segment.translation = "文档"

        result = catalog.apply(text)
        assert result == (
            '请阅读[文档](https://example.com/docs "Guide")，' "用`git status`查看**更改**。\n"
        )

    def test_translation_cannot_change_protected_structure(self):
        # The translator never receives any Markdown syntax: link destination,
        # code content and emphasis marks all come from the source AST.
        text = "Run `cmd` via [site](http://example.com) *now*\n"
        catalog = Catalog.export(text)
        entry = catalog["0"]
        entry.translation = "{0} {1} {2}"
        result = catalog.apply(text)
        assert "`cmd`" in result
        assert "(http://example.com)" in result
        assert "*now*" in result

    def test_apply_nested_segment_only(self):
        text = "see [the docs](http://x) please\n"
        catalog = Catalog.export(text)
        entry = catalog["0"]
        entry.translation = "{0} please"
        entry.placeholders[0].segment.translation = "文档"
        result = catalog.apply(text)
        assert result == "[文档](http://x) please\n"

    def test_untranslated_nested_segment_keeps_source(self):
        text = "see [the docs](http://x)\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "voir {0}"
        # Link text segment is left without a translation.
        result = catalog.apply(text)
        assert result == "voir [the docs](http://x)\n"

    def test_image_alt_is_translated_destination_protected(self):
        text = '![alt text](img.png "title") here\n'
        catalog = Catalog.export(text)
        placeholder = catalog["0"].placeholders[0]
        placeholder.segment.translation = "替代文本"
        catalog["0"].translation = "{0} 在此"
        assert catalog.apply(text) == '![替代文本](img.png "title") 在此\n'

    @pytest.mark.parametrize(
        "source,expected_source,expected_result",
        [
            ("line one\nline two\n", "line one{0}line two", "甲\n乙\n"),
            ("hard break  \nafter\n", "hard break{0}after", "甲\\\n乙\n"),
        ],
    )
    def test_line_breaks_are_protected(self, source, expected_source, expected_result):
        catalog = Catalog.export(source)
        assert catalog["0"].source == expected_source
        ph = catalog["0"].placeholders[0]
        assert ph.type == "line_break"
        catalog["0"].translation = "甲{0}乙"
        result = catalog.apply(source)
        assert result == expected_result
        # The break kind (soft space vs hard <br>) is preserved in HTML.
        assert ("<br" in marko.convert(result)) == ("hard" in source)

    def test_line_break_at_end_of_link_text(self):
        # A soft line break can be the last child of a link text segment.
        source = "[a\n](u) text\n"
        catalog = Catalog.export(source)
        catalog["0"].translation = "{0} 文本"
        result = catalog.apply(source)
        assert result == "[a\n](u) 文本\n"

    def test_translation_with_literal_braces(self):
        text = "value is {x} *y*\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "值为 {{x}} {0}"
        catalog["0"].placeholders[0].segment.translation = "z"
        result = catalog.apply(text)
        assert result == "值为 {x} *z*\n"

    def test_quotes_and_headings_are_translated(self):
        text = "# Hello\n\n> quoted **bold** text\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "你好"
        catalog["2.0"].translation = "引用{0}文本"
        catalog["2.0"].placeholders[0].segment.translation = "粗体"
        result = catalog.apply(text)
        assert result == "# 你好\n\n> 引用**粗体**文本\n"

    def test_apply_accepts_parsed_document(self):
        document = marko.parse("Hello *world*\n")
        catalog = export_catalog(document)
        catalog["0"].translation = "你好{0}"
        catalog["0"].placeholders[0].segment.translation = "世界"
        result = apply_translations(marko.parse("Hello *world*\n"), catalog)
        assert result == "你好*世界*\n"


class TestCatalogValidation:
    @pytest.mark.parametrize(
        "translation",
        [
            "请阅读{2}，用{0}。",  # missing {1}
            "{0}{1}{2} extra {3}",  # unknown placeholder
            "{0} {1} {2} {0}",  # placeholder used twice
            "brace {0 broken",  # malformed brace
            "{0}{1}{2}}",  # lone closing brace
        ],
    )
    def test_bad_placeholder_usage_is_rejected(self, translation):
        text = "Use `a` *b* and [c](d)\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = translation
        with pytest.raises(CatalogError) as excinfo:
            catalog.apply(text)
        assert excinfo.value.entry_id == "0"

    @pytest.mark.parametrize(
        "injected",
        [
            "**injected emphasis**{0}{1}{2}",
            "`injected code`{0}{1}{2}",
            "[injected link](http://evil){0}{1}{2}",
        ],
    )
    def test_markdown_structure_in_translation_is_rejected(self, injected):
        text = "Use `a` *b* and [c](d)\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = injected
        with pytest.raises(CatalogError, match="structure"):
            catalog.apply(text)

    def test_markdown_structure_in_nested_segment_is_rejected(self):
        text = "see [docs](http://x)\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "{0}"
        catalog["0"].placeholders[0].segment.translation = "[hacked](http://evil)"
        with pytest.raises(CatalogError) as excinfo:
            catalog.apply(text)
        assert excinfo.value.entry_id == "0.0"

    def test_duplicate_placeholders_must_all_be_present(self):
        text = "`a` and `a`\n"
        catalog = Catalog.export(text)
        assert [p.type for p in catalog["0"].placeholders] == [
            "code_span",
            "code_span",
        ]
        catalog["0"].translation = "only {0}"
        with pytest.raises(CatalogError, match="missing"):
            catalog.apply(text)

    def test_catalog_from_other_document_is_rejected(self):
        catalog = Catalog.export("Hello *world* and `code`\n")
        catalog["0"].translation = "{0}{1}"
        with pytest.raises(CatalogError, match="does not match the source document"):
            catalog.apply("Hello *world*\n")

    def test_failed_apply_leaves_document_unchanged(self):
        text = "first *a*\n\nsecond `b`\n"
        document = marko.parse(text)
        catalog = export_catalog(document)
        catalog["0"].translation = "第一{0}"
        catalog["0"].placeholders[0].segment.translation = "甲"
        catalog["2"].translation = "**bad**"  # injected structure -> rejected
        with pytest.raises(CatalogError):
            catalog.apply(document)
        # The valid first entry must not be partially applied before the
        # invalid second entry is rejected.
        assert marko.Markdown(renderer=MarkdownRenderer).render(document) == (
            marko.Markdown(renderer=MarkdownRenderer).render(marko.parse(text))
        )

    def test_error_message_describes_the_difference(self):
        text = "a `code` b\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "a b **x**"
        # Placeholder {0} is missing AND extra emphasis is introduced; report
        # the placeholder problem first.
        with pytest.raises(CatalogError, match="missing placeholder"):
            catalog.apply(text)

        catalog2 = Catalog.export(text)
        catalog2["0"].translation = "a {0} b **x**"
        with pytest.raises(CatalogError, match="unexpected strong_emphasis"):
            catalog2.apply(text)


class TestCatalogSerialization:
    def test_to_dict_is_json_serializable(self):
        text = 'Use `a` to see **b** at [c](http://x "T").\n'
        catalog = Catalog.export(text)
        catalog["0"].translation = "译文{0}{1}{2}"
        data = catalog.to_dict()
        assert data["version"] == 1
        encoded = json.dumps(data, ensure_ascii=False)
        restored = json.loads(encoded)
        assert restored == data

    def test_json_roundtrip_applies_identically(self):
        text = "Use `a` **b** [c](http://x).\n"
        catalog = Catalog.export(text)
        catalog["0"].translation = "用{0}{1}{2}。"
        catalog["0"].placeholders[1].segment.translation = "乙"
        catalog["0"].placeholders[2].segment.translation = "丙"
        expected = catalog.apply(text)

        restored = Catalog.from_dict(json.loads(json.dumps(catalog.to_dict())))
        assert restored.apply(text) == expected

    def test_from_dict_rejects_unknown_version(self):
        with pytest.raises(ValueError, match="version"):
            Catalog.from_dict({"version": 99, "entries": []})


class TestGFMExtension:
    def test_gfm_strikethrough_table_task_and_url(self):
        markdown = marko.Markdown(renderer=MarkdownRenderer, extensions=["gfm"])
        text = (
            "# Title *x*\n\n"
            "| Col A | Col B |\n|---|---|\n"
            "| cell `one` | [link](http://x) |\n\n"
            "- [ ] task with ~~old~~ text\n"
            "- plain item\n\n"
            "Visit https://example.com now\n"
        )
        catalog = Catalog.export(text, markdown=markdown)
        # Untranslated application is an exact Markdown-renderer roundtrip.
        assert catalog.apply(text, markdown=markdown) == markdown(text)

        catalog["0"].translation = "标题 {0}"
        catalog["0"].placeholders[0].segment.translation = "甲"
        assert catalog["2.0.0"].source == "Col A"
        catalog["2.0.0"].translation = "列甲"
        catalog["2.0.1"].translation = "列乙"
        catalog["2.1.0"].translation = "单元格 {0}"
        catalog["2.1.1"].translation = "{0}"
        catalog["2.1.1"].placeholders[0].segment.translation = "链接"
        catalog["4.0.0"].translation = "任务{0}文本"
        catalog["4.0.0"].placeholders[0].segment.translation = "旧"
        catalog["4.1.0"].translation = "普通项"
        catalog["6"].translation = "访问 {0}"

        result = catalog.apply(text, markdown=markdown)
        assert "标题 *甲*" in result
        assert "| 列甲 | 列乙 |" in result
        assert "| 单元格 `one` | [链接](http://x) |" in result
        # The task-list marker keeps its separating space, so re-parsing the
        # translated document keeps the checkbox.
        assert "- [ ] 任务~~旧~~文本" in result
        assert "普通项" in result
        assert "https://example.com" in result

        # The protected structure renders to the same HTML tags.
        import re

        gfm_converter = marko.Markdown(extensions=["gfm"])

        def tags(markdown_text):
            return sorted(re.findall(r"<(\w+)[ >]", gfm_converter(markdown_text)))

        assert tags(text) == tags(result)

    def test_task_list_marker_space_is_preserved(self):
        markdown = marko.Markdown(renderer=MarkdownRenderer, extensions=["gfm"])
        gfm_converter = marko.Markdown(extensions=["gfm"])
        for marker, checked in (("[ ]", False), ("[x]", True)):
            text = f"- {marker} buy *milk*\n"
            catalog = Catalog.export(text, markdown=markdown)
            entry = catalog["0.0.0"]
            assert entry.source == f" buy {{0}}"
            entry.translation = "买{0}"
            entry.placeholders[0].segment.translation = "牛奶"
            result = catalog.apply(text, markdown=markdown)
            assert result == f"- {marker} 买*牛奶*\n"
            # Re-parsing the translated Markdown keeps the checkbox state.
            html = gfm_converter(result)
            assert 'type="checkbox"' in html
            assert ('checked=""' in html) is checked


class TestFootnoteExtension:
    def test_footnote_reference_and_definition(self):
        markdown = marko.Markdown(renderer=MarkdownRenderer, extensions=["footnote"])
        text = "See note[^1] now.\n\n[^1]: footnote body *z*\n"
        catalog = Catalog.export(text, markdown=markdown)
        assert catalog["0"].source == "See note{0} now."
        assert catalog["0"].placeholders[0].type == "footnote_ref"
        assert catalog["0"].placeholders[0].attrs == {"label": "1"}
        assert catalog["2.0"].source == "footnote body {0}"

        catalog["0"].translation = "现在参见注释{0}。"
        catalog["2.0"].translation = "脚注内容{0}"
        catalog["2.0"].placeholders[0].segment.translation = "乙"
        result = catalog.apply(text, markdown=markdown)
        assert "[^1]" in result
        assert "脚注内容*乙*" in result
