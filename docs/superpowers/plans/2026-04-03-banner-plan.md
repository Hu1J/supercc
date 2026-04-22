# Banner 功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在终端启动时打印蓝紫色 ASCII 大字符画，在日志文件开头写迷你版 banner，全程无新依赖。

**Architecture:** 两个纯函数放在新建的 `banner.py`，`main()` 入口处调用。版本号从 `importlib.metadata` 动态读取，避免硬编码。

**Tech Stack:** 标准库 only（`sys`, `os`, `pathlib`, `datetime`, `importlib.metadata`），ANSI 转义码配色。

---

## 文件结构

- **Create:** `cc_feishu_bridge/banner.py` — 两个导出函数
- **Modify:** `cc_feishu_bridge/main.py:418-505` — `main()` 开头和日志配置处
- **Create:** `tests/test_banner.py` — 3 个单元测试

---

## Task 1: 编写 banner.py 及单元测试

**Files:**
- Create: `cc_feishu_bridge/banner.py`
- Create: `tests/test_banner.py`

- [ ] **Step 1: 写测试 — write_log_banner 在空文件时写入内容**

```python
"""Tests for banner module."""
import os
import tempfile
from cc_feishu_bridge.banner import write_log_banner


class TestWriteLogBanner:
    """Test write_log_banner behavior."""

    def test_writes_banner_when_file_empty(self):
        """An empty file gets the banner written to it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test.log")
            # File exists but is empty
            open(log_path, "w").close()
            write_log_banner(log_path, "0.1.4")
            content = open(log_path).read()
            assert "cc-feishu-bridge" in content
            assert "0.1.4" in content
            assert "started at" in content
```

Run: `pytest tests/test_banner.py::TestWriteLogBanner::test_writes_banner_when_file_empty -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cc_feishu_bridge.banner'`

- [ ] **Step 2: 写测试 — 非空文件不追加**

```python
    def test_does_not_append_when_file_has_content(self):
        """A non-empty file is left untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test.log")
            original = "2026-04-03 12:00:00 INFO hello world\n"
            open(log_path, "w").write(original)
            write_log_banner(log_path, "0.1.4")
            content = open(log_path).read()
            assert content == original
```

Run: `pytest tests/test_banner.py::TestWriteLogBanner::test_does_not_append_when_file_has_content -v`
Expected: FAIL — module still missing

- [ ] **Step 3: 写测试 — 父目录不存在时创建**

```python
    def test_creates_parent_directory(self):
        """Parent dir is created if missing."""
        import shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "subdir", "deep", "test.log")
            assert not os.path.exists(os.path.dirname(log_path))
            write_log_banner(log_path, "0.1.4")
            assert os.path.exists(log_path)
            assert "cc-feishu-bridge" in open(log_path).read()
```

Run: `pytest tests/test_banner.py -v`
Expected: FAIL — module missing

- [ ] **Step 4: 写 banner.py**

```python
"""Banner — terminal ASCII art and log file header."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path


BLUE = "\033[34m"
PURPLE = "\033[35m"
WHITE = "\033[37m"
RESET = "\033[0m"

# 80-char wide ASCII art: "CC" in blue, "FB" in purple
# Version line in white/purple below
TERMINAL_ART = f"""
{BLUE}  ████▀▀▀███▄▄▄▄▄     ▄▄▄██▀▀▀▀▀▀██▄▄▄
  ██▀▀▀▀▀▀▀▀▀▀▀██       ██▀▀▀▀▀▀▀▀▀▀▀██
  ██  ████████  ██  ██  ██  █████████  ██
  ▀▀▀██▄▄▄▄▄▄▄▄██▀▀▀  ▀▀▀██▄▄▄▄▄▄▄▄▄██▀▀▀
  ██▄▄▄▄▄▄▄▄▄▄██       ██▄▄▄▄▄▄▄▄▄▄▄██
  ▀▀▀████████▀▀▀         ▀▀▀████████▀▀▀
{RESET}
  {PURPLE}cc-feishu-bridge  {WHITE}v{version}{RESET}
"""


def print_banner(version: str) -> None:
    """Print the large ASCII art banner to terminal (sys.__stdout__)."""
    try:
        out = sys.__stdout__
        out.write(TERMINAL_ART.format(version=version))
        out.write("\n")
        out.flush()
    except Exception:
        pass  # Never crash on banner output


def write_log_banner(log_file: str, version: str) -> None:
    """Write mini banner to log file if it is empty or doesn't exist."""
    p = Path(log_file)
    # Create parent dirs
    p.parent.mkdir(parents=True, exist_ok=True)

    # Only write if file doesn't exist or is empty
    if p.exists() and p.stat().st_size > 0:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner = (
        f"{'=' * 40}\n"
        f"  cc-feishu-bridge  v{version}\n"
        f"  started at {timestamp}\n"
        f"{'=' * 40}\n\n"
    )
    with open(p, "a", encoding="utf-8") as f:
        f.write(banner)
```

Run: `pytest tests/test_banner.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: 提交**

```bash
git add cc_feishu_bridge/banner.py tests/test_banner.py
git commit -m "$(cat <<'EOF'
feat: add banner module — terminal ASCII art and log file header

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 集成到 main.py

**Files:**
- Modify: `cc_feishu_bridge/main.py:447-506`

- [ ] **Step 1: 确认注入位置**

在 `main.py` 中找到：
- `args = parser.parse_args(args)` 之后（第 445 行附近）
- logging 初始化 `_stdout_handler` 之前 — 调用 `print_banner`
- `logging.FileHandler` 配置后、`start_bridge()` 调用前 — 调用 `write_log_banner`

确认版本号读取位置：从 `importlib.metadata` 读取。

- [ ] **Step 2: 添加 import 并在最开头调用 print_banner**

在 `main.py` 顶部已有的 `import logging` 附近加一行：
```python
from cc_feishu_bridge.banner import print_banner, write_log_banner
```

在 `args = parser.parse_args(args)` 之后、`try: sys.stdout.reconfigure` 之前加：
```python
    # Print banner before any logging setup
    try:
        from importlib.metadata import version as get_version
        _version = get_version("cc-feishu-bridge")
    except Exception:
        _version = "dev"
    print_banner(_version)
```

- [ ] **Step 3: 在 FileHandler 配置后调用 write_log_banner**

在 `main.py` 中找到这段（约第 496-504 行）：
```python
    # Set up logging to file
    log_file = os.path.join(data_dir, "cc-feishu-bridge.log")
    Path(data_dir).mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    if is_installed:
        logger.info(f"Config found, starting bridge...")
```

在 `logging.getLogger().addHandler(fh)` 之后加一行：
```python
    write_log_banner(log_file, _version)
```

- [ ] **Step 4: 人工验证**

重启 bridge，观察：
- 终端是否显示蓝紫色 ASCII 字符画
- 日志文件顶部是否写了迷你版 banner

- [ ] **Step 5: 提交**

```bash
git add cc_feishu_bridge/main.py
git commit -m "$(cat <<'EOF'
feat: print ASCII banner on startup

- Terminal: blue/purple block-art logo before logging
- Log file: mini banner on first write

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 重新构建 CLI 并推送

**Files:**
- None (build artifact only)

- [ ] **Step 1: 构建 CLI**

```bash
/opt/anaconda3/bin/python build_cli.py 2>&1
```
Expected: PyInstaller builds successfully, `dist/cc-feishu-bridge` updated

- [ ] **Step 2: 提交构建产物并 tag**

```bash
git add dist/
git commit -m "$(cat <<'EOF'
chore: build CLI binary with banner feature

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
git tag -a v0.1.5 -m "$(cat <<'EOF'
v0.1.5 — 启动 Banner：终端蓝紫 ASCII 大字符画 + 日志文件迷你版

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
git push origin main && git push origin v0.1.5
```

---

## Self-Review Checklist

- [x] Spec 第1点（终端大字符画）→ Task 1 `print_banner` + Task 2 集成
- [x] Spec 第2点（日志迷你版）→ Task 1 `write_log_banner` + Task 2 集成
- [x] Spec 第3点（无新依赖）→ 只用标准库
- [x] Spec 数据流（print → logging setup → write_log_banner → 正常日志）→ Task 2 步骤顺序一致
- [x] Spec 错误处理（静默跳过）→ `try/except pass` in `print_banner`，目录创建在 `write_log_banner`
- [x] 测试3个 case → `test_writes_banner_when_file_empty`、`test_does_not_append_when_file_has_content`、`test_creates_parent_directory`
- [x] 无 TBD/TODO 占位符
- [x] 函数签名一致（`write_log_banner(log_file: str, version: str)` 贯穿所有步骤）
