"""Reproduce the q3 localization-catalog behavior with real library calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import marko
from marko.l10n import Catalog, CatalogError


def find_entry(catalog: Catalog, text: str):
    return next(entry for entry in catalog if text in entry.source)


def main() -> None:
    source = (
        "# Release notes\n\n"
        "Use `git status` to see **changes** and read "
        '[the docs](https://example.com/docs "Guide").\n\n'
        "- [ ] review *draft*\n"
    )
    catalog = Catalog.export(source)
    print("== 1. Export: sentence-level text plus protected placeholders ==")
    for entry in catalog:
        print(f"{entry.id:8} {entry.block:12} source={entry.source!r}")
        for placeholder in entry.placeholders:
            print(
                "         placeholder "
                f"{{{placeholder.name}}} type={placeholder.type} attrs={placeholder.attrs}"
            )

    paragraph = find_entry(catalog, "Use")
    paragraph.translation = "阅读{2}，用{0}查看{1}。"
    paragraph.placeholders[1].segment.translation = "更改"
    paragraph.placeholders[2].segment.translation = "文档"
    result = catalog.apply(source)
    print("\n== 2. Apply a reordered Chinese translation ==")
    print(result)
    print("link targets in re-parsed HTML:", marko.convert(result).count('href="https://example.com/docs"'))

    print("\n== 3. Reject structure injected by a translator ==")
    injected = Catalog.export("Read `run` now.\n")
    injected["0"].translation = "请阅读 **hacked**{0}。"
    try:
        injected.apply("Read `run` now.\n")
    except CatalogError as exc:
        print("CatalogError:", exc)

    print("\n== 4. Defect: nested-only translation is ignored ==")
    nested_only = Catalog.export("**important**\n")
    nested_only["0"].placeholders[0].segment.translation = "重要"
    print("input:         **important**")
    print("nested only:  ", repr(nested_only.apply("**important**\n")))
    nested_only["0"].translation = "{0}"
    print("with outer {0}:", repr(nested_only.apply("**important**\n")))

    print("\n== 5. Defect: delimiter characters escape nested structure ==")
    emphasis = Catalog.export("*important*\n")
    emphasis["0"].translation = "{0}"
    emphasis["0"].placeholders[0].segment.translation = "2*3"
    emphasis_out = emphasis.apply("*important*\n")
    print("emphasis input:  *important*")
    print("emphasis output:", emphasis_out.rstrip())
    print("re-parsed HTML: ", marko.convert(emphasis_out).strip())

    link = Catalog.export("[docs](https://example.com/docs)\n")
    link["0"].translation = "{0}"
    link["0"].placeholders[0].segment.translation = "] hacked ["
    link_out = link.apply("[docs](https://example.com/docs)\n")
    print("\nlink input:       [docs](https://example.com/docs)")
    print("link output:     ", link_out.rstrip())
    print("re-parsed HTML:  ", marko.convert(link_out).strip())

    serialized = json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2)
    print("\n== 6. JSON catalog is serializable ==")
    print(serialized.splitlines()[0], "... entries:", len(catalog))


if __name__ == "__main__":
    main()
