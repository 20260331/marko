import pytest

from marko import Markdown


class TestFootnote:
    def setup_method(self):
        self.markdown = Markdown()
        self.markdown.use("footnote")

    def test_footnote(self):
        result = self.markdown("this is a footnote[^1].\n\n[^1]: foo\n")
        assert '<sup class="footnote-ref"' in result
        assert 'foo<a href="#fnref-1" class="footnote">&#8617;</a>' in result

    def test_non_footnote(self):
        result = self.markdown("foo[^1]")
        assert result.rstrip() == "<p>foo[^1]</p>"


class TestToc:
    def setup_method(self):
        self.markdown = Markdown()
        self.markdown.use("toc")

    def test_render_toc(self):
        content = "# Foo\n## Foobar\n## Foofooz\n# Bar\n"
        result = self.markdown(content)
        assert '<h1 id="foo">Foo</h1>' in result
        toc = self.markdown.renderer.render_toc()
        assert '<ul>\n<li><a href="#foo">Foo</a></li>' in toc
        assert '<ul>\n<li><a href="#foobar">Foobar</a></li>' in toc

    def test_render_toc_exceeding_maxdepth(self):
        content = "#### Foobar\n"
        self.markdown(content)
        toc = self.markdown.renderer.render_toc()
        assert '<li><a href="#foobar">Foobar</a></li>' in toc
        content = "# Foo\n#### Foobar\n"
        self.markdown(content)
        toc = self.markdown.renderer.render_toc()
        assert '<li><a href="#foo">Foo</a></li>' in toc
        assert '<li><a href="#foobar">Foobar</a></li>' not in toc

    def test_render_toc_replace_tags(self):
        from marko.ext.toc import make_extension as Toc

        markdown = Markdown(extensions=[Toc("<div>", "</div>")])
        content = "#### Foobar\n"
        markdown(content)
        toc = markdown.renderer.render_toc()
        assert "<div>\n" in toc
        assert "</div>\n" in toc


class TestPangu:
    def setup_method(self):
        self.markdown = Markdown()
        self.markdown.use("pangu")

    def test_render_pangu(self):
        content = "中国2018年"
        result = self.markdown(content)
        assert (
            result
            == '<p>中国<span class="pangu"></span>2018<span class="pangu"></span>年</p>\n'
        )

    def test_chinese_punctuations(self):
        content = "你好：中国。"
        result = self.markdown(content)
        assert result == "<p>你好：中国。</p>\n"


class TestCodeHilite:
    def setup_method(self):
        self.markdown = Markdown(extensions=["codehilite"])

    def test_render_fenced_code(self):
        content = '```python\nprint("hello")\n```'
        assert '<div class="highlight">' in self.markdown(content)
        content = '```foobar\nprint("hello")\n```'
        # Fallback to normal output.
        assert '<div class="highlight">' in self.markdown(content)

    def test_render_code_block(self):
        content = '    print("hello")\n'
        assert '<div class="highlight">' in self.markdown(content)

    def test_codehilite_options(self):
        from marko.ext.codehilite import make_extension

        markdown = Markdown(extensions=[make_extension(linenos="table")])
        content = '```python\nprint("hello")\n```'
        assert '<table class="highlighttable">' in markdown(content)

    def test_render_code_block_with_extra(self):
        content = '```python filename="test.py"\nprint("hello")\n```'
        assert '<span class="filename">test.py</span>' in self.markdown(content)


class TestGFMAlert:
    def setup_method(self):
        from marko import Markdown
        from marko.ast_renderer import ASTRenderer
        from marko.ext.gfm import GFM

        self.md_ast = Markdown(renderer=ASTRenderer, extensions=[GFM])
        self.md_html = Markdown(extensions=[GFM])

    @pytest.mark.parametrize(
        "alert_type", ["WARNING", "CAUTION", "TIP", "warning", "Caution"]
    )
    def test_alert_ast(self, alert_type):
        text = f"> [!{alert_type}]\n> Foo bar\n> Bar\n"
        ast = self.md_ast(text)
        admon = ast["children"][0]
        assert admon["element"] == "alert"
        assert admon["alert_type"] == alert_type.upper()
        inner = admon["children"][0]["children"]
        assert inner[0]["children"] == "Foo bar"
        assert inner[1]["element"] == "line_break"
        assert inner[2]["children"] == "Bar"

    @pytest.mark.parametrize(
        "alert_type", ["WARNING", "CAUTION", "TIP", "warning", "Caution"]
    )
    def test_alert_html(self, alert_type):
        text = f"> [!{alert_type}]\n> Foo bar\n> Bar\n"
        html = self.md_html(text)
        assert f'<blockquote class="alert alert-{alert_type.lower()}">' in html
        assert f"<p>{alert_type.title()}</p>" in html
        assert "<p>Foo bar\nBar</p>" in html

    def test_alert_disallow_content_on_header_line(self):
        text = "> [!NOTE] This is not allowed.\n> Foo bar\n"
        html = self.md_html(text)
        # Should be treated as a normal blockquote.
        assert "<blockquote>\n<p>[!NOTE] This is not allowed." in html


class TestInclude:
    def _make(self, fragments, *, on_error="raise", extensions=()):
        from marko.ext.include import make_extension

        def resolver(target):
            if callable(fragments):
                return fragments(target)
            if target not in fragments:
                return None
            return fragments[target]

        markdown = Markdown()
        markdown.use("footnote", make_extension(resolver, on_error=on_error))
        for extension in extensions:
            markdown.use(extension)
        return markdown

    def test_basic_include(self):
        markdown = self._make({"frag.md": "Hello from fragment."})
        result = markdown("Before\n\n!include frag.md\n\nAfter\n")
        assert "<p>Before</p>" in result
        assert "<p>Hello from fragment.</p>" in result
        assert "<p>After</p>" in result

    def test_quoted_target(self):
        markdown = self._make({"my frag.md": "Quoted."})
        result = markdown('!include "my frag.md"\n')
        assert "<p>Quoted.</p>" in result

    def test_link_ref_defs_have_independent_scopes(self):
        fragments = {
            "frag.md": "Frag [link][x].\n\n[x]: https://frag.example\n",
        }
        markdown = self._make(fragments)
        result = markdown(
            "Top [link][x].\n\n"
            "!include frag.md\n\n"
            "Bottom [link][x].\n\n"
            "[x]: https://top.example\n"
        )
        assert result.count('href="https://top.example"') == 2
        assert result.count('href="https://frag.example"') == 1

    def test_fragment_link_ref_def_does_not_leak(self):
        markdown = self._make({"frag.md": "[x]: https://frag.example\n"})
        result = markdown("!include frag.md\n\norphan [x]\n")
        assert "orphan [x]" in result
        assert "https://frag.example" not in result

    def test_sibling_fragments_keep_same_named_refs(self):
        fragments = {
            "a.md": "A [x][x].\n\n[x]: https://a.example\n",
            "b.md": "B [x][x].\n\n[x]: https://b.example\n",
        }
        markdown = self._make(fragments)
        result = markdown("!include a.md\n\n!include b.md\n")
        assert 'href="https://a.example"' in result
        assert 'href="https://b.example"' in result

    def test_footnotes_have_independent_scopes(self):
        fragments = {
            "a.md": "A note[^1].\n\n[^1]: first note\n",
            "b.md": "B note[^1].\n\n[^1]: second note\n",
        }
        markdown = self._make(fragments)
        result = markdown(
            "Root note[^1].\n\n"
            "!include a.md\n\n"
            "!include b.md\n\n"
            "[^1]: root note\n"
        )
        assert 'id="fnref-include0-1"' in result
        assert 'id="fn-include0-1"' in result
        assert 'id="fnref-include1-1"' in result
        assert 'id="fn-include1-1"' in result
        assert 'id="fnref-1"' in result
        assert "first note" in result
        assert "second note" in result
        assert "root note" in result

    def test_nested_include(self):
        fragments = {
            "a.md": "Outer.\n\n!include b.md\n",
            "b.md": "Inner.\n",
        }
        markdown = self._make(fragments)
        result = markdown("!include a.md\n")
        assert "<p>Outer.</p>" in result
        assert "<p>Inner.</p>" in result

    def test_nested_include_footnote(self):
        fragments = {
            "a.md": "Outer text[^1].\n\n!include b.md\n",
            "b.md": "Inner note[^1].\n\n[^1]: inner footnote\n",
        }
        markdown = self._make(fragments)
        result = markdown("!include a.md\n")
        assert 'href="#fn-include0-include0-1"' in result
        assert "inner footnote" in result

    def test_include_inside_quote_keeps_structure(self):
        markdown = self._make({"frag.md": "Hello\n\n- one\n- two\n"})
        result = markdown("> !include frag.md\n")
        assert result == (
            "<blockquote>\n"
            "<p>Hello</p>\n"
            "<ul>\n"
            "<li>one</li>\n"
            "<li>two</li>\n"
            "</ul>\n"
            "</blockquote>\n"
        )

    def test_include_inside_list_item_keeps_structure(self):
        markdown = self._make({"frag.md": "first\n\nsecond\n"})
        result = markdown("- !include frag.md\n")
        assert result == (
            "<ul>\n" "<li>\n" "<p>first</p>\n" "<p>second</p>\n" "</li>\n" "</ul>\n"
        )

    def test_include_cycle_raises(self):
        from marko.ext.include import IncludeCycle

        fragments = {
            "a.md": "A\n\n!include b.md\n",
            "b.md": "B\n\n!include a.md\n",
        }
        markdown = self._make(fragments)
        with pytest.raises(IncludeCycle) as exc_info:
            markdown("!include a.md\n")
        assert exc_info.value.target == "a.md"
        assert exc_info.value.chain == ["a.md", "b.md"]

    def test_self_include_raises(self):
        from marko.ext.include import IncludeCycle

        markdown = self._make({"a.md": "!include a.md\n"})
        with pytest.raises(IncludeCycle):
            markdown("!include a.md\n")

    def test_include_cycle_can_be_skipped(self):
        fragments = {
            "a.md": "A\n\n!include b.md\n",
            "b.md": "B\n\n!include a.md\n",
        }
        markdown = self._make(fragments, on_error="skip")
        result = markdown("!include a.md\n")
        assert "<p>A</p>" in result
        assert "<p>B</p>" in result

    def test_missing_target_raises(self):
        from marko.ext.include import IncludeNotFound

        markdown = self._make({})
        with pytest.raises(IncludeNotFound):
            markdown("!include missing.md\n")

    def test_missing_target_can_be_skipped(self):
        markdown = self._make({}, on_error="skip")
        assert markdown("Before\n\n!include missing.md\n\nAfter\n").count("<p>") == 2

    def test_resolver_exception_is_wrapped(self):
        from marko.ext.include import IncludeResolveFailed

        def resolver(target):
            raise RuntimeError("boom")

        markdown = self._make(resolver)
        with pytest.raises(IncludeResolveFailed) as exc_info:
            markdown("!include frag.md\n")
        assert isinstance(exc_info.value.original, RuntimeError)

    def test_bad_resolver_return_is_wrapped(self):
        from marko.ext.include import IncludeResolveFailed

        markdown = self._make(lambda target: 42)
        with pytest.raises(IncludeResolveFailed):
            markdown("!include frag.md\n")

    def test_resolver_exception_can_be_skipped(self):
        def resolver(target):
            raise RuntimeError("boom")

        markdown = self._make(resolver, on_error="skip")
        assert markdown("!include frag.md\n") == ""

    def test_directive_is_ignored_inside_fenced_code(self):
        def resolver(target):
            raise AssertionError("resolver must not be called for code blocks")

        markdown = self._make(resolver)
        result = markdown("```\n!include frag.md\n```\n")
        assert "!include frag.md" in result

    def test_directive_is_ignored_inside_indented_code(self):
        def resolver(target):
            raise AssertionError("resolver must not be called for code blocks")

        markdown = self._make(resolver)
        result = markdown("    !include frag.md\n")
        assert "!include frag.md" in result

    def test_markdown_renderer_flattens_fragments(self):
        from marko.ext.include import make_extension
        from marko.md_renderer import MarkdownRenderer

        markdown = Markdown(
            renderer=MarkdownRenderer,
            extensions=[make_extension({"frag.md": "Hello\n"}.__getitem__)],
        )
        result = markdown("Before\n\n!include frag.md\n")
        assert "!include" not in result
        assert "Hello" in result

    def test_invalid_on_error(self):
        from marko.ext.include import make_extension

        with pytest.raises(ValueError):
            make_extension(lambda t: "", on_error="bogus")


class TestGFMTableLlistElement:
    def setup_method(self):
        from marko import Markdown
        from marko.ext.gfm import GFM
        from marko.md_renderer import MarkdownRenderer

        self.md_md = Markdown(renderer=MarkdownRenderer, extensions=[GFM])

    def test_render_table_in_list_indented(self):
        text = "- Item.\n\n  |foo|bar|\n  |---|---|\n  |foo|bar|"
        md = self.md_md(text)
        assert md == "- Item.\n\n  | foo | bar |\n  | --- | --- |\n  | foo | bar |\n"
