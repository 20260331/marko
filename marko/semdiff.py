"""
Semantic diff for Markdown documents
====================================

A source-level ``diff`` treats every equivalent Markdown spelling as a
modification (``-`` vs. ``*`` list bullets, ATX vs. setext headings, inline
vs. reference links ...), while diffing the rendered HTML loses the document
structure (links, list items and node boundaries disappear into one text
blob).

This module diffs the documents on the level of their *parsed semantics*:

1. Both sides are parsed into the Marko AST and normalized into
   :class:`SemNode` trees, a canonical representation that ignores purely
   syntactic variations but keeps every semantic boundary (links, images,
   lists, code spans, attributes ...).
2. The normalized trees are aligned with a tree differ (exact LCS plus
   similarity matching for modified nodes and word-level text diffing) to
   produce a stable, JSON serializable :class:`ChangeSet`.
3. The change set is self-contained: both the old and the new semantic
   documents can be reconstructed from it alone
   (:meth:`ChangeSet.old_document` / :meth:`ChangeSet.new_document`), and a
   human-readable review document can be rendered
   (:meth:`ChangeSet.review`).

Example::

    >>> from marko.semdiff import semantic_diff
    >>> changes = semantic_diff("- buy milk\\n", "* buy milk\\n")
    >>> changes.is_empty()
    True
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence, Union, cast

from . import block, inline

__all__ = (
    "SemNode",
    "ChangeSet",
    "semanticize",
    "semantic_diff",
    "render_semantic",
)

#: Version of the serialized change set format.
CHANGE_SET_VERSION = 1

#: A child of a semantic node is either another node or a plain text leaf.
SemChild = Union["SemNode", str]

# ---------------------------------------------------------------------------
# Canonical semantic model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemNode:
    """A canonical, hashable semantic node.

    Two Markdown documents are semantically equivalent if and only if their
    normalized ``SemNode`` trees are equal.

    :param type: the canonical element type name (snake case), e.g.
        ``"heading"``, ``"link"`` or ``"list"``.
    :param attrs: ordered ``(name, value)`` pairs of scalar attributes that
        carry semantics, e.g. a heading's ``level`` or a link's ``dest``.
    :param children: child nodes and plain text leaves.
    """

    type: str
    attrs: tuple[tuple[str, Any], ...] = ()
    children: tuple[SemChild, ...] = ()

    def attr(self, name: str, default: Any = None) -> Any:
        """Return the attribute value or *default*."""
        for key, value in self.attrs:
            if key == name:
                return value
        return default

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dictionary."""
        return {
            "type": self.type,
            "attrs": {key: value for key, value in self.attrs},
            "children": [
                child.to_dict() if isinstance(child, SemNode) else child
                for child in self.children
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemNode:
        """Build a node from the :meth:`to_dict` representation."""
        children = tuple(
            cls.from_dict(child) if isinstance(child, dict) else child
            for child in data.get("children", ())
        )
        attrs = tuple(sorted(data.get("attrs", {}).items()))
        return cls(data["type"], attrs, children)


# Element types that carry no semantics of their own and are dropped during
# normalization. Blank lines only separate blocks (list tightness is already
# computed on the List node), link reference definitions are resolved into
# the corresponding Link nodes while parsing.
_DROP_TYPES = frozenset({"blank_line", "link_ref_def"})

# Public attribute names that never carry semantics and must not survive the
# generic attribute extraction.
_SKIP_ATTRS = frozenset(
    {
        "children",
        "body",
        "inline_body",
        "escape",
        "bullet",
        "extra",
        "syntax_spans",
        "source_span",
        "dest_span",
        "title_span",
        "link_ref_defs",
        "footnotes",
        "delimiters",
    }
)

_SCALAR_ATTR_TYPES = (str, int, bool, float)

_WHITESPACE_RE = re.compile(r"\s+")


def semanticize(text: str, *, markdown: Any | None = None) -> SemNode:
    """Parse *text* into a canonical semantic tree.

    :param text: the Markdown source.
    :param markdown: an optional configured :class:`marko.Markdown` instance,
        e.g. one with the GFM extension enabled. A bare CommonMark parser is
        used when omitted.
    """
    if markdown is None:
        from .parser import Parser

        markdown = _default_parser()
    node = _convert_element(markdown.parse(text))
    assert node is not None  # the document root is never dropped
    return node


def _default_parser():
    parser = getattr(_default_parser, "_instance", None)
    if parser is None:
        from .parser import Parser

        parser = Parser()
        _default_parser._instance = parser  # type: ignore[attr-defined]
    return parser


def _convert_element(element: Any) -> SemNode | None:
    """Convert a Marko element (block or inline) into a SemNode."""
    node_type = element.get_type(snake_case=True)
    if node_type in _DROP_TYPES:
        return None

    # ATX and setext headings are the same node semantically.
    if isinstance(element, block.SetextHeading):
        node_type = "heading"
    # Inline links, autolinks and GFM bare URLs render identically.
    if isinstance(element, (inline.AutoLink,)):
        node_type = "link"
    if isinstance(element, inline.StrongEmphasis):
        node_type = "strong"

    if isinstance(element, block.HTMLBlock):
        return SemNode("html_block", (), (element.body,))

    # Fenced and indented code blocks collapse onto one canonical node.
    # Code content is literal: only the trailing newline convention is
    # unified, every other byte (including leading blank lines) is kept.
    if isinstance(element, (block.FencedCode, block.CodeBlock)):
        content = cast(str, cast(Any, element.children[0]).children)
        if content:
            content = re.sub(r"\n+$", "\n", content)
        return SemNode("code_block", (("lang", element.lang or ""),), (content,))

    # Inline elements whose only child is a raw string are leaf nodes.
    raw_children = getattr(element, "children", None)
    if isinstance(raw_children, str):
        if isinstance(element, inline.CodeSpan):
            return SemNode("code_span", (), (raw_children,))
        if isinstance(element, inline.InlineHTML):
            return SemNode("inline_html", (), (raw_children,))
        return SemNode(
            node_type,
            _extract_attrs(element, node_type),
            (raw_children,),
        )

    children = _convert_children(element)
    attrs = _extract_attrs(element, node_type)
    return SemNode(node_type, attrs, tuple(children))


def _convert_children(element: Any) -> list[SemChild]:
    raw_children = getattr(element, "children", None)
    if not raw_children:
        return []

    items: list[SemChild | None] = []
    for child in raw_children:
        if isinstance(child, str):
            items.append(child)
            continue
        child_type = child.get_type(snake_case=True)
        if child_type in _DROP_TYPES:
            items.append(None)
        elif isinstance(child, inline.Literal):
            # Backslash escapes resolve to the literal escaped character.
            items.append(cast(str, child.children))
        elif isinstance(child, inline.RawText):
            text = cast(str, child.children)
            if getattr(child, "escape", False):
                # Character references (e.g. ``&ast;``) resolve as well.
                text = html.unescape(text)
            items.append(text)
        elif isinstance(child, inline.LineBreak):
            # Soft line breaks are mere wrapping; hard breaks are a node.
            if child.soft:
                items.append(" ")
            else:
                items.append(SemNode("line_break"))
        else:
            items.append(_convert_element(child))
    return _merge_text(items)


def _merge_text(items: Sequence[SemChild | None]) -> list[SemChild]:
    """Join adjacent text leaves and collapse insignificant whitespace."""
    result: list[SemChild] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            item = _WHITESPACE_RE.sub(" ", item)
            if not item:
                continue
        if isinstance(item, str) and result and isinstance(result[-1], str):
            result[-1] = _WHITESPACE_RE.sub(" ", result[-1] + item)
        else:
            result.append(item)
    return result


def _extract_attrs(element: Any, node_type: str) -> tuple[tuple[str, Any], ...]:
    if node_type == "heading":
        return (("level", element.level),)
    if node_type == "list":
        # The bullet character (``-``/``*``/``+``/``1.``/``1)``) is dropped,
        # only list semantics survive.
        return (
            ("ordered", bool(element.ordered)),
            ("start", element.start),
            ("tight", bool(element.tight)),
        )
    if node_type in ("link", "image"):
        attrs: tuple[tuple[str, Any], ...] = (("dest", element.dest),)
        if element.title:
            attrs += (("title", element.title),)
        return attrs
    if node_type == "paragraph" and hasattr(element, "checked"):
        return (("checked", bool(element.checked)),)
    if node_type == "table_cell":
        cell_attrs: tuple[tuple[str, Any], ...] = (("align", element.align),)
        if element.header:
            cell_attrs += (("header", True),)
        return cell_attrs
    if node_type == "alert":
        return (("alert_type", element.alert_type),)

    # Generic fallback for extension elements (footnotes, ...): keep every
    # public scalar attribute in a deterministic order.
    generic_attrs: list[tuple[str, Any]] = []
    for name, value in vars(element).items():
        if name.startswith("_") or name in _SKIP_ATTRS:
            continue
        if value is None or isinstance(value, _SCALAR_ATTR_TYPES):
            generic_attrs.append((name, value))
    return tuple(sorted(generic_attrs))


# ---------------------------------------------------------------------------
# Tree diffing
# ---------------------------------------------------------------------------

#: Minimum similarity score (0..1) for two nodes to be paired as modified
#: instead of being reported as delete plus insert.
_PAIR_THRESHOLD = 0.3

#: Gaps up to this many candidate pairs are scored exhaustively; larger gaps
#: use the content-word inverted index.
_EXHAUSTIVE_PAIR_LIMIT = 2500

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def semantic_diff(
    old: str | SemNode,
    new: str | SemNode,
    *,
    markdown: Any | None = None,
) -> ChangeSet:
    """Compute the semantic change set between two documents.

    :param old: the old Markdown text or its normalized :class:`SemNode`.
    :param new: the new Markdown text or its normalized :class:`SemNode`.
    :param markdown: optional :class:`marko.Markdown` instance used to parse
        raw text inputs (ignored for pre-parsed :class:`SemNode` inputs).
    """
    old_node = old if isinstance(old, SemNode) else semanticize(old, markdown=markdown)
    new_node = new if isinstance(new, SemNode) else semanticize(new, markdown=markdown)
    changes = _diff_children(old_node.children, new_node.children)
    return ChangeSet(changes)


def _diff_children(
    old_children: Sequence[SemChild], new_children: Sequence[SemChild]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for kind, data in _lcs_segments(old_children, new_children):
        if kind == "equal":
            old_index, new_index = data
            item = old_children[old_index]
            entries.append({"op": "context", "node": item})
        else:
            old_slice, new_slice = data
            entries.extend(
                _diff_gap(
                    [old_children[i] for i in old_slice],
                    [new_children[j] for j in new_slice],
                )
            )
    return entries


def _lcs_segments(
    old: Sequence[SemChild], new: Sequence[SemChild]
) -> list[tuple[str, Any]]:
    """Return alternating ``equal``/``gap`` segments from a maximal LCS.

    Exact matches are found in expected linear time via hash anchoring
    (greedy earliest non-crossing index), which is critical for long
    sequences of structurally heavy nodes such as list items. Consecutive
    non-matched items are grouped into one gap so old-only and new-only
    items in the same region stay together for similarity pairing and
    word-level text diffing.
    """
    positions: dict[Any, list[int]] = {}
    for j, item in enumerate(new):
        positions.setdefault(item, []).append(j)

    matches: list[tuple[int, int]] = []
    last_new = -1
    for i, item in enumerate(old):
        candidates = positions.get(item)
        if not candidates:
            continue
        k = bisect_right(candidates, last_new)
        if k < len(candidates):
            last_new = candidates[k]
            matches.append((i, last_new))

    segments: list[tuple[str, Any]] = []
    cursor_old = cursor_new = 0
    for old_index, new_index in matches:
        if old_index > cursor_old or new_index > cursor_new:
            segments.append(
                (
                    "gap",
                    (range(cursor_old, old_index), range(cursor_new, new_index)),
                )
            )
        segments.append(("equal", (old_index, new_index)))
        cursor_old = old_index + 1
        cursor_new = new_index + 1
    if cursor_old < len(old) or cursor_new < len(new):
        segments.append(
            (
                "gap",
                (range(cursor_old, len(old)), range(cursor_new, len(new))),
            )
        )
    return segments


def _diff_gap(old: list[SemChild], new: list[SemChild]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cursor_old = cursor_new = 0
    for old_index, new_index in _pair_nodes(old, new):
        entries.extend(
            _diff_segment(old[cursor_old:old_index], new[cursor_new:new_index])
        )
        entries.append(_diff_pair(old[old_index], new[new_index]))
        cursor_old, cursor_new = old_index + 1, new_index + 1
    entries.extend(_diff_segment(old[cursor_old:], new[cursor_new:]))
    return entries


def _pair_nodes(
    old: Sequence[SemChild], new: Sequence[SemChild]
) -> list[tuple[int, int]]:
    """Monotone, score-maximizing pairing of same-type structural nodes.

    Candidate pairs are obtained from an inverted index on content words
    (words occurring in more than a fifth of the nodes are treated as
    stopwords), which keeps the work linear-ish for large, similarly shaped
    sequences instead of scoring every old/new combination.
    """
    n, m = len(old), len(new)
    scores: dict[tuple[int, int], float] = {}

    def consider(i: int, j: int) -> None:
        old_item, new_item = old[i], new[j]
        if (
            isinstance(old_item, SemNode)
            and isinstance(new_item, SemNode)
            and old_item.type == new_item.type
        ):
            score = _similarity(old_item, new_item)
            if score >= _PAIR_THRESHOLD:
                scores[i, j] = score

    # Small gaps are scored exhaustively, which also catches short same-type
    # nodes that share no content word ("30" vs "31").
    if n * m <= _EXHAUSTIVE_PAIR_LIMIT:
        for i in range(n):
            for j in range(m):
                consider(i, j)
        return _select_pairs(n, m, scores)

    old_sets = [
        frozenset(_WORD_RE.findall(_plain_text(item).lower()))
        if isinstance(item, SemNode)
        else frozenset()
        for item in old
    ]
    new_sets = [
        frozenset(_WORD_RE.findall(_plain_text(item).lower()))
        if isinstance(item, SemNode)
        else frozenset()
        for item in new
    ]

    # Build (type, word) postings and document frequencies on the new side.
    postings: dict[tuple[str, str], list[int]] = {}
    frequencies: dict[tuple[str, str], int] = {}
    for j, item in enumerate(new):
        if not isinstance(item, SemNode):
            continue
        for word in new_sets[j]:
            key = (item.type, word)
            postings.setdefault(key, []).append(j)
            frequencies[key] = frequencies.get(key, 0) + 1
    stopword_limit = max(10, m // 5)

    candidate_pairs: set[tuple[int, int]] = set()
    wordless_old: list[int] = []
    for i, item in enumerate(old):
        if not isinstance(item, SemNode):
            continue
        words = old_sets[i]
        if not words:
            wordless_old.append(i)
            continue
        for word in words:
            key = (item.type, word)
            if 0 < frequencies.get(key, 0) <= stopword_limit:
                candidate_pairs.update((i, j) for j in postings[key])

    for i, j in candidate_pairs:
        if _jaccard(old_sets[i], new_sets[j]) >= _PAIR_THRESHOLD:
            scores[i, j] = _jaccard(old_sets[i], new_sets[j])

    # Wordless nodes (empty headings, list items with only nested nodes ...)
    # are compared by character ratio, but only when the pool is small:
    # they are almost always anchored exactly or rare in practice.
    if wordless_old and len(wordless_old) <= 50:
        wordless_new = [
            j
            for j, item in enumerate(new)
            if isinstance(item, SemNode) and not new_sets[j]
        ]
        if len(wordless_new) <= 50:
            for i in wordless_old:
                for j in wordless_new:
                    consider(i, j)

    return _select_pairs(n, m, scores)


def _select_pairs(
    n: int, m: int, scores: dict[tuple[int, int], float]
) -> list[tuple[int, int]]:
    """Monotone DP selecting the highest-scoring non-crossing node pairs."""
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            paired_score = scores.get((i - 1, j - 1))
            if paired_score is not None:
                best = max(best, dp[i - 1][j - 1] + paired_score)
            dp[i][j] = best

    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        paired_score = scores.get((i - 1, j - 1))
        if (
            paired_score is not None
            and abs(dp[i][j] - (dp[i - 1][j - 1] + paired_score)) < 1e-12
        ):
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _jaccard(old_words: frozenset[str], new_words: frozenset[str]) -> float:
    if not old_words and not new_words:
        return 0.0
    union = old_words | new_words
    return len(old_words & new_words) / len(union)


def _similarity(old: SemNode, new: SemNode) -> float:
    old_text = _plain_text(old)
    new_text = _plain_text(new)
    old_words = frozenset(_WORD_RE.findall(old_text.lower()))
    new_words = frozenset(_WORD_RE.findall(new_text.lower()))
    jaccard = _jaccard(old_words, new_words)
    if jaccard > 0:
        # Content-word overlap is the strongest, cheapest identity signal.
        return jaccard
    # The character ratio is only consulted for short nodes with no words
    # in common (or attribute-only nodes): SequenceMatcher is quadratic on
    # long, repetitive text, so it must never run over whole subtrees.
    if len(old_text) <= 200 and len(new_text) <= 200:
        return (
            difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).ratio()
            * 0.9
        )
    return 0.0


def _plain_text(node: SemChild) -> str:
    if isinstance(node, str):
        return node
    parts: list[str] = []
    for child in node.children:
        # Separate block siblings (list items, table cells, nested blocks)
        # with a space, otherwise words on both sides merge into one token.
        if parts and isinstance(child, SemNode) and _is_block(child):
            parts.append(" ")
        parts.append(_plain_text(child))
    return "".join(parts)


class _NodeMark:
    """Identity-compared marker for a structural node inside a token stream.

    Node markers never compare equal, so tokens align only with tokens and
    unmatched structural nodes fall out as plain insertions/deletions.
    """

    __slots__ = ("node",)

    def __init__(self, node: SemNode) -> None:
        self.node = node


def _diff_segment(
    old: Sequence[SemChild], new: Sequence[SemChild]
) -> list[dict[str, Any]]:
    """Emit entries for unpaired leftovers between two matched anchors.

    Text leaves are split into word/punctuation tokens and structural nodes
    are wrapped in identity markers. :class:`difflib.SequenceMatcher` then
    aligns tokens *across* intervening nodes (linear on near-equal input),
    so unchanged punctuation on the far side of an inserted link keeps its
    place instead of being reported as rewritten.
    """
    old_flat = _flatten_segment(old)
    new_flat = _flatten_segment(new)

    # Strip the common prefix/suffix in linear time: in the typical case of
    # a small edit inside an otherwise unchanged text, this reduces the
    # matcher input to a handful of tokens and avoids quadratic behavior on
    # long, repetitive streams.
    n, m = len(old_flat), len(new_flat)
    prefix = 0
    limit = min(n, m)
    while prefix < limit and old_flat[prefix] == new_flat[prefix]:
        prefix += 1
    suffix = 0
    limit -= prefix
    while suffix < limit and old_flat[n - 1 - suffix] == new_flat[m - 1 - suffix]:
        suffix += 1

    middle_old = old_flat[prefix : n - suffix]
    middle_new = new_flat[prefix : m - suffix]
    # Pathological, large and dissimilar middle: accept the popular-element
    # heuristic, which keeps the computation bounded.
    autojunk = max(len(middle_old), len(middle_new)) > 2000
    matcher = difflib.SequenceMatcher(None, middle_old, middle_new, autojunk=autojunk)

    entries: list[dict[str, Any]] = []
    if prefix:
        entries.append(
            {
                "op": "context",
                "node": "".join(cast(str, t) for t in old_flat[:prefix]),
            }
        )
    deleted_tokens: list[str] = []
    inserted_tokens: list[str] = []

    def flush_text() -> None:
        if not deleted_tokens and not inserted_tokens:
            return
        if deleted_tokens and inserted_tokens:
            entries.append(
                {
                    "op": "text",
                    "parts": [
                        {"op": "delete", "text": "".join(deleted_tokens)},
                        {"op": "insert", "text": "".join(inserted_tokens)},
                    ],
                }
            )
        elif deleted_tokens:
            entries.append({"op": "delete", "node": "".join(deleted_tokens)})
        else:
            entries.append({"op": "insert", "node": "".join(inserted_tokens)})
        deleted_tokens.clear()
        inserted_tokens.clear()

    def append_context(text: str) -> None:
        if (
            entries
            and entries[-1]["op"] == "context"
            and isinstance(entries[-1]["node"], str)
        ):
            entries[-1]["node"] += text
        else:
            entries.append({"op": "context", "node": text})

    def consume(flat: list[Any], start: int, end: int, deleted: bool) -> None:
        for k in range(start, end):
            item = flat[k]
            if isinstance(item, _NodeMark):
                flush_text()
                entries.append(
                    {
                        "op": "delete" if deleted else "insert",
                        "node": item.node,
                    }
                )
            elif deleted:
                deleted_tokens.append(item)
            else:
                inserted_tokens.append(item)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            flush_text()
            append_context("".join(middle_old[k] for k in range(i1, i2)))
        elif tag == "delete":
            consume(middle_old, i1, i2, True)
        elif tag == "insert":
            consume(middle_new, j1, j2, False)
        else:  # replace
            consume(middle_old, i1, i2, True)
            consume(middle_new, j1, j2, False)
    flush_text()
    if suffix:
        append_context("".join(cast(str, t) for t in old_flat[n - suffix :]))
    return entries


def _flatten_segment(items: Sequence[SemChild]) -> list[Any]:
    flat: list[Any] = []
    for item in items:
        if isinstance(item, str):
            flat.extend(_TOKEN_RE.findall(item))
        else:
            flat.append(_NodeMark(item))
    return flat


def _diff_pair(old: SemChild, new: SemChild) -> dict[str, Any]:
    if not isinstance(old, SemNode) or not isinstance(new, SemNode):
        # Text pairs are already handled by the word-level differ.
        return {"op": "replace", "old": old, "new": new}

    old_attrs = dict(old.attrs)
    new_attrs = dict(new.attrs)
    # Carry the *complete* attribute set (equal values included) so both
    # sides can be reconstructed from the change set alone.
    attr_values = [
        {"name": name, "old": old_attrs.get(name), "new": new_attrs.get(name)}
        for name in sorted(old_attrs.keys() | new_attrs.keys())
    ]
    attr_changes = [attr for attr in attr_values if attr["old"] != attr["new"]]
    child_changes = _diff_children(old.children, new.children)
    if not attr_changes and all(entry["op"] == "context" for entry in child_changes):
        return {"op": "context", "node": new}
    return {
        "op": "update",
        "type": new.type,
        "attrs": attr_values,
        "changes": child_changes,
    }


# ---------------------------------------------------------------------------
# Change set: serialization, reconstruction and review rendering
# ---------------------------------------------------------------------------


class ChangeSet:
    """The structured result of a semantic diff.

    A change set is a tree of entries describing the new document against
    the old one:

    * ``{"op": "context", "node": ...}`` -- unchanged node or text;
    * ``{"op": "insert", "node": ...}`` / ``{"op": "delete", "node": ...}``;
    * ``{"op": "update", "type", "attrs", "changes"}`` -- a matched node
      whose scalar attributes and/or children changed;
    * ``{"op": "text", "parts": [...]}`` -- a word-level inline text change.

    The ``context`` entries make the set self-contained, so both original
    semantic documents can be reconstructed without any extra input.
    """

    def __init__(self, changes: list[dict[str, Any]]) -> None:
        self.changes = changes

    def is_empty(self) -> bool:
        """True if no semantic change was found (formatting-only edits)."""
        return not _has_change(self.changes)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dictionary."""
        return {
            "version": CHANGE_SET_VERSION,
            "changes": [_entry_to_dict(entry) for entry in self.changes],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize to a stable JSON string."""
        return json.dumps(
            self.to_dict(), indent=indent, ensure_ascii=False, sort_keys=False
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeSet:
        """Load a change set produced by :meth:`to_dict`."""
        return cls([_entry_from_dict(entry) for entry in data["changes"]])

    def old_document(self) -> SemNode:
        """Reconstruct the old side's full semantic document."""
        return SemNode("document", (), tuple(_project(self.changes, "old")))

    def new_document(self) -> SemNode:
        """Reconstruct the new side's full semantic document."""
        return SemNode("document", (), tuple(_project(self.changes, "new")))

    def stats(self) -> dict[str, int]:
        """Return counts of insertions, deletions, attribute/text updates."""
        counts = {"insert": 0, "delete": 0, "update": 0, "text": 0}
        _count_changes(self.changes, counts)
        return counts

    def review(self, *, show_context: bool = False) -> str:
        """Render a Markdown review document for human inspection."""
        counts = self.stats()
        total = sum(counts.values())
        lines = [
            "# Markdown Semantic Diff Review",
            "",
            f"- format version: {CHANGE_SET_VERSION}",
            "- summary: "
            f"{total} change(s) "
            f"({counts['insert']} inserted, {counts['delete']} deleted, "
            f"{counts['update']} updated, {counts['text']} text edit(s))",
            "",
            "## Changes",
            "",
        ]
        if self.is_empty():
            lines.append(
                "_No semantic changes. The documents only differ in " "formatting._"
            )
            lines.append("")
        else:
            lines.extend(_render_entries(self.changes, 0, show_context))
        return "\n".join(lines)

    def __bool__(self) -> bool:
        return not self.is_empty()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ChangeSet) and other.changes == self.changes

    def __repr__(self) -> str:
        return f"<ChangeSet {self.stats()}>"


def _has_change(entries: Sequence[dict[str, Any]]) -> bool:
    for entry in entries:
        op = entry["op"]
        if op in ("insert", "delete", "replace", "update", "text"):
            return True
    return False


def _count_changes(entries: Sequence[dict[str, Any]], counts: dict[str, int]) -> None:
    for entry in entries:
        op = entry["op"]
        if op in ("insert", "delete", "update", "text"):
            if op == "text":
                if any(part["op"] != "equal" for part in entry["parts"]):
                    counts["text"] += 1
            else:
                counts[op] += 1
        if op == "update":
            _count_changes(entry["changes"], counts)


def _project(entries: Sequence[dict[str, Any]], side: str) -> list[SemChild]:
    result: list[SemChild] = []
    for entry in entries:
        op = entry["op"]
        if op == "context":
            result.append(entry["node"])
        elif op == "insert" and side == "new":
            result.append(entry["node"])
        elif op == "delete" and side == "old":
            result.append(entry["node"])
        elif op == "replace":
            result.append(entry["old" if side == "old" else "new"])
        elif op == "update":
            attrs = tuple(
                (
                    change["name"],
                    change["old"] if side == "old" else change["new"],
                )
                for change in entry["attrs"]
            )
            result.append(
                SemNode(entry["type"], attrs, tuple(_project(entry["changes"], side)))
            )
        elif op == "text":
            text = "".join(
                part["text"]
                for part in entry["parts"]
                if part["op"] == "equal"
                or (side == "old" and part["op"] == "delete")
                or (side == "new" and part["op"] == "insert")
            )
            if text:
                result.append(text)
    # Adjacent text leaves can be produced at text/context boundaries; the
    # normalized tree merges them, so concatenate to reproduce it exactly.
    # Whitespace must not be re-collapsed here: code block content is literal
    # and every normalized text leaf is already collapsed on its own.
    merged: list[SemChild] = []
    for item in result:
        if isinstance(item, str) and merged and isinstance(merged[-1], str):
            merged[-1] += item
        else:
            merged.append(item)
    return merged


def _entry_to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    op = entry["op"]
    if op in ("context", "insert", "delete"):
        node = entry["node"]
        return {"op": op, "node": _item_to_dict(node)}
    if op == "replace":
        return {
            "op": op,
            "old": _item_to_dict(entry["old"]),
            "new": _item_to_dict(entry["new"]),
        }
    if op == "update":
        return {
            "op": op,
            "type": entry["type"],
            "attrs": entry["attrs"],
            "changes": [_entry_to_dict(child) for child in entry["changes"]],
        }
    if op == "text":
        return {"op": op, "parts": entry["parts"]}
    raise ValueError(f"Unknown change entry op: {op!r}")


def _entry_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    op = data["op"]
    if op in ("context", "insert", "delete"):
        return {"op": op, "node": _item_from_dict(data["node"])}
    if op == "replace":
        return {
            "op": op,
            "old": _item_from_dict(data["old"]),
            "new": _item_from_dict(data["new"]),
        }
    if op == "update":
        return {
            "op": op,
            "type": data["type"],
            "attrs": data["attrs"],
            "changes": [_entry_from_dict(child) for child in data["changes"]],
        }
    return {"op": op, "parts": data["parts"]}


def _item_to_dict(item: SemChild) -> Any:
    return item.to_dict() if isinstance(item, SemNode) else item


def _item_from_dict(data: Any) -> SemChild:
    return SemNode.from_dict(data) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Review document rendering
# ---------------------------------------------------------------------------

#: Types that occupy block boundaries (extension nodes fall back to
#: structural inference).
_BLOCK_TYPES = frozenset(
    {
        "document",
        "paragraph",
        "heading",
        "list",
        "list_item",
        "quote",
        "code_block",
        "html_block",
        "thematic_break",
        "table",
        "table_row",
        "alert",
        "footnote_def",
    }
)

_INLINE_CONTAINERS = frozenset({"paragraph", "heading", "table_cell"})


def _is_block(item: SemChild) -> bool:
    if isinstance(item, str):
        return False
    if item.type in _BLOCK_TYPES:
        return True
    return any(
        isinstance(child, SemNode) and _is_block(child) for child in item.children
    )


def _render_entries(
    entries: Sequence[dict[str, Any]], depth: int, show_context: bool
) -> list[str]:
    indent = "  " * depth
    lines: list[str] = []
    inline_parent = _entries_are_inline(entries)
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer:
            lines.append(indent + "  " + buffer)
            buffer = ""

    def mark(item: SemChild, inserted: bool) -> str:
        preview = _preview_inline(item)
        return f"{{+{preview}+}}" if inserted else f"[-{preview}-]"

    for entry in entries:
        op = entry["op"]
        if op == "context":
            node = entry["node"]
            is_inline = not _is_block(node)
            if inline_parent and is_inline:
                buffer += _preview_inline(node)
            elif show_context:
                flush()
                if is_inline:
                    lines.append(indent + "  " + _preview_inline(node))
                else:
                    lines.extend(_render_removed_or_added(indent, " ", node))
        elif op in ("insert", "delete"):
            node = entry["node"]
            if inline_parent and not _is_block(node):
                buffer += mark(node, op == "insert")
            else:
                flush()
                lines.extend(
                    _render_removed_or_added(
                        indent, "+" if op == "insert" else "-", node
                    )
                )
        elif op == "replace":
            if (
                inline_parent
                and not _is_block(entry["old"])
                and not _is_block(entry["new"])
            ):
                buffer += mark(entry["old"], False) + mark(entry["new"], True)
            else:
                flush()
                lines.extend(_render_removed_or_added(indent, "-", entry["old"]))
                lines.extend(_render_removed_or_added(indent, "+", entry["new"]))
        elif op == "update":
            flush()
            lines.append(f"{indent}@{entry['type']}")
            for change in entry["attrs"]:
                if change["old"] == change["new"]:
                    continue
                lines.append(
                    f"{indent}  ~ {change['name']}: "
                    f"{json.dumps(change['old'], ensure_ascii=False)} -> "
                    f"{json.dumps(change['new'], ensure_ascii=False)}"
                )
            lines.extend(_render_entries(entry["changes"], depth + 1, show_context))
        elif op == "text":
            buffer += "".join(
                part["text"]
                if part["op"] == "equal"
                else f"[-{part['text']}-]"
                if part["op"] == "delete"
                else f"{{+{part['text']}+}}"
                for part in entry["parts"]
            )
    flush()
    return lines


def _entries_are_inline(entries: Sequence[dict[str, Any]]) -> bool:
    for entry in entries:
        op = entry["op"]
        if op == "text":
            return True
        if op in ("context", "insert", "delete", "replace"):
            nodes = [
                entry[key]
                for key in ("node", "old", "new")
                if entry.get(key) is not None
            ]
            if any(not _is_block(node) for node in nodes):
                return True
        if op == "update" and entry["type"] in _INLINE_CONTAINERS:
            return True
    return False


def _render_removed_or_added(indent: str, marker: str, item: SemChild) -> list[str]:
    if not _is_block(item):
        return [f"{indent}{marker} {_preview_inline(item)}"]
    assert isinstance(item, SemNode)
    rendered = _CanonicalMarkdown.render_node(item).rstrip("\n")
    return [
        f"{indent}{marker} {line}" if line else f"{indent}{marker}"
        for line in rendered.splitlines()
    ]


def _preview_inline(item: SemChild) -> str:
    if isinstance(item, str):
        return item
    return _INLINE_WRITER.render_inline(item)


# ---------------------------------------------------------------------------
# Canonical Markdown writer (re-serialize a semantic tree)
# ---------------------------------------------------------------------------


def render_semantic(node: SemNode) -> str:
    """Render a semantic document (or subtree) back to canonical Markdown."""
    return _CanonicalMarkdown.render_node(node)


class _CanonicalMarkdown:
    """Deterministic Markdown serializer for semantic nodes."""

    def __init__(self) -> None:
        self._prefix = ""
        self._second_prefix = ""

    @classmethod
    def render_node(cls, node: SemNode) -> str:
        writer = cls()
        with writer.container("", ""):
            if node.type == "document":
                return writer.render_document(node)
            if _is_block(node):
                return writer.render_block(node)
            return writer.render_inline(node)

    @contextmanager
    def container(self, prefix: str, second_prefix: str) -> Iterator[None]:
        old_prefix, old_second = self._prefix, self._second_prefix
        self._prefix += prefix
        self._second_prefix += second_prefix
        yield
        self._prefix, self._second_prefix = old_prefix, old_second

    def render_document(self, node: SemNode) -> str:
        blocks = [
            self.render_block(child)
            for child in node.children
            if isinstance(child, SemNode)
        ]
        blocks = [block.rstrip("\n") for block in blocks if block.strip()]
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def _apply_prefix(self, text: str) -> str:
        # Hard line breaks may split an inline serialization into several
        # physical lines.
        lines = text.rstrip("\n").split("\n")
        result = self._prefix + lines[0] + "\n"
        for line in lines[1:]:
            result += self._second_prefix + line + "\n"
        self._prefix = self._second_prefix
        return result

    def render_block(self, node: SemNode) -> str:
        handler = getattr(self, f"block_{node.type}", None)
        if handler is not None:
            return handler(node)
        # Generic fallback: block-like unknown node with block children, or
        # an inline-only unknown node treated as a paragraph.
        block_children = [
            child
            for child in node.children
            if isinstance(child, SemNode) and _is_block(child)
        ]
        if block_children:
            return "".join(self.render_block(child) for child in block_children)
        return self._apply_prefix(self.render_children(node) + "\n")

    def render_children(self, node: SemNode) -> str:
        return "".join(self.render_inline(child) for child in node.children)

    # -- Block handlers ----------------------------------------------------

    def block_paragraph(self, node: SemNode) -> str:
        text = self.render_children(node)
        if node.attr("checked") is not None:
            box = "[x] " if node.attr("checked") else "[ ] "
            text = box + text
        return self._apply_prefix(text + "\n")

    def block_heading(self, node: SemNode) -> str:
        result = (
            self._prefix
            + "#" * int(node.attr("level", 1))
            + " "
            + self.render_children(node)
            + "\n"
        )
        self._prefix = self._second_prefix
        return result

    def block_thematic_break(self, node: SemNode) -> str:
        result = self._prefix + "* * *\n"
        self._prefix = self._second_prefix
        return result

    def block_code_block(self, node: SemNode) -> str:
        lang = node.attr("lang", "") or ""
        content = "".join(
            child for child in node.children if isinstance(child, str)
        ).rstrip("\n")
        lines = [self._prefix + f"```{lang}"]
        lines.extend(self._second_prefix + line for line in content.split("\n"))
        lines.append(self._second_prefix + "```")
        self._prefix = self._second_prefix
        return "\n".join(lines) + "\n"

    def block_html_block(self, node: SemNode) -> str:
        body = "".join(child for child in node.children if isinstance(child, str))
        result = self._prefix + body
        if not result.endswith("\n"):
            result += "\n"
        self._prefix = self._second_prefix
        return result

    def block_quote(self, node: SemNode) -> str:
        with self.container("> ", "> "):
            blocks = [
                self.render_block(child)
                for child in node.children
                if isinstance(child, SemNode)
            ]
            result = self._join_blocks(blocks)
        self._prefix = self._second_prefix
        return result

    def block_alert(self, node: SemNode) -> str:
        lines = [self._prefix + f"> [!{node.attr('alert_type')}]"]
        with self.container("> ", "> "):
            blocks = [
                self.render_block(child)
                for child in node.children
                if isinstance(child, SemNode)
            ]
            body = self._join_blocks(blocks)
        if body:
            lines.append(body.rstrip("\n"))
        self._prefix = self._second_prefix
        return "\n".join(lines) + "\n"

    def _join_blocks(self, blocks: Sequence[str]) -> str:
        """Join rendered blocks with a properly prefixed blank separator."""
        kept = [block.rstrip("\n") for block in blocks if block.strip()]
        if not kept:
            return ""
        separator = "\n" + self._second_prefix.rstrip() + "\n"
        return separator.join(kept) + "\n"

    def block_list(self, node: SemNode) -> str:
        result: list[str] = []
        ordered = node.attr("ordered", False)
        number = int(node.attr("start", 1) or 1)
        for child in node.children:
            if not isinstance(child, SemNode):
                continue
            if ordered:
                marker = f"{number}. "
                number += 1
            else:
                marker = "- "
            with self.container(marker, " " * len(marker)):
                result.append(self.render_block(child))
        self._prefix = self._second_prefix
        separator = (
            self._second_prefix.rstrip() + "\n" if not node.attr("tight", True) else ""
        )
        return separator.join(result)

    def block_list_item(self, node: SemNode) -> str:
        blocks = [
            self.render_block(child)
            for child in node.children
            if isinstance(child, SemNode)
        ]
        return "".join(blocks)

    def block_table(self, node: SemNode) -> str:
        rows = [child for child in node.children if isinstance(child, SemNode)]
        if not rows:
            return ""
        lines = [self._prefix + self._render_table_row(rows[0])]
        delimiters = []
        for cell in rows[0].children:
            if not isinstance(cell, SemNode):
                continue
            align = cell.attr("align")
            if align == "center":
                delimiters.append(":---:")
            elif align == "left":
                delimiters.append(":---")
            elif align == "right":
                delimiters.append("---:")
            else:
                delimiters.append("---")
        lines.append(self._prefix + "| " + " | ".join(delimiters) + " |")
        for row in rows[1:]:
            lines.append(self._prefix + self._render_table_row(row))
        self._prefix = self._second_prefix
        return "\n".join(lines) + "\n"

    def _render_table_row(self, row: SemNode) -> str:
        cells = [
            self.render_children(cell).replace("|", "\\|")
            for cell in row.children
            if isinstance(cell, SemNode)
        ]
        return "| " + " | ".join(cells) + " |"

    def block_footnote_def(self, node: SemNode) -> str:
        label = node.attr("label", "")
        blocks = [
            self.render_block(child)
            for child in node.children
            if isinstance(child, SemNode)
        ]
        inner = self._join_blocks(blocks).rstrip("\n")
        inner_lines = inner.split("\n")
        result = self._prefix + f"[^{label}]: " + inner_lines[0] + "\n"
        for line in inner_lines[1:]:
            result += self._second_prefix + "    " + line + "\n"
        self._prefix = self._second_prefix
        return result

    # -- Inline handlers ---------------------------------------------------

    def render_inline(self, item: SemChild) -> str:
        if isinstance(item, str):
            return _escape_text(item)
        handler = getattr(self, f"inline_{item.type}", None)
        if handler is not None:
            return handler(item)
        return self.render_children(item)

    def inline_line_break(self, node: SemNode) -> str:
        return "\\\n"

    def inline_emphasis(self, node: SemNode) -> str:
        return f"*{self.render_children(node)}*"

    def inline_strong(self, node: SemNode) -> str:
        return f"**{self.render_children(node)}**"

    def inline_strikethrough(self, node: SemNode) -> str:
        return f"~~{self.render_children(node)}~~"

    def inline_code_span(self, node: SemNode) -> str:
        content = "".join(child for child in node.children if isinstance(child, str))
        longest = max(
            (len(match.group()) for match in re.finditer(r"`+", content)),
            default=0,
        )
        fence = "`" * (longest + 1 if longest else 1)
        pad = (
            " "
            if content.startswith("`") or content.endswith("`") or not content
            else ""
        )
        return f"{fence}{pad}{content}{pad}{fence}"

    def inline_inline_html(self, node: SemNode) -> str:
        return "".join(child for child in node.children if isinstance(child, str))

    def inline_link(self, node: SemNode) -> str:
        return self._render_linkish(node, "link")

    def inline_image(self, node: SemNode) -> str:
        return self._render_linkish(node, "image")

    def _render_linkish(self, node: SemNode, kind: str) -> str:
        text = self.render_children(node)
        dest = node.attr("dest", "") or ""
        if re.search(r"\s", dest):
            dest = f"<{dest}>"
        title = node.attr("title")
        title_part = f" {json.dumps(title, ensure_ascii=False)}" if title else ""
        prefix = "!" if kind == "image" else ""
        return f"{prefix}[{text}]({dest}{title_part})"

    def inline_footnote_ref(self, node: SemNode) -> str:
        return f"[^{node.attr('label', '')}]"


# Characters that can start inline Markdown constructs and therefore need to
# be escaped when serializing plain text leaves.
_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>~])")


def _escape_text(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


# Shared by review rendering; inline handlers never mutate writer state.
_INLINE_WRITER = _CanonicalMarkdown()


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------


def _build_markdown(extensions: Sequence[str]) -> Any:
    import marko

    return marko.Markdown(extensions=list(extensions) or None)


def main(argv: Sequence[str] | None = None) -> int:
    """Render a semantic review document (``--json`` for the change set).

    Invoke with ``python -m marko.semdiff_cli old.md new.md``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m marko.semdiff_cli",
        description="Semantically diff two Markdown documents.",
    )
    parser.add_argument("old", help="path to the old Markdown file")
    parser.add_argument("new", help="path to the new Markdown file")
    parser.add_argument(
        "-e",
        "--extension",
        action="append",
        default=[],
        metavar="EXTENSION",
        help="Marko extension to enable, may be given multiple times",
    )
    parser.add_argument(
        "--json", action="store_true", help="output the JSON change set"
    )
    parser.add_argument("-o", "--output", help="write output to this file")
    args = parser.parse_args(argv)

    with open(args.old, encoding="utf-8") as f:
        old_text = f.read()
    with open(args.new, encoding="utf-8") as f:
        new_text = f.read()

    markdown = _build_markdown(args.extension)
    change_set = semantic_diff(old_text, new_text, markdown=markdown)
    output = change_set.to_json() if args.json else change_set.review()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
