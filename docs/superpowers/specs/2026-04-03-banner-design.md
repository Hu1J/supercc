# cc-feishu-bridge 启动 Banner 设计

## 背景

v0.1.4 主动推送功能上线后，bridge 启动时没有任何品牌展示，缺少辨识度。用户希望参照 vllm 等工具，在终端和日志文件里都加上 ASCII 字符画风格的品牌 banner。

## 目标

1. 终端每次启动时打印蓝紫色大字符画 logo
2. 日志文件每次新 session 开头写迷你版 ASCII banner
3. 不引入任何新依赖，只用 ANSI 转义码

---

## 设计

### 1. 终端大字符画 Banner

**触发时机：** `main()` 最开头，logging 初始化之前

**内容：** 手工绘制的 block ASCII art（80 字符宽度内）

```
  ████▀▀▀███▄▄▄▄▄     ▄▄▄██▀▀▀▀▀▀██▄▄▄
  ██▀▀▀▀▀▀▀▀▀▀▀██       ██▀▀▀▀▀▀▀▀▀▀▀██
  ██  ████████  ██  ██  ██  █████████  ██
  ▀▀▀██▄▄▄▄▄▄▄▄██▀▀▀  ▀▀▀██▄▄▄▄▄▄▄▄▄██▀▀▀
  ██▄▄▄▄▄▄▄▄▄▄██       ██▄▄▄▄▄▄▄▄▄▄▄██
  ▀▀▀████████▀▀▀         ▀▀▀████████▀▀▀

  cc-feishu-bridge  v0.1.4
```

**配色：**
- 字符画主体：飞书蓝 `\033[34m`
- 第二部分 + 底部版本行：Claude 紫 `\033[35m`
- 纯白版本号文字： `\033[37m`
- 重置：`\033[0m`

**输出目标：** `sys.__stdout__`（绕过可能的 stdout reconfigure 问题，直接写到原始 stdout）

---

### 2. 日志文件 Banner

**触发时机：** 日志文件存在且非空时不追加；每次 bridge 新启动 session 写一次

**内容（迷你版，3-5 行）：**

```
========================================
  cc-feishu-bridge  v0.1.4
  started at 2026-04-03 07:05:44
========================================

```

**实现方式：** 在 `logging.FileHandler` 配置好后，以追加模式打开文件，写入 banner 后再让 logging 正常写日志

---

### 3. 实现文件

新建 `cc_feishu_bridge/banner.py`：

```python
# cc_feishu_bridge/banner.py
def print_banner(version: str) -> None:
    """Print large ASCII art banner to terminal."""

def write_log_banner(log_file: str, version: str) -> None:
    """Append banner to log file if session is new (file is empty or doesn't exist)."""
```

---

## 数据流

```
main()
  └─> print_banner(version)        # 终端，sys.__stdout__
  └─> 配置 logging (FileHandler)
  └─> write_log_banner(log_file)  # 日志文件（只在文件为空时）
  └─> 后续日志正常写入
```

---

## 错误处理

- `print_banner` 不抛异常，写入失败静默跳过
- `write_log_banner` 检查文件存在性，不存在则创建父目录，文件已存在且非空则跳过

---

## 测试

- 人工验证：重启 bridge，看终端是否显示彩色 banner，日志文件是否写入迷你版
- 自动化：单元测试 `tests/test_banner.py`，验证：
  - `write_log_banner` 在空文件时写入内容
  - `write_log_banner` 在非空文件时不追加
  - `write_log_banner` 在父目录不存在时创建目录
