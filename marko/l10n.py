r"""
Localization catalog for translating Markdown documents.
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Translating Markdown by handing the raw source to a translator is risky: the
translator may accidentally alter links, emphasis or code structure. Replacing
:class:`~marko.inline.RawText` nodes one by one, on the other hand, loses the
sentence context whenever a sentence contains inline markup.

This module exports a *localization catalog* that keeps both concerns apart:

* translatable text is exported sentence by sentence (one catalog entry per
  paragraph, heading or table cell), with structural inline elements replaced
  by numbered placeholders such as ``{0}``, ``{1}``;
* the protected structure (code spans, link destinations, HTML tags, line
  breaks, ...) stays in the AST and is never shown as editable Markdown;
* translatable content nested inside emphasis, links, images or GFM
  strikethrough is exported as a nested segment;
* when translations are applied, every segment is re-parsed and its structure
  skeleton is compared with the original one. A translation that drops, adds
  or modifies structure is rejected with :class:`CatalogError`.

Placeholders may be reordered (word order differs between languages) but every
placeholder must appear exactly once. Literal braces are escaped by doubling
them: ``{{`` and ``}}``.

Example::

    import json
    from marko.l10n import Catalog

    catalog = Catalog.export(markdown_text)
    data = catalog.to_dict()
    with open("messages.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ... translators fill in the "translation" fields ...

    catalog = Catalog.from_dict(json.load(open("messages.json", encoding="utf-8")))
    translated = catalog.apply(markdown_text)

When extensions that introduce new inline elements (such as ``gfm``) are used,
the configured :class:`~marko.Markdown` instance must be passed so that the
matching parser and Markdown renderer are available::

    import marko
    from marko.md_renderer import MarkdownRenderer

    md = marko.Markdown(renderer=MarkdownRenderer, extensions=["gfm"])
    catalog = Catalog.export(text, markdown=md)
    translated = catalog.apply(text, markdown=md)
"""

from __future__ import annotations

import copy
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from . import block, inline
from .md_renderer import MarkdownRenderer
from .parser import Parser
from .source import Source

if TYPE_CHECKING:
    from . import Markdown
    from .element import Element

CATALOG_VERSION = 1

#: Inline elements whose children are themselves translatable text.
TRANSLATABLE_CONTAINERS = frozenset(
    {"emphasis", "strong_emphasis", "link", "image", "strikethrough"}
)

# "{{" and "}}" are literal braces, "{digits}" is a placeholder, and any
# remaining brace sequence is malformed.
_TOKEN_RE = re.compile(r"\{\{|\}\}|\{(\d+)\}|\{[^{}]*\}|[{}]")
_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}")


class CatalogError(ValueError):
    """Raised when a translation cannot be applied because it does not preserve
    the Markdown structure of the source segment.

    The :attr:`entry_id` attribute identifies the offending catalog entry (and
    possibly the nested placeholder path, e.g. ``"3.0"``).
    """

    def __init__(self, message: str, entry_id: str | None = None) -> None:
        self.entry_id = entry_id
        prefix = f"[catalog entry {entry_id}] " if entry_id is not None else ""
        super().__init__(prefix + message)


@dataclass
class Segment:
    """A translatable text unit: the :attr:`source` string interleaved with
    numbered placeholders declared in :attr:`placeholders`, and the (possibly
    empty) :attr:`translation` provided by the translator."""

    source: str
    placeholders: list[Placeholder] = field(default_factory=list)
    translation: str = ""


@dataclass
class Placeholder:
    """A protected inline structure embedded in a :class:`Segment`.

    :param name: placeholder number as referenced by ``"{name}"``.
    :param type: snake-case element type, e.g. ``"link"`` or ``"code_span"``.
    :param attrs: protected attributes describing the structure (``dest``,
        ``title``, ``content`` ...). They are exported as translator hints and
        are always verified against the source document when applying.
    :param segment: nested translatable segment for container elements
        (emphasis, links, images, strikethrough), or ``None``.
    """

    name: str
    type: str
    attrs: dict[str, Any] = field(default_factory=dict)
    segment: Segment | None = None


@dataclass
class CatalogEntry:
    """A catalog entry bound to one inline-bearing block of the document."""

    id: str
    block: str
    segment: Segment
    level: int | None = None

    @property
    def source(self) -> str:
        """The source text with placeholders."""
        return self.segment.source

    @property
    def translation(self) -> str:
        """The translated text with placeholders. Empty means untranslated."""
        return self.segment.translation

    @translation.setter
    def translation(self, value: str) -> None:
        self.segment.translation = value

    @property
    def placeholders(self) -> list[Placeholder]:
        return self.segment.placeholders


class Catalog:
    """A collection of :class:`CatalogEntry` objects.

    Use :meth:`export` to build a catalog from Markdown text or a parsed
    document, :meth:`to_dict`/:meth:`from_dict` to exchange it with translators
    (e.g. as JSON), and :meth:`apply` to render a translated Markdown document.
    """

    def __init__(self, entries: Sequence[CatalogEntry] | None = None) -> None:
        self.entries: list[CatalogEntry] = list(entries or [])

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[CatalogEntry]:
        return iter(self.entries)

    def __getitem__(self, entry_id: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        raise KeyError(entry_id)

    def __contains__(self, entry_id: object) -> bool:
        return any(entry.id == entry_id for entry in self.entries)

    # ------------------------------------------------------------------ export

    @classmethod
    def export(
        cls,
        content: str | block.Document,
        *,
        markdown: Markdown | None = None,
    ) -> Catalog:
        """Export the translatable segments of a Markdown document.

        :param content: Markdown text or a document previously parsed with the
            same parser configuration.
        :param markdown: optional configured :class:`~marko.Markdown` instance.
            Required when the document uses extensions that add inline
            elements or Markdown render rules (e.g. ``gfm``).
        """
        ctx = _Context(markdown)
        document = ctx.parser.parse(content) if isinstance(content, str) else content
        entries: list[CatalogEntry] = []
        for host, path in _walk_hosts(document.children):
            segment = _build_segment(host.children)
            if not _segment_has_text(segment):
                continue
            entries.append(
                CatalogEntry(
                    id=".".join(str(part) for part in path),
                    block=host.get_type(snake_case=True),
                    segment=segment,
                    level=getattr(host, "level", None),
                )
            )
        return cls(entries)

    # ------------------------------------------------------------------- apply

    def apply(
        self,
        content: str | block.Document,
        *,
        markdown: Markdown | None = None,
    ) -> str:
        """Apply the translations and return the translated Markdown text.

        Entries without a translation are left untouched. Every translated
        segment is re-parsed and checked against the source structure before
        the document is rendered; the first violation raises
        :class:`CatalogError` and leaves the document unchanged.

        :param content: the original Markdown text or a parsed document. It
            must match the document the catalog was exported from.
        :param markdown: optional configured :class:`~marko.Markdown` instance,
            matching the one passed to :meth:`export`.
        """
        ctx = _Context(markdown)
        document = ctx.parser.parse(content) if isinstance(content, str) else content
        entries = {entry.id: entry for entry in self.entries}
        with ctx.renderer as renderer:
            renderer.root_node = document
            # Validate and build every translated segment before mutating the
            # document, so a rejected translation leaves it unchanged.
            updates: list[tuple[block.BlockElement, list[inline.InlineElement]]] = []
            for host, path in _walk_hosts(document.children):
                entry = entries.get(".".join(str(part) for part in path))
                if entry is None or not entry.segment.translation:
                    continue
                translated_nodes = _apply_segment(
                    host.children,
                    entry.segment,
                    entry.segment.translation,
                    ctx,
                    entry.id,
                )
                updates.append((host, translated_nodes))
            for host, translated_nodes in updates:
                host.children = translated_nodes
            return renderer.render(document)

    # ------------------------------------------------------------- (de)serialize

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the catalog."""
        return {
            "version": CATALOG_VERSION,
            "entries": [
                {
                    "id": entry.id,
                    "block": entry.block,
                    "level": entry.level,
                    **_segment_to_dict(entry.segment),
                }
                for entry in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        """Create a catalog from the structure produced by :meth:`to_dict`."""
        version = data.get("version")
        if version != CATALOG_VERSION:
            raise ValueError(
                f"Unsupported catalog version: {version!r}, "
                f"expected {CATALOG_VERSION}."
            )
        entries = []
        for item in data.get("entries", []):
            segment = _segment_from_dict(item)
            entries.append(
                CatalogEntry(
                    id=item["id"],
                    block=item.get("block", "paragraph"),
                    segment=segment,
                    level=item.get("level"),
                )
            )
        return cls(entries)


# --------------------------------------------------------------------- helpers


class _Context:
    """Resolved parser and Markdown renderer (extension-aware)."""

    def __init__(self, markdown: Markdown | None) -> None:
        if markdown is None:
            self.parser = Parser()
            self.renderer = MarkdownRenderer()
        else:
            markdown._setup_extensions()
            self.parser = markdown.parser
            renderer_cls = type(
                "_L10nMarkdownRenderer",
                tuple(markdown._renderer_mixins) + (MarkdownRenderer,),
                {},
            )
            self.renderer = renderer_cls()


def _is_code_block(element: Element) -> bool:
    return isinstance(element, (block.CodeBlock, block.FencedCode))


def _walk_hosts(
    nodes: Sequence[Element], path: tuple[int, ...] = ()
) -> Iterator[tuple[block.BlockElement, tuple[int, ...]]]:
    """Yield ``(block, path)`` pairs for blocks that directly host inline
    content, recursing through block-only containers (quotes, lists, table
    rows, footnote definitions, ...)."""
    for index, node in enumerate(nodes):
        if not isinstance(node, block.BlockElement):
            continue
        node_path = path + (index,)
        children = getattr(node, "children", None)
        if not isinstance(children, list) or not children:
            continue
        if not _is_code_block(node) and all(
            isinstance(child, inline.InlineElement) for child in children
        ):
            yield node, node_path
        elif isinstance(children[0], block.BlockElement):
            yield from _walk_hosts(children, node_path)


def _escape_braces(text: str) -> str:
    """Escape literal braces so the source string stays tokenizable."""
    return text.replace("{", "{{").replace("}", "}}")


def _hint_attrs(element: inline.InlineElement) -> dict[str, Any]:
    """Protected attributes exported as translator hints."""
    etype = element.get_type(snake_case=True)
    attrs: dict[str, Any] = {}
    if etype in ("link", "image", "auto_link", "url"):
        attrs["dest"] = getattr(element, "dest")
        title = getattr(element, "title", None)
        if title:
            attrs["title"] = title
    elif etype == "code_span":
        attrs["content"] = element.children
    elif etype == "literal":
        attrs["char"] = element.children
    elif etype == "line_break":
        attrs["soft"] = getattr(element, "soft")
    elif etype == "footnote_ref":
        attrs["label"] = getattr(element, "label")
    return attrs


def _build_segment(children: Sequence[Element]) -> Segment:
    """Turn a sequence of inline elements into a translatable segment."""
    parts: list[str] = []
    placeholders: list[Placeholder] = []
    counter = itertools.count()
    for child in children:
        if isinstance(child, inline.RawText):
            parts.append(_escape_braces(child.children))
            continue
        assert isinstance(child, inline.InlineElement)
        name = str(next(counter))
        etype = child.get_type(snake_case=True)
        child_children = getattr(child, "children", None)
        nested = (
            _build_segment(child_children)
            if etype in TRANSLATABLE_CONTAINERS and isinstance(child_children, list)
            else None
        )
        placeholders.append(
            Placeholder(name=name, type=etype, attrs=_hint_attrs(child), segment=nested)
        )
        parts.append("{" + name + "}")
    return Segment(source="".join(parts), placeholders=placeholders)


def _segment_has_text(segment: Segment) -> bool:
    """Whether a segment contains any non-whitespace translatable text,
    looking through nested translatable containers but not through protected
    leaves such as code spans."""
    if _PLACEHOLDER_RE.sub("", segment.source).strip():
        return True
    return any(
        placeholder.segment is not None and _segment_has_text(placeholder.segment)
        for placeholder in segment.placeholders
    )


def _tokenize(text: str, location: str) -> list[tuple[str, str]]:
    """Split a translated string into ``("text", ...)`` and ``("ph", ...)``
    tokens, unescaping doubled braces."""
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in _TOKEN_RE.finditer(text):
        if match.start() > position:
            tokens.append(("text", text[position : match.start()]))
        token = match.group(0)
        if token == "{{":
            tokens.append(("text", "{"))
        elif token == "}}":
            tokens.append(("text", "}"))
        elif match.group(1) is not None:
            tokens.append(("ph", match.group(1)))
        else:
            raise CatalogError(
                f"malformed placeholder {token!r} at position {match.start()}; "
                "use {0}, {1}, ... for placeholders and {{ or }} for literal "
                "braces.",
                location,
            )
        position = match.end()
    if position < len(text):
        tokens.append(("text", text[position:]))
    return tokens


def _preserve_boundary_whitespace(
    tokens: list[tuple[str, str]], segment: Segment, location: str
) -> list[tuple[str, str]]:
    """Keep leading/trailing whitespace of the source segment.

    Such whitespace can be syntactically significant even though translators
    cannot be expected to reproduce it, e.g. the space separating a GFM
    task-list marker ``[ ]`` from the paragraph text."""
    source_tokens = _tokenize(segment.source, location)
    tokens = list(tokens)
    if not source_tokens:
        return tokens
    if source_tokens[0][0] == "text":
        leading_match = re.match(r"\s*", source_tokens[0][1])
        leading = leading_match.group(0) if leading_match else ""
        if leading and not (
            tokens and tokens[0][0] == "text" and tokens[0][1].startswith(leading)
        ):
            tokens.insert(0, ("text", leading))
    if source_tokens[-1][0] == "text":
        trailing_match = re.search(r"\s*$", source_tokens[-1][1])
        trailing = trailing_match.group(0) if trailing_match else ""
        if trailing and not (
            tokens and tokens[-1][0] == "text" and tokens[-1][1].endswith(trailing)
        ):
            tokens.append(("text", trailing))
    return tokens


def _parse_inline_body(
    body: str, document: block.Document, ctx: _Context
) -> list[inline.InlineElement]:
    """Re-parse a reconstructed inline body in the context of the document."""
    source = Source(body)
    source.parser = ctx.parser
    with source.under_state(document):
        return ctx.parser._parse_inline(body, source)


def _apply_segment(
    original_nodes: Sequence[Element],
    segment: Segment,
    translation: str,
    ctx: _Context,
    location: str,
) -> list[inline.InlineElement]:
    """Validate and render one segment, returning the parsed inline nodes."""
    tokens = _tokenize(translation, location)
    declared = {placeholder.name: placeholder for placeholder in segment.placeholders}
    used = [value for kind, value in tokens if kind == "ph"]
    used_counter: Counter[str] = Counter(used)
    expected_counter: Counter[str] = Counter(declared.keys())
    if used_counter != expected_counter:
        missing = expected_counter - used_counter
        extra = used_counter - expected_counter
        details = []
        if missing:
            details.append(
                "missing placeholder(s): " + ", ".join(sorted(missing.elements()))
            )
        if extra:
            details.append(
                "unexpected placeholder(s): " + ", ".join(sorted(extra.elements()))
            )
        raise CatalogError(
            "placeholders do not match the source segment ("
            + "; ".join(details)
            + ").",
            location,
        )

    # Placeholders are declared in the same order as the structural children.
    structural_originals: list[inline.InlineElement] = [
        child
        for child in original_nodes
        if isinstance(child, inline.InlineElement)
        and not isinstance(child, inline.RawText)
    ]
    if len(structural_originals) != len(segment.placeholders):
        raise CatalogError(
            "the catalog does not match the source document; please re-export "
            "the catalog before applying translations.",
            location,
        )
    original_by_name = {
        placeholder.name: element
        for placeholder, element in zip(segment.placeholders, structural_originals)
    }

    # Keep the boundary whitespace of the source segment. It is usually
    # syntactically meaningful (e.g. the space after a GFM task-list marker
    # ``[ ]`` or the two spaces before a hard line break) even though it is
    # invisible to the translator.
    tokens = _preserve_boundary_whitespace(tokens, segment, location)

    parts: list[str] = []
    for kind, value in tokens:
        if kind == "text":
            parts.append(ctx.renderer.render(inline.RawText(value)))
        else:
            placeholder = declared[value]
            element = original_by_name[value]
            parts.append(
                _render_translated_element(element, placeholder, ctx, location)
            )
    body = "".join(parts)

    # A line break cannot be the last character of a parsed inline body, so a
    # segment whose translation ends with a line-break placeholder needs a
    # throwaway whitespace tail which is removed again after parsing.
    trailing_break = (
        bool(tokens)
        and tokens[-1][0] == "ph"
        and (declared[tokens[-1][1]].type == "line_break")
    )
    if trailing_break:
        body += " "
    document = ctx.renderer.root_node
    assert document is not None, "renderer has no root document"
    candidate = _parse_inline_body(body, document, ctx)
    if trailing_break:
        while candidate and isinstance(candidate[-1], inline.RawText):
            if candidate[-1].children.strip():
                break
            candidate.pop()

    mismatch = _skeleton_diff(
        _skeleton(structural_originals, ctx), _skeleton(candidate, ctx)
    )
    if mismatch:
        raise CatalogError(
            "the translation does not preserve the original Markdown "
            "structure: " + mismatch,
            location,
        )
    return candidate


def _render_translated_element(
    element: inline.InlineElement,
    placeholder: Placeholder,
    ctx: _Context,
    location: str,
) -> str:
    """Render the protected Markdown of one placeholder, substituting the
    nested translation for container elements."""
    if placeholder.segment is None:
        return ctx.renderer.render(element)
    nested_location = f"{location}.{placeholder.name}"
    nested_translation = placeholder.segment.translation or placeholder.segment.source
    children = element.children
    assert isinstance(children, list)
    inner_nodes = _apply_segment(
        children,
        placeholder.segment,
        nested_translation,
        ctx,
        nested_location,
    )
    clone = copy.copy(element)
    clone.children = inner_nodes
    return ctx.renderer.render(clone)


# ------------------------------------------------------------- structure check


def _protected_attrs(
    element: inline.InlineElement, ctx: _Context
) -> tuple[tuple[str, Any], ...]:
    """Attributes that must be identical after re-application."""
    etype = element.get_type(snake_case=True)
    if etype in ("link", "image"):
        return (
            ("dest", getattr(element, "dest")),
            ("title", getattr(element, "title")),
        )
    if etype in ("auto_link", "url"):
        return (("dest", getattr(element, "dest")),)
    if etype == "code_span":
        return (("content", element.children),)
    if etype == "inline_html":
        return (("html", element.children),)
    if etype == "literal":
        return (("char", element.children),)
    if etype == "line_break":
        return (("soft", getattr(element, "soft")),)
    if etype == "footnote_ref":
        return (("label", getattr(element, "label")),)
    if etype in TRANSLATABLE_CONTAINERS:
        # Emphasis, strong, strikethrough: only the nested skeleton matters.
        return ()
    # Unknown extension element: compare its rendered Markdown verbatim.
    return (("markdown", ctx.renderer.render(element)),)


def _skeleton(
    nodes: Sequence[Element], ctx: _Context
) -> frozenset[tuple[tuple[Any, ...], int]]:
    """Structural multiset of an inline tree, ignoring translatable text.

    Placeholders may be reordered by the translator, hence a multiset rather
    than an ordered sequence. Any Markdown structure smuggled in through
    translated text appears here as an extra structural node."""
    result: Counter[tuple[Any, ...]] = Counter()
    for node in nodes:
        if not isinstance(node, inline.InlineElement) or isinstance(
            node, inline.RawText
        ):
            continue
        children = getattr(node, "children", None)
        nested = _skeleton(children, ctx) if isinstance(children, list) else frozenset()
        result[
            (node.get_type(snake_case=True), _protected_attrs(node, ctx), nested)
        ] += 1
    return frozenset(result.items())


def _describe_skeleton_node(node: tuple[Any, ...]) -> str:
    etype, attrs, _nested = node
    if attrs:
        return f"{etype}({', '.join(f'{key}={value!r}' for key, value in attrs)})"
    return etype


def _skeleton_diff(
    original: frozenset[tuple[tuple[Any, ...], int]],
    candidate: frozenset[tuple[tuple[Any, ...], int]],
) -> str | None:
    """Return a human-readable description of the first structural difference,
    or ``None`` if both skeletons match."""
    missing = Counter(dict(original)) - Counter(dict(candidate))
    unexpected = Counter(dict(candidate)) - Counter(dict(original))
    if not missing and not unexpected:
        return None
    messages = []
    if missing:
        messages.append(
            "missing "
            + ", ".join(_describe_skeleton_node(node) for node in missing.elements())
        )
    if unexpected:
        messages.append(
            "unexpected "
            + ", ".join(_describe_skeleton_node(node) for node in unexpected.elements())
        )
    return "; ".join(messages)


# ------------------------------------------------------------- (de)serialize IO

_RESERVED_PLACEHOLDER_KEYS = {"name", "type", "segment"}


def _segment_to_dict(segment: Segment) -> dict[str, Any]:
    return {
        "source": segment.source,
        "translation": segment.translation,
        "placeholders": [
            _placeholder_to_dict(placeholder) for placeholder in segment.placeholders
        ],
    }


def _placeholder_to_dict(placeholder: Placeholder) -> dict[str, Any]:
    result: dict[str, Any] = {"name": placeholder.name, "type": placeholder.type}
    result.update(placeholder.attrs)
    if placeholder.segment is not None:
        result["segment"] = _segment_to_dict(placeholder.segment)
    return result


def _segment_from_dict(data: dict[str, Any]) -> Segment:
    return Segment(
        source=data.get("source", ""),
        translation=data.get("translation", ""),
        placeholders=[
            _placeholder_from_dict(item) for item in data.get("placeholders", [])
        ],
    )


def _placeholder_from_dict(data: dict[str, Any]) -> Placeholder:
    nested = data.get("segment")
    attrs = {
        key: value
        for key, value in data.items()
        if key not in _RESERVED_PLACEHOLDER_KEYS
    }
    return Placeholder(
        name=data["name"],
        type=data["type"],
        attrs=attrs,
        segment=_segment_from_dict(nested) if nested else None,
    )


# ---------------------------------------------------------------- module-level API


def export_catalog(
    content: str | block.Document, *, markdown: Markdown | None = None
) -> Catalog:
    """Convenience wrapper for :meth:`Catalog.export`."""
    return Catalog.export(content, markdown=markdown)


def apply_translations(
    content: str | block.Document,
    catalog: Catalog,
    *,
    markdown: Markdown | None = None,
) -> str:
    """Convenience wrapper for :meth:`Catalog.apply`."""
    return catalog.apply(content, markdown=markdown)
