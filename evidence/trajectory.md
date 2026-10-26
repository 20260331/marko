# Markdown 结构化本地化接口

> **Workspace:** /home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip

---

**User**

翻译 Markdown 时，直接把文本重新解析会让译者输入改变链接、强调和代码结构，逐个 RawText 替换又没有句子上下文。增加本地化目录导出和应用接口，把可翻译文本与受保护的结构占位符分开，并在应用前校验译文仍保持原结构。

**Assistant**

Let me look at the source, pangu extension, html_renderer and footnote extension to complete my understanding:

Let me check a few remaining details in the inline parser and renderer context tests:


I now have a complete picture of the codebase. Let me update my plan and design the solution.


Now I'll implement the localization module. The design:

- **导出**：每个承载 inline 内容的块（段落/标题/表格单元格）生成一个条目，整句为翻译单位，链接/强调/代码等结构替换为 `{0}` 占位符；链接文本、强调内容等递归为嵌套翻译段；代码、URL、HTML 等完全保护。
- **应用**：占位符允许重序但必须完整，结构片段由我方从 AST 渲染（译者接触不到 markdown 语法），再用同一 parser 重新解析并做**结构骨架比对**，不一致即拒绝。


Now expose the public API in `marko/__init__.py`:


Now let me run a quick smoke test to validate the design before writing formal tests:


Bug found — `Counter(dict)` counts values instead of keys. Fix:


The protected-attrs fallback wrongly catches emphasis/strong/strikethrough (they have no protected attrs, but shouldn't be treated as unknown elements):


Core flow works. Now test edge cases — GFM, line breaks (including line break at end of link text), images, literal braces, headings/lists/tables:


IDs were right in the code; my test script guessed wrong paths (blank lines occupy indices). Fix the script:


The `- [ ]task` and `| --- |` formatting differences are pre-existing `MarkdownRenderer` behavior (checkbox space and table delimiter normalization), not caused by localization. Let me verify against the untranslated baseline and continue:


All the tricky edge cases pass (hard/soft breaks, line break at end of link text). My test expectation for code-block indices was wrong (`4` is the code block itself; para is `6`). Let me verify remaining cases — footnotes, quotes, reference links, inline HTML, parsed Document input:


Export is exactly right (autolink correctly has no nested segment; literals split correctly). Fix my test script's placeholder index and verify apply output:


That mismatch test was flawed — with no translation the entry is skipped (by design), and "Goodbye *world*" is structurally identical anyway. The real mismatch case is different structure:


All scenarios pass. Now let me write the formal test suite:


Let me clean up the convoluted line-break test:


Now run the new tests:


36 passed; the one failure is a bug in my test itself — compared default (non-GFM) HTML against GFM HTML. Fix the comparison to use GFM for both sides:


Found a real bug: GFM task-list paragraphs have a structurally-required leading space (stripped from the checkbox prefix) that translators naturally drop, producing `- [ ]任务` which loses the checkbox on re-parse. The catalog should preserve boundary whitespace. Let me fix `_apply_segment` to preserve leading/trailing whitespace at segment boundaries:


The hard-break case is already safe (hard breaks render with `\`), but task-list leading space needs boundary preservation. Add the helper:


`re` is already imported. Now add the helper after `_tokenize`:


Update the GFM test to assert the checkbox survives, and rerun:


Path is `0.0.0` (List → ListItem → Paragraph), not `0.0`:


All 38 new tests pass. Now run the full test suite to check for regressions:


All 1474 tests pass. Now check type/lint compliance (the project uses mypy + pre-commit):


mypy found type-narrowing issues (black/isort invocations just lost the env prefix). Let me fix the type issues in `l10n.py`:


Now fix `_hint_attrs`, `_build_segment`, and `_build_placeholder` typing:


Wait — `RawText` is itself an `InlineElement`, so the filter must still exclude it:


Now fix the `root_node` optional, `_render_translated_element`, `_protected_attrs`, and `_skeleton`:


Only a pre-existing optional-dependency warning in `element.py` (`objprint`, the `marko[repr]` extra). Let me confirm it's pre-existing and run the full-package mypy plus black/isort/flake8 properly:


mypy is clean except the pre-existing `objprint` optional-import note. Let me format with black and verify:


The 5 reformat-needing files are pre-existing (newer black version). My files are clean. Re-run the full test suite after formatting:


All tests pass and mypy is clean (the lone error is the pre-existing `objprint` optional dep). One correctness improvement: make `apply` validate *all* entries before mutating any node, so a failure leaves the document truly unchanged (currently early entries would be mutated before a later entry raises):


Add a regression test for the atomicity guarantee:


Let me check flake8 config (line length) and verify no long lines in my files:


Strong robustness check: use every entry's own source as translation (forcing the whole document through re-parse + skeleton validation) on the real sample documents:


Identity roundtrip works on all real samples. Let me check whether `docs/api.rst` explicitly lists modules (keeping docs consistent without creating new docs):


I'll add a small API reference section for the new public API to the existing docs:


Final review of the complete module:


Small cleanup — export doesn't render anything (attrs come straight from elements), so the renderer context isn't needed there:


Final full verification — all tests, formatting, and types:


All **1475 tests pass**, mypy is clean for the new code (the single remaining note is a pre-existing optional `objprint` import in `element.py`), and black formatting is satisfied.


已完成。针对「直接重解析让译者改坏结构 / 逐个 RawText 替换丢失句子上下文」的问题，实现了句子级本地化目录的导出与应用闭环。

## 核心设计

新增模块 [l10n.py](file:///home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip/marko/l10n.py)：

**导出（`Catalog.export`）**
- 以每个承载 inline 内容的块为翻译单位（段落、标题、setext 标题、表格单元格、脚注正文等），整句可见，不再是孤立的 RawText 片段
- 结构性 inline 元素替换为 `{0}`、`{1}` 占位符，译者完全接触不到 markdown 语法
- 分级保护：强调/粗体/删除线/链接文本/图片 alt 文本递归为**嵌套翻译段**（保留完整上下文）；代码 span、链接 URL/title、自动链接、HTML 标签、换行（软/硬）、反斜杠转义、脚注引用为**完全保护叶子**，并作为 `attrs` 提示输出（如 `dest`、`content`）
- 代码块/缩进代码块/HTML 块不导出；只有代码没有可译文本的条目自动跳过
- 条目 ID 为文档结构路径（如 `2.1.1`、`4.0.0`），文档其他部分改动时仍稳定

**应用（`Catalog.apply`）——三重校验**
1. 占位符词法校验：`{n}` 必须存在、无未知/重复/缺失，允许重序以适配不同语序；字面花括号用 `{{`/`}}` 转义
2. 译文与占位符 markdown 重组后，用**同一套 parser 配置**重新解析
3. 结构骨架（结构节点多重集合 + 受保护属性，忽略文本内容）与原文比对——译者夹带 `**加粗**`、`` `代码` ``、`[链接](evil)` 等任何结构改动都会被精确拒绝，错误定位到条目（含嵌套路径如 `0.2`）

另保留了任务列表 `[ ]` 分隔空格等译者看不到但有语法意义的边界空白；所有条目校验通过后才一次性写回 AST，失败时文档不变。

**对接 TMS**：`to_dict()`/`from_dict()` 支持 JSON 往返；扩展（GFM、脚注）通过传入配置好的 `Markdown` 实例自动适配 parser/renderer。

## 用法示例

```python
from marko.l10n import Catalog

catalog = Catalog.export(text)          # 或 Catalog.export(text, markdown=md)
json.dump(catalog.to_dict(), f, ...)    # 交给译者，填 translation 字段
catalog = Catalog.from_dict(json.load(f))
translated = catalog.apply(text)        # 结构不合法时抛 CatalogError
```

## 改动文件

- [marko/l10n.py](file:///home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip/marko/l10n.py)（新增）：`Catalog`、`CatalogEntry`、`Segment`、`Placeholder`、`CatalogError` 及 `export_catalog`/`apply_translations`
- [marko/__init__.py](file:///home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip/marko/__init__.py)：导出公共 API
- [tests/test_l10n.py](file:///home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip/tests/test_l10n.py)（新增）：39 个测试，覆盖占位符重序、嵌套翻译、软/硬换行（含链接文本末尾换行边界）、图片 alt、GFM 表格/删除线/任务列表/裸链、脚注、JSON 往返、各类结构篡改拒绝、原子性
- [docs/api.rst](file:///home/admin/code/trae/swe/repo/marko-swe/worktrees/q3-localization-catalog-roundtrip/docs/api.rst)：Localization API 章节

验证结果：新增 39 个测试全部通过；全量 **1475 个测试无回归**；在 `syntax.md`/`jquery.md`/`demo.md` 三个真实样例上做"以源文当译文"的全量往返（强制每段都过重解析+骨架校验），CommonMark 与 GFM 配置下均与 `MarkdownRenderer` 基线完全一致；mypy/black 对新代码通过。

---

