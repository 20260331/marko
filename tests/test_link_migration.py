from __future__ import annotations

import pytest

import marko
from marko import block, inline
from marko.ast_renderer import ASTRenderer
from marko.html_renderer import HTMLRenderer
from marko.link_migration import LinkAction, LinkKind, migrate_links
from marko.md_renderer import MarkdownRenderer


def link_nodes(document):
    nodes = []

    def visit(element):
        children = getattr(element, "children", None)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, (inline.Link, inline.AutoLink)):
                    nodes.append(child)
                visit(child)

    visit(document)
    return nodes


def image_nodes(document):
    nodes = []

    def visit(element):
        children = getattr(element, "children", None)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, inline.Image):
                    nodes.append(child)
                visit(child)

    visit(document)
    return nodes


def refdef_nodes(document):
    return [child for child in document.children if isinstance(child, block.LinkRefDef)]


class TestMigrateLinks:
    def test_relative_links_and_images_use_separate_mappings(self):
        document = marko.parse("[a](a.md) and ![b](images/b.png)\n")
        migrated, decisions = migrate_links(
            document,
            document_path="guide/intro.md",
            link_map={"guide/a.md": "pages/a.html"},
            image_map={"guide/images/b.png": "assets/b.png"},
        )

        assert link_nodes(migrated)[0].dest == "../pages/a.html"
        assert image_nodes(migrated)[0].dest == "../assets/b.png"
        # The image mapping must not touch document links and vice versa.
        assert {d.action for d in decisions if d.kind == LinkKind.LINK} == {
            LinkAction.REWRITTEN
        }
        assert {d.action for d in decisions if d.kind == LinkKind.IMAGE} == {
            LinkAction.REWRITTEN
        }

    def test_rewrite_is_relative_to_the_new_document_path(self):
        document = marko.parse("[a](../shared/s.md) and ![b](/img/logo.png)\n")
        migrated, _ = migrate_links(
            document,
            document_path="guide/intro.md",
            link_map={
                "guide/intro.md": "books/start/intro.html",
                "shared/s.md": "reference/s.html",
            },
            image_map={"/img/logo.png": "/static/logo.png"},
        )

        assert link_nodes(migrated)[0].dest == "../../reference/s.html"
        assert image_nodes(migrated)[0].dest == "/static/logo.png"

    def test_explicit_new_document_path(self):
        document = marko.parse("[a](a.md)\n")
        migrated, _ = migrate_links(
            document,
            document_path="guide/intro.md",
            new_document_path="guide/intro.html",
            link_map={"guide/a.md": "guide/appendix.html"},
        )
        assert link_nodes(migrated)[0].dest == "appendix.html"

    def test_query_and_fragment_are_preserved(self):
        document = marko.parse("[a](a.md?v=1#section)\n")
        migrated, _ = migrate_links(
            document,
            document_path="intro.md",
            link_map={"a.md": "pages/a.html"},
        )
        assert link_nodes(migrated)[0].dest == "pages/a.html?v=1#section"

    def test_external_and_anchor_destinations_are_left_alone(self):
        document = marko.parse(
            "<https://example.com> "
            "[mail](mailto:a@example.com) "
            "[sec](#section) "
            "[proto](//example.com/x)\n"
        )
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"intro.md": "moved.html"},
            image_map={},
        )

        dests = [node.dest for node in link_nodes(migrated)]
        assert dests == [
            "https://example.com",
            "mailto:a@example.com",
            "#section",
            "//example.com/x",
        ]
        assert [d.action for d in decisions] == [
            LinkAction.EXTERNAL,
            LinkAction.EXTERNAL,
            LinkAction.ANCHOR,
            LinkAction.EXTERNAL,
        ]

    def test_unmapped_local_destination_is_reported_not_rewritten(self):
        document = marko.parse("[a](missing.md)\n")
        migrated, decisions = migrate_links(
            document, document_path="intro.md", link_map={}
        )
        assert link_nodes(migrated)[0].dest == "missing.md"
        decision = decisions[0]
        assert decision.action == LinkAction.UNMAPPED
        assert decision.source_path == "missing.md"
        assert decision.target_path is None

    def test_callable_mapping(self):
        document = marko.parse("[a](a.md) [b](b.md)\n")

        def mapping(path):
            return "mapped/" + path if path == "a.md" else None

        migrated, decisions = migrate_links(
            document, document_path="intro.md", link_map=mapping
        )
        assert [node.dest for node in link_nodes(migrated)] == [
            "mapped/a.md",
            "b.md",
        ]
        assert [d.action for d in decisions] == [
            LinkAction.REWRITTEN,
            LinkAction.UNMAPPED,
        ]

    def test_unchanged_when_mapping_keeps_destination(self):
        document = marko.parse("[a](a.md)\n")
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"a.md": "a.md"},
        )
        assert link_nodes(migrated)[0].dest == "a.md"
        assert decisions[0].action == LinkAction.UNCHANGED


class TestReferenceDefinitions:
    def test_link_reference_definition_is_migrated_everywhere(self):
        text = "[a][r]\n\n[r]: old.md\n"
        document = marko.parse(text)
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"old.md": "new.md"},
        )

        assert link_nodes(migrated)[0].dest == "new.md"
        assert refdef_nodes(migrated)[0].dest == "new.md"
        assert migrated.link_ref_defs["r"] == ("new.md", None)

        decision = next(d for d in decisions if d.kind == LinkKind.LINK_REF_DEF)
        assert decision.action == LinkAction.REWRITTEN
        assert decision.label == "r"
        assert decision.source_path == "old.md"
        assert decision.target_path == "new.md"

        html = marko.Markdown(renderer=HTMLRenderer).render(migrated)
        assert 'href="new.md"' in html
        # The Markdown renderer must keep producing reference syntax with the
        # migrated destination instead of a stale or inlined link.
        markdown = marko.Markdown(renderer=MarkdownRenderer).render(migrated)
        assert "[a][r]" in markdown
        assert "[r]: new.md" in markdown
        assert "old.md" not in markdown

    def test_image_reference_definition_uses_image_mapping(self):
        text = '![a][r]\n\n[r]: images/p.png "t"\n'
        document = marko.parse(text)
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"images/p.png": "should-not-use.html"},
            image_map={"images/p.png": "assets/p.png"},
        )

        assert image_nodes(migrated)[0].dest == "assets/p.png"
        assert refdef_nodes(migrated)[0].dest == "assets/p.png"
        assert migrated.link_ref_defs["r"][0] == "assets/p.png"
        markdown = marko.Markdown(renderer=MarkdownRenderer).render(migrated)
        assert "[r]: assets/p.png" in markdown
        assert all(d.action != LinkAction.CONFLICT for d in decisions)

    def test_unreferenced_definition_is_still_migrated(self):
        text = "[r]: ghost.md\n"
        document = marko.parse(text)
        migrated, _ = migrate_links(
            document,
            document_path="intro.md",
            link_map={"ghost.md": "ghost-x.md"},
        )
        assert refdef_nodes(migrated)[0].dest == "ghost-x.md"
        assert migrated.link_ref_defs["r"][0] == "ghost-x.md"

    def test_shared_label_with_conflicting_mappings_is_reported(self):
        text = "[a][r] ![b][r]\n\n[r]: shared\n"
        document = marko.parse(text)
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"shared": "linked.html"},
            image_map={"shared": "img/out.png"},
        )

        # Inline nodes keep their own, individually mapped destinations.
        assert link_nodes(migrated)[0].dest == "linked.html"
        assert image_nodes(migrated)[0].dest == "img/out.png"
        # The shared definition cannot satisfy both and is left untouched.
        refdef_decision = next(d for d in decisions if d.kind == LinkKind.LINK_REF_DEF)
        assert refdef_decision.action == LinkAction.CONFLICT
        assert refdef_decision.updated == "shared"
        assert "linked.html" in refdef_decision.note
        assert refdef_nodes(migrated)[0].dest == "shared"
        assert migrated.link_ref_defs["r"][0] == "shared"

    def test_duplicate_label_keeps_first_definition_authoritative(self):
        text = "[a][r]\n\n[r]: one.md\n[r]: two.md\n"
        document = marko.parse(text)
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"one.md": "one-x.md", "two.md": "two-x.md"},
        )

        assert migrated.link_ref_defs["r"] == ("one-x.md", None)
        assert [node.dest for node in refdef_nodes(migrated)] == [
            "one-x.md",
            "two-x.md",
        ]
        refdef_decisions = [d for d in decisions if d.kind == LinkKind.LINK_REF_DEF]
        assert refdef_decisions[0].note == ""
        assert "Duplicate" in refdef_decisions[1].note


class TestDocumentImmutability:
    def test_original_document_is_not_mutated(self):
        text = "[a](a.md) ![b](p.png) [r][ref]\n\n" "[ref]: old/ref.md\n"
        document = marko.parse(text)
        original_html = marko.convert(text)

        migrate_links(
            document,
            document_path="intro.md",
            link_map={"a.md": "pages/a.html", "old/ref.md": "old/r-x.md"},
            image_map={"p.png": "assets/p.png"},
        )

        assert link_nodes(document)[0].dest == "a.md"
        assert image_nodes(document)[0].dest == "p.png"
        assert refdef_nodes(document)[0].dest == "old/ref.md"
        assert document.link_ref_defs["ref"] == ("old/ref.md", None)
        assert marko.Markdown(renderer=HTMLRenderer).render(document) == (original_html)

    def test_decisions_reference_migrated_nodes_with_source_spans(self):
        document = marko.parse("[a](a.md)\n")
        migrated, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"a.md": "pages/a.html"},
        )
        decision = decisions[0]
        assert decision.node is link_nodes(migrated)[0]
        assert decision.node is not link_nodes(document)[0]
        assert decision.span == decision.node.source_span
        assert decision.span is not None


class TestRendererCoverage:
    def test_ast_renderer_sees_migrated_destinations(self):
        document = marko.parse("[a](a.md)\n")
        migrated, _ = migrate_links(
            document,
            document_path="intro.md",
            link_map={"a.md": "pages/a.html"},
        )
        ast = marko.Markdown(renderer=ASTRenderer).render(migrated)
        paragraph = ast["children"][0]
        link = next(
            child for child in paragraph["children"] if child["element"] == "link"
        )
        assert link["dest"] == "pages/a.html"
        assert ast["link_ref_defs"] == {}

    def test_gfm_autolink_is_classified_as_external(self):
        markdown = marko.Markdown(extensions=["gfm"])
        document = markdown.parse("visit https://example.com now\n")
        _, decisions = migrate_links(
            document,
            document_path="intro.md",
            link_map={"https://example.com": "x"},
        )
        assert len(decisions) == 1
        assert decisions[0].kind == LinkKind.AUTO_LINK
        assert decisions[0].action == LinkAction.EXTERNAL


def test_migrate_links_rejects_non_document():
    with pytest.raises(TypeError):
        migrate_links(
            "[a](a.md)",
            document_path="intro.md",
            link_map={"a.md": "b.md"},
        )
