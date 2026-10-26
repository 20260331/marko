"""
Conditional content extension
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Render a single source document differently depending on a set of
variables, such as the product version being documented.

The conditional directives are line based and use the familiar Jinja-style
form. Only one branch is ever parsed as normal Markdown, so references,
footnotes and inline syntax in the other branches can never pollute the
result::

    {% if version == "pro" %}
    This paragraph only ships with the Pro docs. It may safely use
    [pro-only references][admin] and footnotes[^pro].

    [^pro]: a footnote that the free build never sees.
    {% elif version == "free" %}
    This paragraph ships with the Free docs.
    {% else %}
    This paragraph ships with every other build.
    {% endif %}

The directives may be nested. Every directive is checked -- including the
ones in branches that are not selected -- for balanced nesting and valid
expressions. A malformed document raises
:class:`ConditionalSyntaxError` while parsing.

The accepted expression grammar is intentionally small and evaluated
without :func:`eval`: names, string/number/boolean/``None`` constants
(plus the Jinja-style lowercase ``true``/``false``/``null``),
``and``/``or``/``not``, parentheses, the comparisons ``==``, ``!=``,
``<``, ``<=``, ``>``, ``>=``, ``in``/``not in`` as well as tuple/list
literals. Names missing from the variables evaluate as false.

Usage::

    from marko import Markdown
    from marko.ext.conditional import make_extension

    markdown = Markdown(extensions=[make_extension({"version": "pro"})])
    print(markdown(text))

    # The same instance can render another build by swapping the variables.
    markdown.parser.conditional_variables["version"] = "free"
    print(markdown(text))
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING, Any

from marko import block
from marko.helpers import MarkoExtension

if TYPE_CHECKING:
    from marko.source import Source

__all__ = ("ConditionalBlock", "ConditionalSyntaxError", "make_extension")


class ConditionalSyntaxError(ValueError):
    """Raised when a conditional directive is unbalanced or has an
    invalid expression."""


# ---------------------------------------------------------------------------
# Expression handling
# ---------------------------------------------------------------------------

_ALLOWED_EXPRESSION_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Tuple,
    ast.List,
)

_ALLOWED_CONSTANT_TYPES = (str, int, float, bool, type(None))


def _compile_expression(expression: str) -> ast.Expression:
    """Parse an expression and make sure it only uses the safe grammar."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ConditionalSyntaxError(
            f"Invalid conditional expression {expression!r}: {exc.msg}"
        ) from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, _ALLOWED_CONSTANT_TYPES):
                raise ConditionalSyntaxError(
                    f"Unsupported constant in conditional expression " f"{expression!r}"
                )
        elif not isinstance(node, _ALLOWED_EXPRESSION_NODES):
            raise ConditionalSyntaxError(
                f"Unsupported syntax in conditional expression {expression!r}"
            )
    return tree


def _evaluate(node: ast.AST, variables: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, variables) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _evaluate(node.operand, variables)
        if isinstance(node.op, ast.USub):
            return -_evaluate(node.operand, variables)
        return +_evaluate(node.operand, variables)
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, variables)
            if isinstance(op, ast.Eq):
                matched = left == right
            elif isinstance(op, ast.NotEq):
                matched = left != right
            elif isinstance(op, ast.Lt):
                matched = left < right
            elif isinstance(op, ast.LtE):
                matched = left <= right
            elif isinstance(op, ast.Gt):
                matched = left > right
            elif isinstance(op, ast.GtE):
                matched = left >= right
            elif isinstance(op, ast.In):
                matched = left in right
            else:  # ast.NotIn
                matched = left not in right
            if not matched:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        # Jinja-style lowercase literals.
        if node.id == "true":
            return True
        if node.id == "false":
            return False
        if node.id == "null":
            return None
        # Unknown feature flags default to false, Jinja style.
        return variables.get(node.id, False)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(elt, variables) for elt in node.elts)
    if isinstance(node, ast.List):
        return [_evaluate(elt, variables) for elt in node.elts]
    # Validation in ``_compile_expression`` makes this unreachable.
    raise ConditionalSyntaxError(  # pragma: no cover
        f"Unsupported expression node {type(node).__name__}"
    )


# ---------------------------------------------------------------------------
# Block element
# ---------------------------------------------------------------------------

# A directive occupies a whole line: ``{% if expr %}`` / ``{% elif expr %}``
# / ``{% else %}`` / ``{% endif %}``. The source prefix (block quote markers,
# list indentation) is stripped by ``Source.expect_re`` before matching.
_DIRECTIVE_RE = re.compile(
    r" {,3}\{%[^\S\n]*(if|elif|else|endif)\b(.*?)%\}[^\S\n]*\n?",
    flags=re.MULTILINE,
)
_FENCE_RE = re.compile(r" {,3}(`{3,}|~{3,})")


class _Boundary:
    """Internal marker returned by ``ConditionalBlock.parse`` when a
    selected-branch parse reaches an ``elif``/``else``/``endif``
    directive belonging to the enclosing frame. The marker is not
    appended to the AST; ``ConditionalParserMixin.parse_source`` stops
    and hands it to the frame through ``source.context.boundary``."""

    def __init__(
        self, kind: str, tree: ast.Expression | None, expression: str = ""
    ) -> None:
        self.kind = kind
        self.tree = tree
        self.expression = expression


class ConditionalBlock(block.BlockElement):
    """A block holding the selected branch of an
    ``{% if %}...{% endif %}`` conditional.

    The other branches are consumed during parsing but never turned into
    block elements, so their link reference definitions, footnote
    definitions and inline content stay out of the document.
    """

    priority = 8
    breaks_paragraph = True
    pattern = _DIRECTIVE_RE

    #: Marks instances so the parser can find the enclosing frame.
    _conditional_frame = True
    _prefix = ""

    def __init__(self, branch: str = "if", expression: str | None = None) -> None:
        #: The kind of the selected branch: ``"if"``, ``"elif"`` or ``"else"``.
        self.branch = branch
        #: The raw expression of the selected branch, if any.
        self.expression = expression
        self.children: list[block.BlockElement] = []

    @classmethod
    def match(cls, source: Source) -> re.Match[str] | None:
        return source.expect_re(cls.pattern)

    @classmethod
    def parse(cls, source: Source) -> ConditionalBlock | _Boundary:
        match = source.match
        assert match is not None
        kind = match.group(1)
        expression = match.group(2).strip()
        tree: ast.Expression | None
        if kind == "if":
            tree = cls._require_expression(source, expression, match.start())
            return cls._parse_frame(source, match, tree, expression)
        # ``elif``/``else``/``endif`` either close the enclosing frame or
        # are stray directives without a matching ``{% if %}``.
        if cls._find_frame(source) is None:
            raise ConditionalSyntaxError(
                f"{{% {kind} %}} without a matching {{% if %}} "
                f"at line {cls._line_no(source, match.start())}"
            )
        tree = None
        if kind == "elif":
            tree = cls._require_expression(source, expression, match.start())
        elif expression:
            raise ConditionalSyntaxError(
                f"{{% {kind} %}} takes no expression but got {expression!r} "
                f"at line {cls._line_no(source, match.start())}"
            )
        return _Boundary(kind, tree, expression)

    # ------------------------------------------------------------------
    # Frame parsing
    # ------------------------------------------------------------------

    @classmethod
    def _parse_frame(
        cls,
        source: Source,
        opening_match: re.Match[str],
        tree: ast.Expression,
        expression: str,
    ) -> ConditionalBlock:
        opening_pos = opening_match.start()
        state = cls("if", expression)
        variables = getattr(source.parser, "conditional_variables", {})

        # Consume through the normal API so enclosing states (e.g. list
        # items) switch to their continuation prefix.
        source.consume()

        chosen = bool(_evaluate(tree, variables))
        active = chosen
        seen_else = False
        branch_kind = "if"
        branch_expression: str | None = expression
        while True:
            if active:
                state.branch = branch_kind
                state.expression = branch_expression
                source.context.boundary = None
                with source.under_state(state):
                    state.children = source.parser.parse_source(source)
                boundary = source.context.boundary
                source.context.boundary = None
                if boundary is None:
                    raise ConditionalSyntaxError(
                        f"Unclosed {{% if %}} at line "
                        f"{cls._line_no(source, opening_pos)}"
                    )
                kind = boundary.kind
                next_tree = boundary.tree
                next_expression = boundary.expression or None
            else:
                kind, next_tree, next_expression = cls._skip_branch(source, opening_pos)

            # ``source.pos`` points at the boundary directive line in both
            # paths, so report errors before consuming it.
            boundary_pos = source.pos
            if kind != "endif" and seen_else:
                raise ConditionalSyntaxError(
                    f"{{% {kind} %}} after {{% else %}} at line "
                    f"{cls._line_no(source, boundary_pos)}"
                )
            cls._consume_directive_line(source)

            if kind == "endif":
                return state
            if kind == "else":
                seen_else = True
                branch_kind = "else"
                branch_expression = None
                active = not chosen
            else:  # elif
                branch_kind = "elif"
                branch_expression = next_expression
                active = not chosen and bool(_evaluate(next_tree, variables))
                if active:
                    chosen = True

    @classmethod
    def _skip_branch(
        cls,
        source: Source,
        opening_pos: int,
    ) -> tuple[str, ast.Expression | None, str | None]:
        """Scan a non-selected branch without parsing any of its Markdown.

        All directives encountered are still validated for balanced
        nesting and valid expressions, including those of nested
        conditionals. Scanning stops at the next ``elif``/``else``/
        ``endif`` directive of *this* frame.
        """
        buffer = source._buffer
        pos = source.pos
        prefix = source.prefix
        # One ``seen_else`` flag per nested conditional level.
        nested_stack: list[bool] = []
        fence: tuple[str, int] | None = None
        while pos < len(buffer):
            line_end = buffer.find("\n", pos)
            if line_end < 0:
                line_end = len(buffer)
            line = buffer[pos:line_end]
            next_pos = line_end + 1 if line_end < len(buffer) else line_end
            prefix_len = source.match_prefix(prefix, line)
            if fence is not None:
                if prefix_len >= 0 and cls._matches_closing_fence(
                    line[prefix_len:], fence
                ):
                    fence = None
                pos = next_pos
                continue
            if prefix_len < 0:
                pos = next_pos
                continue
            body = line[prefix_len:]
            fence_match = _FENCE_RE.match(body)
            if fence_match:
                fence = (fence_match.group(1)[0], len(fence_match.group(1)))
                pos = next_pos
                continue
            directive = _DIRECTIVE_RE.match(body)
            if directive:
                kind = directive.group(1)
                raw_expression = directive.group(2).strip()
                boundary_pos = pos + prefix_len
                if kind == "if":
                    # Expressions are compiled even when the surrounding
                    # branch is not selected.
                    cls._require_expression(source, raw_expression, boundary_pos)
                    nested_stack.append(False)
                elif not nested_stack:
                    # Boundary of the frame being scanned.
                    tree: ast.Expression | None = None
                    if kind == "elif":
                        tree = cls._require_expression(
                            source, raw_expression, boundary_pos
                        )
                    elif raw_expression:
                        raise ConditionalSyntaxError(
                            f"{{% {kind} %}} takes no expression but got "
                            f"{raw_expression!r} at line "
                            f"{cls._line_no(source, boundary_pos)}"
                        )
                    source.pos = pos
                    return kind, tree, (raw_expression if kind == "elif" else None)
                else:
                    cls._validate_nested_directive(
                        source, kind, raw_expression, nested_stack, boundary_pos
                    )
            pos = next_pos
        raise ConditionalSyntaxError(
            f"Unclosed {{% if %}} at line {cls._line_no(source, opening_pos)}"
        )

    @classmethod
    def _validate_nested_directive(
        cls,
        source: Source,
        kind: str,
        raw_expression: str,
        nested_stack: list[bool],
        pos: int,
    ) -> None:
        """Validate a directive belonging to a conditional nested inside
        a non-selected branch."""
        line_no = cls._line_no(source, pos)
        if kind == "endif":
            if raw_expression:
                raise ConditionalSyntaxError(
                    f"{{% endif %}} takes no expression but got "
                    f"{raw_expression!r} at line {line_no}"
                )
            nested_stack.pop()
            return
        if kind == "else":
            if raw_expression:
                raise ConditionalSyntaxError(
                    f"{{% else %}} takes no expression but got "
                    f"{raw_expression!r} at line {line_no}"
                )
            if nested_stack[-1]:
                raise ConditionalSyntaxError(
                    f"Duplicate {{% else %}} at line {line_no}"
                )
            nested_stack[-1] = True
            return
        # elif
        cls._require_expression(source, raw_expression, pos)
        if nested_stack[-1]:
            cls._raise_after_else(source, kind, pos)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _find_frame(cls, source: Source) -> ConditionalBlock | None:
        for state in reversed(source._states):
            if getattr(state, "_conditional_frame", False):
                return state  # type: ignore[return-value]
        return None

    @staticmethod
    def _line_no(source: Source, pos: int) -> int:
        return source._buffer.count("\n", 0, pos) + 1

    @classmethod
    def _require_expression(
        cls, source: Source, expression: str, pos: int
    ) -> ast.Expression:
        if not expression:
            raise ConditionalSyntaxError(
                f"Missing expression in conditional directive at line "
                f"{cls._line_no(source, pos)}"
            )
        try:
            return _compile_expression(expression)
        except ConditionalSyntaxError:
            raise ConditionalSyntaxError(
                f"Invalid conditional expression {expression!r} at line "
                f"{cls._line_no(source, pos)}"
            ) from None

    @staticmethod
    def _matches_closing_fence(line: str, fence: tuple[str, int]) -> bool:
        char, length = fence
        pattern = r" {,3}" + re.escape(char) + "{" + str(length) + r",}[^\S\n]*$"
        return re.match(pattern, line) is not None

    @classmethod
    def _raise_after_else(cls, source: Source, kind: str, pos: int) -> None:
        raise ConditionalSyntaxError(
            f"{{% {kind} %}} after {{% else %}} at line " f"{cls._line_no(source, pos)}"
        )

    @staticmethod
    def _consume_directive_line(source: Source) -> None:
        """Advance past the directive line at ``source.pos`` (its trailing
        newline included), whether or not it was matched through the
        source prefix machinery."""
        buffer = source._buffer
        line_end = buffer.find("\n", source.pos)
        if line_end < 0:
            source.pos = len(buffer)
        else:
            source.pos = line_end + 1
            # Keep enclosing state prefixes (list items, quotes) in sync,
            # just like ``Source.consume`` does.
            source._update_prefix()
        source.match = None


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------


class ConditionalParserMixin:
    """Holds the variables used to evaluate conditional expressions and
    stops block parsing when a conditional frame reaches its boundary
    directive."""

    def __init__(self) -> None:
        super().__init__()
        self.conditional_variables: dict[str, Any] = {}

    def parse_source(self, source: Source) -> list[block.BlockElement]:
        """Mirror :meth:`Parser.parse_source`, but return the parsed
        children (without consuming anything) when the current frame's
        ``elif``/``else``/``endif`` boundary is reached."""
        element_list = self._build_block_element_list()  # type: ignore[attr-defined]
        ast: list[block.BlockElement] = []
        while not source.exhausted:
            start = source._current_pos
            for ele_type in element_list:
                if ele_type.match(source):
                    result = ele_type.parse(source)
                    if isinstance(result, _Boundary):
                        # Do not consume the boundary; let the frame handle it.
                        source.context.boundary = result
                        return ast
                    end = source.pos
                    if not hasattr(result, "priority"):
                        result = ele_type(result)  # type: ignore[operator]
                    if result.source_span is None:
                        result.source_span = (start, end)
                    ast.append(result)
                    break
            else:
                break
        return ast


def make_extension(variables: dict[str, Any] | None = None) -> MarkoExtension:
    """Create the conditional content extension.

    :param variables: initial variables available to conditional
        expressions. They can later be read or replaced through
        ``markdown.parser.conditional_variables`` to render another
        build with the same parser.
    """
    initial = dict(variables or {})

    class _ConfiguredConditionalParserMixin(ConditionalParserMixin):
        def __init__(self) -> None:
            super().__init__()
            self.conditional_variables.update(initial)

    return MarkoExtension(
        elements=[ConditionalBlock],
        parser_mixins=[_ConfiguredConditionalParserMixin],
    )
