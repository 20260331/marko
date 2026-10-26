# mypy: disable-error-code="no-redef"
"""
Include extension
~~~~~~~~~~~~~~~~~

Include external markdown fragments, with the fragment content supplied by a
caller-provided resolver. Useful for documentation sites that assemble pages
from multiple files: simply concatenating the sources would let link reference
definitions and footnotes with the same label collide, and fragments embedded
inside list items or block quotes would lose their surrounding structure.

The include directive is a line-level block::

    !include chapter-1.md

An optional pair of matching single or double quotes is allowed when the
target contains whitespace::

    !include "chapter 1.md"

Each included document is parsed independently, so it owns a private symbol
scope: its link reference definitions and footnotes never leak into the
including document or into sibling fragments, even when labels are identical.
Includes may be nested to any depth. The directive has no effect inside code
blocks or code spans.

Usage::

    from pathlib import Path

    from marko import Markdown
    from marko.ext.include import make_extension


    def resolver(target: str) -> str | None:
        path = Path("docs") / target
        if not path.is_file():
            return None
        return path.read_text()


    markdown = Markdown(extensions=[make_extension(resolver)])
    print(markdown("!include intro.md\n"))

:param resolver: a callable taking the target string and returning the
    markdown text of the fragment. Returning ``None`` means the target could
    not be found.
:param on_error: ``"raise"`` (the default) to raise an :class:`IncludeError`
    when a fragment cannot be loaded or an include cycle is detected, or
    ``"skip"`` to silently render the directive as empty content.

Cycle detection is performed against the raw target strings on the active
include chain, so a resolver should canonicalize targets (e.g. resolve
relative paths) if fragments may refer to each other through different
spellings.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from marko import block
from marko.helpers import MarkoExtension
from marko.source import Source

if TYPE_CHECKING:
    from marko.parser import Parser

Resolver = Callable[[str], "str | None"]


class IncludeError(Exception):
    """Base class for failures while resolving an include directive."""

    def __init__(self, target: str, chain: Sequence[str]) -> None:
        self.target = target
        self.chain = list(chain)

    @property
    def path(self) -> str:
        return " -> ".join([*self.chain, self.target])


class IncludeNotFound(IncludeError):
    """Raised when the resolver returns ``None`` for a target."""

    def __str__(self) -> str:
        return f"The include target {self.target!r} could not be found."


class IncludeResolveFailed(IncludeError):
    """Raised when the resolver raises an exception or returns a bad value."""

    def __init__(self, target: str, chain: Sequence[str], original: Exception) -> None:
        super().__init__(target, chain)
        self.original = original

    def __str__(self) -> str:
        return f"Failed to resolve include target {self.target!r}: {self.original!s}"


class IncludeCycle(IncludeError):
    """Raised when an include chain refers back to one of its ancestors."""

    def __init__(self, target: str, chain: Sequence[str]) -> None:
        super().__init__(target, chain)

    def __str__(self) -> str:
        return f"Detected a circular include: {self.path}."


class IncludeParserMixin:
    """Holds the resolver configuration and the active include chain."""

    if TYPE_CHECKING:
        include_resolver: Resolver
        include_on_error: str
        _include_chain: list[str]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._include_chain = []


class Include(block.BlockElement):
    """The ``!include <target>`` block directive.

    The element is rendered transparently, i.e. its output is the rendered
    fragment children, so it may appear inside list items, block quotes and
    other containers without breaking their structure.
    """

    pattern = re.compile(r"[ \t]{0,3}!include[ \t]+(.+?)[ \t]*$\n?", re.MULTILINE)
    priority = 6
    breaks_paragraph = True

    def __init__(self, target: str) -> None:
        self.target = target
        self.children: list[block.BlockElement] = []

    @classmethod
    def match(cls, source: Source) -> re.Match[str] | None:
        return source.expect_re(cls.pattern)

    @classmethod
    def parse(cls, source: Source) -> Include:
        assert source.match is not None
        target = cls._parse_target(source.match.group(1))
        # Consume the directive line before resolving so the rest of the
        # document can still be parsed when ``on_error`` is ``"skip"``.
        source.consume()
        node = cls(target)
        parser = cast("IncludeParserMixin", source.parser)
        on_error = parser.include_on_error
        chain: list[str] = parser._include_chain

        if target in chain:
            if on_error == "skip":
                return node
            raise IncludeCycle(target, chain)

        try:
            content = parser.include_resolver(target)
            if content is not None and not isinstance(content, str):
                raise TypeError(
                    "The include resolver must return a string or None, "
                    f"got {type(content).__name__}."
                )
        except IncludeError:
            raise
        except Exception as e:
            if on_error == "skip":
                return node
            raise IncludeResolveFailed(target, chain, e) from e
        if content is None:
            if on_error == "skip":
                return node
            raise IncludeNotFound(target, chain)

        chain.append(target)
        try:
            node.children = cls._load_fragment(cast("Parser", parser), source, content)
        except IncludeError:
            if on_error == "skip":
                return node
            raise
        finally:
            chain.pop()
        return node

    @staticmethod
    def _parse_target(raw: str) -> str:
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            return raw[1:-1]
        return raw

    @classmethod
    def _load_fragment(
        cls, parser: Parser, parent_source: Source, content: str
    ) -> list[block.BlockElement]:
        """Parse ``content`` into block elements.

        A fresh :class:`Source` with a fresh document root gives the fragment
        its private link-reference/footnote scope. The parent container
        states (lists, quotes, ...) are shadowed with neutralized prefixes so
        the fragment blocks are parsed as if written in place and graft into
        the surrounding structure.
        """
        sub_source = Source(content)
        sub_source.parser = parser
        sub_root = parser.block_elements["Document"]()
        sub_source.push_state(sub_root)
        for state in parent_source._states[1:]:
            shadow = copy.copy(state)
            shadow._prefix = ""
            if hasattr(shadow, "_second_prefix"):
                shadow._second_prefix = ""
            sub_source.push_state(shadow)

        children = parser.parse_source(sub_source)
        sub_root.children = children
        # Inline parsing must run against the fragment source so that link
        # references resolve within the fragment's own scope.
        parser.parse_inline(sub_root, sub_source)
        cls._promote_footnotes(parent_source.root, sub_root, children)
        return children

    @classmethod
    def _promote_footnotes(
        cls,
        parent_root: block.BlockElement,
        sub_root: block.BlockElement,
        children: list[block.BlockElement],
    ) -> None:
        """Give the fragment footnotes a unique label prefix and merge them
        into the including document, so identical footnote labels never
        collide across fragments."""
        footnotes: dict[str, Any] | None = getattr(sub_root, "footnotes", None)
        if not footnotes:
            return
        scope = getattr(parent_root, "_include_scope_count", 0)
        setattr(parent_root, "_include_scope_count", scope + 1)
        prefix = f"include{scope}-"
        cls._prefix_footnote_labels(children, prefix)
        for label in list(footnotes):
            footnotes[prefix + label] = footnotes.pop(label)
        parent_footnotes: dict[str, Any] = getattr(parent_root, "footnotes")
        parent_footnotes.update(footnotes)

    @classmethod
    def _prefix_footnote_labels(cls, nodes: Sequence[Any], prefix: str) -> None:
        for node in nodes:
            if not hasattr(node, "get_type"):
                continue
            if node.get_type(snake_case=True) in ("footnote_ref", "footnote_def"):
                node.label = prefix + node.label
            children = getattr(node, "children", None)
            if isinstance(children, Sequence) and not isinstance(children, str):
                cls._prefix_footnote_labels(children, prefix)


def make_extension(resolver: Resolver, *, on_error: str = "raise") -> MarkoExtension:
    """Create the include extension.

    :param resolver: callable mapping an include target to markdown text or
        ``None`` when the target is missing.
    :param on_error: either ``"raise"`` (raise :class:`IncludeError`) or
        ``"skip"`` (drop directives that cannot be resolved).
    """
    if on_error not in ("raise", "skip"):
        raise ValueError('on_error must be either "raise" or "skip".')
    parser_mixin = type(
        "ConfiguredIncludeParserMixin",
        (IncludeParserMixin,),
        {
            "include_resolver": staticmethod(resolver),
            "include_on_error": on_error,
        },
    )
    return MarkoExtension(parser_mixins=[parser_mixin], elements=[Include])
