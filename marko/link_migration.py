"""
Link destination migration on parsed documents.

When a publishing site reorganizes its directory layout, the destinations of
document links and image assets must be rewritten. Doing the replacement on
rendered output (e.g. HTML) is incomplete: link reference definitions render
as nothing in HTML, and every other renderer (Markdown, LaTeX, AST, ...) would
keep the stale destinations.

This module performs the migration on the parsed :class:`~marko.block.Document`
instead. Relative destinations are resolved against the document's *logical
publish path*, looked up in the appropriate mapping and rewritten relative to
the document's new publish path. Every link-bearing node produces a
:class:`LinkDecision` describing what was done, and the input document is
never mutated -- a migrated copy is returned.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import posixpath
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, List, Optional, Union
from urllib.parse import urlsplit, urlunsplit

from . import block, inline

if TYPE_CHECKING:
    from .element import Element

_LINK_NODE_TYPES = (
    inline.Link,
    inline.Image,
    inline.AutoLink,
    block.LinkRefDef,
)
LinkNode = Union[inline.Link, inline.Image, inline.AutoLink, block.LinkRefDef]

#: A mapping from an old logical publish path to the new one, or a callable
#: taking the old path and returning the new path (or ``None`` if the path is
#: not covered by the mapping).
PathMapping = Optional[Union[Mapping[str, str], Callable[[str], Optional[str]]]]


class LinkKind(str, enum.Enum):
    """The kind of a link-bearing node."""

    LINK = "link"
    IMAGE = "image"
    AUTO_LINK = "auto_link"
    LINK_REF_DEF = "link_ref_def"


class LinkAction(str, enum.Enum):
    """The decision made for a single link-bearing node."""

    #: The destination was rewritten by the mapping.
    REWRITTEN = "rewritten"
    #: The destination is local and covered by the mapping, but the rewritten
    #: value is identical to the original.
    UNCHANGED = "unchanged"
    #: An absolute URL (scheme or network location), e.g. ``https://`` or
    #: ``mailto:`` -- nothing to migrate.
    EXTERNAL = "external"
    #: A same-document reference without a path (e.g. ``#section``).
    ANCHOR = "anchor"
    #: A local destination that no mapping entry covers.
    UNMAPPED = "unmapped"
    #: One reference definition is consumed by links and images whose mapped
    #: destinations disagree, so the shared definition is left untouched.
    CONFLICT = "conflict"


@dataclasses.dataclass(frozen=True)
class LinkDecision:
    """The migration decision for one link-bearing node."""

    #: What kind of node the decision applies to.
    kind: LinkKind
    #: The action that was taken.
    action: LinkAction
    #: The original destination.
    original: str
    #: The destination after migration (equal to ``original`` unless the
    #: action is :attr:`LinkAction.REWRITTEN`).
    updated: str
    #: The migrated node, or ``None`` for ``document.link_ref_defs`` entries
    #: that have no corresponding node in the tree.
    node: Element | None
    #: The reference label for link reference definition decisions.
    label: str | None = None
    #: The logical publish path the original relative destination resolves to,
    #: or ``None`` for external/anchor destinations.
    source_path: str | None = None
    #: The logical publish path the destination was mapped to.
    target_path: str | None = None
    #: The source span of the node, when available.
    span: tuple[int, int] | None = None
    #: Additional human-readable information, e.g. a conflict explanation.
    note: str = ""


def _lookup(path_map: PathMapping, source: str) -> str | None:
    if path_map is None:
        return None
    if isinstance(path_map, Mapping):
        return path_map.get(source)
    return path_map(source)


def _resolve_path(document_path: str, target_path: str) -> str:
    """Resolve a possibly relative destination against a document path."""
    if target_path.startswith("/"):
        return posixpath.normpath(target_path)
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(document_path), target_path)
    )


def _retarget(
    target: str,
    new_document_path: str,
    was_absolute: bool,
    query: str,
    fragment: str,
) -> str:
    """Build the rewritten destination relative to the new document path."""
    if was_absolute:
        path = target
    else:
        path = posixpath.relpath(target, posixpath.dirname(new_document_path))
    return urlunsplit(("", "", path, query, fragment))


def _migrate_local(
    dest: str,
    document_path: str,
    new_document_path: str,
    path_map: PathMapping,
) -> tuple[str, LinkAction, str | None, str | None]:
    """Migrate one destination. Returns (updated, action, source, target)."""
    parts = urlsplit(dest)
    if parts.scheme or parts.netloc:
        return dest, LinkAction.EXTERNAL, None, None
    if not parts.path:
        return dest, LinkAction.ANCHOR, None, None
    source = _resolve_path(document_path, parts.path)
    target = _lookup(path_map, source)
    if target is None:
        return dest, LinkAction.UNMAPPED, source, None
    updated = _retarget(
        target,
        new_document_path,
        parts.path.startswith("/"),
        parts.query,
        parts.fragment,
    )
    action = LinkAction.REWRITTEN if updated != dest else LinkAction.UNCHANGED
    return updated, action, source, target


def _collect_link_nodes(element: Element) -> List[LinkNode]:
    """Return all link-bearing nodes in document (depth-first) order."""
    nodes: List[LinkNode] = []

    def visit(node: Element) -> None:
        children = getattr(node, "children", None)
        if not isinstance(children, list):
            return
        for child in children:
            if isinstance(child, _LINK_NODE_TYPES):
                nodes.append(child)
            visit(child)

    visit(element)
    return nodes


def migrate_links(
    document: block.Document,
    *,
    document_path: str,
    new_document_path: str | None = None,
    link_map: PathMapping = None,
    image_map: PathMapping = None,
) -> tuple[block.Document, list[LinkDecision]]:
    """Migrate link and image destinations on a parsed document.

    The document is deep-copied first; the returned copy has every local
    :class:`~marko.inline.Link`, :class:`~marko.inline.Image`,
    :class:`~marko.inline.AutoLink` and :class:`~marko.block.LinkRefDef`
    destination (plus the ``document.link_ref_defs`` mapping) rewritten, while
    the input document stays untouched. The migration therefore takes effect
    for every renderer rather than only for HTML output.

    :param document: the parsed document.
    :param document_path: the logical publish path of the document *before*
        the migration. Relative destinations are resolved against it.
    :param new_document_path: the logical publish path of the document
        *after* the migration. If omitted, the destination of ``document_path``
        in ``link_map`` is used, falling back to ``document_path`` when the
        document itself is not moved.
    :param link_map: mapping for document link destinations.
    :param image_map: mapping for image asset destinations.
    :returns: a tuple of the migrated document copy and the per-node
        :class:`LinkDecision` list in document order.
    """
    if not isinstance(document, block.Document):
        raise TypeError("migrate_links expects a parsed block.Document instance.")

    if new_document_path is None:
        new_document_path = _lookup(link_map, document_path) or document_path

    migrated = copy.deepcopy(document)
    decisions: list[LinkDecision] = []

    inline_nodes: List[Union[inline.Link, inline.Image, inline.AutoLink]] = []
    refdef_nodes: List[block.LinkRefDef] = []
    for node in _collect_link_nodes(migrated):
        if isinstance(node, block.LinkRefDef):
            refdef_nodes.append(node)
        else:
            inline_nodes.append(node)

    # Migrate inline links/images first. ``consumer_results`` remembers the
    # result for each original destination so reference definitions (which a
    # link and an image may share through one label) stay consistent with the
    # nodes that consume them.
    consumer_results: dict[str, list[tuple[str, LinkAction]]] = {}
    for node in inline_nodes:
        if isinstance(node, inline.Image):
            kind = LinkKind.IMAGE
            path_map = image_map
        elif isinstance(node, inline.AutoLink):
            kind = LinkKind.AUTO_LINK
            path_map = link_map
        else:
            kind = LinkKind.LINK
            path_map = link_map
        original = node.dest
        updated, action, source, target = _migrate_local(
            original, document_path, new_document_path, path_map
        )
        node.dest = updated
        consumer_results.setdefault(original, []).append((updated, action))
        decisions.append(
            LinkDecision(
                kind=kind,
                action=action,
                original=original,
                updated=updated,
                node=node,
                source_path=source,
                target_path=target,
                span=node.source_span,
            )
        )

    # Migrate reference definitions. A definition follows the decision of the
    # nodes consuming it; when links and images map the shared destination to
    # different values the conflict is reported and the definition is kept.
    migrated_labels: set[str] = set()
    for node in refdef_nodes:
        original = node.dest
        parts = urlsplit(original)
        note = ""
        if parts.scheme or parts.netloc:
            updated, action, source, target = (
                original,
                LinkAction.EXTERNAL,
                None,
                None,
            )
        elif not parts.path:
            updated, action, source, target = (
                original,
                LinkAction.ANCHOR,
                None,
                None,
            )
        else:
            source = _resolve_path(document_path, parts.path)
            results = consumer_results.get(original)
            if results:
                updated_values = {updated for updated, _ in results}
                if len(updated_values) > 1:
                    updated = original
                    action = LinkAction.CONFLICT
                    target = None
                    note = (
                        "Reference definition is shared by links and images "
                        "that map to different destinations: "
                        + ", ".join(sorted(updated_values))
                    )
                else:
                    updated, action = results[0]
                    target = (
                        _resolve_path(new_document_path, urlsplit(updated).path)
                        if action == LinkAction.REWRITTEN
                        else None
                    )
            else:
                # Unreferenced definition: migrate with whichever mapping
                # covers it, preferring the document link mapping.
                updated, action, source, target = _migrate_local(
                    original,
                    document_path,
                    new_document_path,
                    link_map,
                )
                if action == LinkAction.UNMAPPED:
                    updated, action, source, target = _migrate_local(
                        original,
                        document_path,
                        new_document_path,
                        image_map,
                    )
        node.dest = updated
        if node.label not in migrated_labels:
            title = migrated.link_ref_defs[node.label][1]
            migrated.link_ref_defs[node.label] = (updated, title)
            migrated_labels.add(node.label)
        else:
            note = (
                note + " " if note else ""
            ) + "Duplicate label; the first definition is authoritative."
        decisions.append(
            LinkDecision(
                kind=LinkKind.LINK_REF_DEF,
                action=action,
                original=original,
                updated=updated,
                node=node,
                label=node.label,
                source_path=source,
                target_path=target,
                span=node.source_span,
                note=note,
            )
        )

    # Definitions living only in the mapping (e.g. programmatically built
    # documents) still need their dictionary entry rewritten.
    for label, (dest, title) in migrated.link_ref_defs.items():
        if label in migrated_labels:
            continue
        updated, action, source, target = _migrate_local(
            dest, document_path, new_document_path, link_map
        )
        if action == LinkAction.UNMAPPED:
            updated, action, source, target = _migrate_local(
                dest, document_path, new_document_path, image_map
            )
        migrated.link_ref_defs[label] = (updated, title)
        decisions.append(
            LinkDecision(
                kind=LinkKind.LINK_REF_DEF,
                action=action,
                original=dest,
                updated=updated,
                node=None,
                label=label,
                source_path=source,
                target_path=target,
            )
        )

    return migrated, decisions
