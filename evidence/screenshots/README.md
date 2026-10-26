# q3 截图方式

q3 的产物是 Python 库能力，没有图形界面，因此截图采用真实可复现的运行记录，而不是手工截桌面：

1. 在 q3 工作树运行 `PYTHONPATH=. python3 evidence/demo_q3.py`，用 `tee` 保存完整 stdout/stderr 到 `../logs/q3-demo.log`。
2. 运行专项、全量、格式和类型检查，日志保存到 `../logs/`。
3. `PYTHONPATH=. python3 evidence/render_screenshots.py` 把真实日志和 rubric 结果渲染成自包含 HTML。
4. 脚本调用 headless Google Chrome 输出 PNG，核心参数如下：

```bash
google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor=1 --window-size=1500,1350 \
  --screenshot=/absolute/path/evidence/screenshots/q3-artifact-demo.png \
  file:///absolute/path/evidence/screenshots/q3-artifact-demo.html
```

本次两张图：

- `q3-artifact-demo.png`：真实导出、应用、结构注入拒绝，以及两个结构保真失败用例。
- `q3-review-report.png`：7 条 rubric、完成度、Type、测试结果与缺陷总览。

原始日志在 `../logs/`，截图生成脚本为 `../render_screenshots.py`。
