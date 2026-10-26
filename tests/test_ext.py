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


def _conditional_markdown(variables=None, *extensions):
    from marko.ext.conditional import make_extension

    return Markdown(extensions=[*extensions, make_extension(variables)])


class TestConditional:
    def test_if_selected(self):
        text = "{% if pro %}\nPro text.\n{% endif %}\n"
        assert _conditional_markdown({"pro": True})(text) == "<p>Pro text.</p>\n"

    def test_if_not_selected(self):
        text = "{% if pro %}\nPro text.\n{% endif %}\n"
        assert _conditional_markdown({"pro": False})(text) == ""

    @pytest.mark.parametrize(
        ("version", "expected"),
        [("pro", "Pro"), ("free", "Free"), ("trial", "Other")],
    )
    def test_elif_else_chain(self, version, expected):
        text = (
            '{% if version == "pro" %}\nPro\n'
            '{% elif version == "free" %}\nFree\n'
            "{% else %}\nOther\n{% endif %}\n"
        )
        result = _conditional_markdown({"version": version})(text)
        assert result == f"<p>{expected}</p>\n"

    def test_no_branch_selected_is_empty(self):
        text = '{% if version == "pro" %}\nPro\n{% endif %}\n'
        assert _conditional_markdown({"version": "free"})(text) == ""

    def test_undefined_variable_is_falsy(self):
        text = "{% if flag %}\nyes\n{% else %}\nno\n{% endif %}\n"
        assert _conditional_markdown()(text) == "<p>no</p>\n"

    def test_directive_breaks_paragraph(self):
        text = "Intro\n{% if pro %}\nPro\n{% endif %}\n"
        result = _conditional_markdown({"pro": True})(text)
        assert result == "<p>Intro</p>\n<p>Pro</p>\n"

    @pytest.mark.parametrize(
        ("expression", "selected"),
        [
            ("pro and not beta", True),
            ('beta or version == "pro"', True),
            ("(pro or beta) and not archived", True),
            ('version in ("pro", "trial")', True),
            ('version not in ("free",)', True),
            ("users >= 10", True),
            ("true", True),
            ("false", False),
            ("version != null", True),
            ("missing", False),
        ],
    )
    def test_expression_grammar(self, expression, selected):
        text = "{% if " + expression + " %}\nyes\n{% endif %}\n"
        result = _conditional_markdown(
            {
                "pro": True,
                "beta": False,
                "archived": False,
                "version": "pro",
                "users": 100,
            }
        )(text)
        assert result == ("<p>yes</p>\n" if selected else "")

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os')",
            "version.upper()",
            "obj.attr",
            "version if pro else other",
            "lambda: 1",
            "1 + 2",
        ],
    )
    def test_unsafe_expression_rejected(self, expression):
        from marko.ext.conditional import ConditionalSyntaxError

        text = "{% if " + expression + " %}\n{% endif %}\n"
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown({"pro": True})(text)

    def test_nested_conditionals(self):
        text = (
            "{% if a %}\nA\n{% if b %}\nB\n{% endif %}\n" "{% else %}\nC\n{% endif %}\n"
        )
        markdown = _conditional_markdown({"a": True, "b": True})
        assert markdown(text).split() == ["<p>A</p>", "<p>B</p>"]
        markdown = _conditional_markdown({"a": True, "b": False})
        assert markdown(text).split() == ["<p>A</p>"]
        markdown = _conditional_markdown({"a": False, "b": True})
        assert markdown(text).split() == ["<p>C</p>"]

    def test_nested_branch_in_skipped_branch(self):
        text = (
            "{% if a %}\nA\n{% else %}\n"
            "{% if b %}\nB\n{% else %}\nC\n{% endif %}\n"
            "{% endif %}\n"
        )
        assert _conditional_markdown({"a": False, "b": True})(text).split() == [
            "<p>B</p>"
        ]
        assert _conditional_markdown({"a": False, "b": False})(text).split() == [
            "<p>C</p>"
        ]

    def test_footnote_definition_in_skipped_branch_does_not_resolve(self):
        text = (
            "[^maybe]\n\n"
            "{% if false %}\n[^maybe]: defined only in the skipped branch.\n"
            "{% endif %}\n"
        )
        markdown = _conditional_markdown({}, "footnote")
        result = markdown(text)
        assert "[^maybe]" in result
        assert "skipped branch" not in result

    def test_footnote_definition_chosen_per_branch(self):
        text = (
            "See note[^v].\n\n"
            "{% if version == 'pro' %}\n[^v]: Pro only.\n"
            "{% else %}\n[^v]: Free only.\n{% endif %}\n"
        )
        markdown = _conditional_markdown({"version": "pro"}, "footnote")
        pro = markdown(text)
        assert "Pro only." in pro
        assert "Free only." not in pro
        markdown.parser.conditional_variables["version"] = "free"
        free = markdown(text)
        assert "Free only." in free
        assert "Pro only." not in free

    def test_link_reference_in_skipped_branch_does_not_resolve(self):
        text = (
            "[only hidden][x]\n\n"
            "{% if false %}\n[x]: https://hidden.example\n{% endif %}\n"
        )
        result = _conditional_markdown({})(text)
        assert "[only hidden][x]" in result
        assert "https://hidden.example" not in result

    def test_link_reference_chosen_per_branch(self):
        text = (
            "[go][x]\n\n"
            "{% if pro %}\n[x]: https://pro.example\n"
            "{% else %}\n[x]: https://free.example\n{% endif %}\n"
        )
        markdown = _conditional_markdown({"pro": True})
        assert "https://pro.example" in markdown(text)
        markdown.parser.conditional_variables["pro"] = False
        assert "https://free.example" in markdown(text)

    def test_directives_inside_fenced_code_are_ignored_when_skipping(self):
        text = "{% if false %}\n```\n{% if fake %}\n{% endif %}\n```\n" "{% endif %}\n"
        assert _conditional_markdown()(text) == ""

    def test_indented_code_keeps_directive_literal(self):
        text = "{% if true %}\n    {% if nope %}\n{% endif %}\n"
        result = _conditional_markdown()(text)
        assert "{% if nope %}" in result
        assert "<code>" in result

    def test_inline_directive_is_literal(self):
        result = _conditional_markdown()("a {% if x %} b\n")
        assert result == "<p>a {% if x %} b</p>\n"

    def test_conditional_inside_block_quote(self):
        text = "> {% if pro %}\n> Pro\n> {% else %}\n> Free\n> {% endif %}\n"
        assert "<p>Pro</p>" in _conditional_markdown({"pro": True})(text)
        assert "<p>Free</p>" in _conditional_markdown({"pro": False})(text)

    def test_conditional_inside_list_item(self):
        text = "- {% if pro %}\n  Pro\n  {% else %}\n  Free\n  {% endif %}\n"
        assert "<p>Pro</p>" in _conditional_markdown({"pro": True})(text)
        assert "<p>Free</p>" in _conditional_markdown({"pro": False})(text)

    def test_markdown_renderer_outputs_selected_branch(self):
        from marko.md_renderer import MarkdownRenderer

        markdown = Markdown(
            renderer=MarkdownRenderer,
            extensions=[],
        )
        from marko.ext.conditional import make_extension

        markdown.use(make_extension({"pro": True}))
        result = markdown("{% if pro %}\nPro text.\n{% else %}\nhidden\n{% endif %}\n")
        assert result == "Pro text.\n"
        assert "hidden" not in result

    def test_variables_can_be_swapped_between_parses(self):
        markdown = _conditional_markdown({"version": "free"})
        text = '{% if version == "pro" %}\npro\n{% else %}\nfree\n{% endif %}\n'
        assert markdown(text) == "<p>free</p>\n"
        markdown.parser.conditional_variables["version"] = "pro"
        assert markdown(text) == "<p>pro</p>\n"

    @pytest.mark.parametrize(
        "text",
        [
            "{% if x %}\nbody",
            "{% endif %}\n",
            "{% else %}\n{% endif %}\n",
            "{% if x %}\n{% else %}\n{% else %}\n{% endif %}\n",
            "{% if x %}\n{% else %}\n{% elif y %}\n{% endif %}\n",
            "{% if x %}\n{% elif %}\n{% endif %}\n",
            "{% endif x %}\n",
            "{% else x %}\n",
        ],
    )
    def test_malformed_directives_raise(self, text):
        from marko.ext.conditional import ConditionalSyntaxError

        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown({"x": True, "y": True})(text)

    def test_invalid_expression_checked_in_skipped_branch(self):
        # The elif branch is never selected nor parsed as Markdown, yet its
        # expression must still be validated.
        from marko.ext.conditional import ConditionalSyntaxError

        text = "{% if false %}\n{% elif version == %}\n{% endif %}\n"
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown()(text)

    def test_invalid_expression_checked_in_nested_skipped_block(self):
        from marko.ext.conditional import ConditionalSyntaxError

        text = "{% if false %}\n{% if version == %}\n{% endif %}\n{% endif %}\n"
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown()(text)

    def test_nesting_checked_inside_skipped_branch(self):
        from marko.ext.conditional import ConditionalSyntaxError

        # The nested ``{% if %}`` is never closed.
        text = "{% if false %}\n{% if x %}\n{% endif %}\n"
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown()(text)
        # A nested ``{% endif %}`` is one too many.
        text = "{% if false %}\n{% endif %}\n{% endif %}\n"
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown()(text)
        # Duplicate else inside a skipped nested block.
        text = (
            "{% if false %}\n{% if x %}\n{% else %}\n{% else %}\n"
            "{% endif %}\n{% endif %}\n"
        )
        with pytest.raises(ConditionalSyntaxError):
            _conditional_markdown()(text)

    def test_ast_contains_only_selected_branch(self):
        from marko.ast_renderer import ASTRenderer

        markdown = Markdown(
            renderer=ASTRenderer,
        )
        from marko.ext.conditional import make_extension

        markdown.use(make_extension({"pro": True}))
        ast = markdown("{% if pro %}\nPro\n{% else %}\nFree\n{% endif %}\n")
        block = ast["children"][0]
        assert block["element"] == "conditional_block"
        assert block["branch"] == "if"
        assert block["children"][0]["children"][0]["children"] == "Pro"


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
