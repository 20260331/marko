"""Render real q3 logs and the rubric review into reproducible PNG screenshots."""

from __future__ import annotations

import html
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)

TERMINAL_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #10151b; color: #d8e1ea;
       font-family: "DejaVu Sans Mono", "Noto Sans CJK SC", monospace; }
.wrap { padding: 30px 34px; }
.bar { display: flex; gap: 8px; margin-bottom: 14px; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.red { background: #ff5f57; } .yellow { background: #febc2e; } .green { background: #28c840; }
h1 { font-size: 20px; color: #fff; margin: 0 0 8px; font-family: "Noto Sans CJK SC", sans-serif; }
.sub { color: #8ea1b4; font-size: 13px; margin-bottom: 18px; }
pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.55; }
.ok { color: #7ee787; } .warn { color: #ffd166; } .bad { color: #ff7b72; }
"""

REPORT_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #edf2f6; color: #17212b;
       font-family: "Noto Sans CJK SC", "DejaVu Sans", sans-serif; }
.wrap { width: 1500px; padding: 34px 42px 48px; }
.card { background: #fff; border-radius: 12px; padding: 26px 30px; margin-bottom: 20px;
        box-shadow: 0 1px 4px rgba(23,33,43,.10); }
h1 { margin: 0 0 8px; font-size: 30px; }
h2 { margin: 0 0 16px; font-size: 21px; }
.sub, .meta { color: #58697a; font-size: 15px; line-height: 1.7; }
.meta { margin-top: 10px; }
code { background: #e9eff4; padding: 2px 6px; border-radius: 4px;
       font-family: "DejaVu Sans Mono", monospace; font-size: 13px; }
.badges { display: flex; align-items: center; gap: 14px; margin-top: 20px; }
.badge { color: white; border-radius: 8px; padding: 12px 20px; font-weight: 700; font-size: 21px; }
.partial { background: #b56a13; } .badge.type { background: #176b87; font-size: 15px; padding: 13px 17px; }
.summary { margin-top: 18px; font-size: 17px; line-height: 1.75; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #e2e9ef; padding: 11px 12px; text-align: left;
         vertical-align: top; font-size: 15px; line-height: 1.55; }
th { color: #58697a; font-size: 13px; letter-spacing: .04em; }
td.id { width: 38px; font-weight: 700; } td.type { width: 58px; color: #8a5a00;
       font-family: "DejaVu Sans Mono", monospace; font-size: 13px; }
td.pass { color: #1a7f37; font-weight: 700; width: 72px; }
td.fail { color: #c62828; font-weight: 700; width: 72px; }
.defect { border-left: 5px solid #c62828; background: #fff6f5; padding: 15px 18px;
          margin-top: 14px; font-size: 15px; line-height: 1.7; }
.evidence { margin: 0; padding-left: 22px; font-size: 15px; line-height: 1.85; }
.evidence b { color: #17212b; }
.foot { color: #667787; font-size: 13px; margin-top: 14px; }
"""


def chrome_screenshot(name: str, width: int, height: int) -> None:
    source = (SHOTS / f"{name}.html").resolve()
    target = SHOTS / f"{name}.png"
    subprocess.run(
        [
            "google-chrome",
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            f"--screenshot={target}",
            source.as_uri(),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    print(f"wrote {target} ({target.stat().st_size} bytes)")


def render_terminal() -> None:
    log = (LOGS / "q3-demo.log").read_text(encoding="utf-8")
    body = html.escape(log)
    body = body.replace("CatalogError:", '<span class="bad">CatalogError:</span>')
    body = body.replace("re-parsed HTML:", '<span class="warn">re-parsed HTML:</span>')
    document = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>marko q3 artifact demo</title><style>{TERMINAL_CSS}</style></head><body><div class="wrap">
<div class="bar"><span class="dot red"></span><span class="dot yellow"></span><span class="dot green"></span></div>
<h1>marko q3 · 本地化目录真实运行记录</h1>
<div class="sub">PYTHONPATH=. python3 evidence/demo_q3.py · 基准提交 1cc1583 · 分支 swe/q3-localization-catalog-roundtrip</div>
<pre>{body}</pre></div></body></html>"""
    (SHOTS / "q3-artifact-demo.html").write_text(document, encoding="utf-8")
    chrome_screenshot("q3-artifact-demo", 1500, 1350)


def render_report() -> None:
    rubrics = [
        (1, "p2p", "通过", "既有解析/渲染行为不变；全量 1475 passed。"),
        (2, "f2p", "通过", "混合 inline 内容的同一块导出为一个句子级条目，RawText 不再拆散上下文。"),
        (3, "f2p", "通过", "链接目标、代码内容、强调/链接等结构进入占位符与受保护元数据。"),
        (4, "f2p", "未通过", "只有链接或强调文本时，仅填写嵌套译文不生效，还需额外填写外层占位符 {0}，否则静默保持原文。"),
        (5, "f2p", "通过", "缺失、重复、未知占位符及译文注入 Markdown 结构的用例均被拒绝。"),
        (6, "f2p", "未通过", "最终序列化边界未保持结构：强调文本译文 2*3 输出 *2*3*，<em> 只覆盖 2；链接文本译文 ] hacked [ 输出 [] hacked [](https://example.com/docs)。"),
        (7, "f2p", "通过", "结构校验失败时，作为输入的解析文档未被部分修改。"),
    ]
    rows = "\n".join(
        f"<tr><td class='id'>{rid}</td><td class='type'>{kind}</td>"
        f"<td class='{'pass' if status == '通过' else 'fail'}'>{status}</td>"
        f"<td>{html.escape(detail)}</td></tr>"
        for rid, kind, status, detail in rubrics
    )
    document = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>marko q3 review report</title><style>{REPORT_CSS}</style></head><body><div class="wrap">
<div class="card">
<h1>marko q3 · Markdown 结构化本地化接口</h1>
<div class="sub">SWE-like 审核证据 · 2026-10-26 · 有效轮数 73 · 分支 swe/q3-localization-catalog-roundtrip</div>
<div class="meta">基线 <code>1cc1583dd38bc41fecc113e56df4ce9d2d693efa</code> ·
新增 <code>marko/l10n.py</code>、<code>tests/test_l10n.py</code>，修改公共导出与 API 文档</div>
<div class="badges"><span class="badge partial">部分完成</span>
<span class="badge type">Type：有效轮数 &lt; 100 且 效果差</span></div>
<div class="summary">目录导出、占位符隔离、译文应用和多数结构校验已实现；但嵌套译文可能静默漏译，且译文含普通 Markdown 定界符时会静默损坏链接或强调结构，核心结构保真要求未完全满足。</div>
</div>
<div class="card"><h2>Verify Rubric 结果（7 条：6 f2p + 1 p2p）</h2>
<table><tr><th>id</th><th>type</th><th>结果</th><th>说明</th></tr>{rows}</table>
<div class="defect"><b>独立复现：</b>强调文本译文 <code>2*3</code> 输出 <code>*2*3*</code>，HTML 为
<code>&lt;p&gt;&lt;em&gt;2&lt;/em&gt;3*&lt;/p&gt;</code>；链接文本译文 <code>] hacked [</code> 输出
<code>[] hacked [](https://example.com/docs)</code>，可见文本被移出链接。另有 **important** 仅填写嵌套译文 重要 时应用结果仍为 **important**，必须补写外层 {0} 才生效。实现只在插入 AST 前校验骨架，没有校验最终 Markdown 重新解析后的结构。</div></div>
<div class="card"><h2>测试与工程证据</h2><ul class="evidence">
<li><b>专项：</b><code>python3 -m pytest tests/test_l10n.py -q</code> → 39 passed。</li>
<li><b>全量：</b><code>python3 -m pytest -q</code> → 1475 passed。</li>
<li><b>格式：</b>black 检查通过；mypy 仅剩仓库既有的 objprint 可选依赖缺失。</li>
<li><b>工程缺口：</b>项目预提交使用的 flake8-bugbear 对新代码报告 B042/B009，测试文件报告 F541；docs/api.rst 文件尾另有 diff --check 空行提示。</li>
<li><b>环境：</b>isort 未安装，未执行该项检查。</li>
</ul><div class="foot">rubric 与原始 Prompt 的逐条映射见 evidence/rubric_prompt_mapping.md；未把 JSON、扩展支持、类名或文件位置等实现细节写入强制验收。</div></div>
</div></body></html>"""
    (SHOTS / "q3-review-report.html").write_text(document, encoding="utf-8")
    chrome_screenshot("q3-review-report", 1500, 1490)


if __name__ == "__main__":
    render_terminal()
    render_report()
