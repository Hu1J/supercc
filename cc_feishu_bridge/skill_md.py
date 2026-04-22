"""Inline skill content for cc-feishu-send-file.

This module exists so the skill is bundled inside the package — it works
correctly whether the package is installed via pip or run as a PyInstaller binary.
"""
from __future__ import annotations

SKILL_NAME = "cc-feishu-send-file"
SKILL_VERSION = "1.0.0"

SKILL_MD = """\
---
name: cc-feishu-send-file
version: 1.0.0
description: |
  当你需要把本地图片或文件发送给飞书用户时使用。
  调用方式: cc-feishu-bridge send <文件路径> --config <config.yaml路径>
  示例: cc-feishu-bridge send screenshot.png --config /project/.cc-feishu-bridge/config.yaml
  支持图片: png, jpg, jpeg, gif, webp, bmp
  支持文件: pdf, doc, docx, xls, xlsx, zip, txt, csv 等
  config.yaml 路径为当前项目 .cc-feishu-bridge/ 目录下的配置文件
---

## 使用场景

- 你生成了图片（图表、截图、设计稿），需要发给用户
- 你生成了文件（报告、文档），需要发给用户
- 用户要求你把某个文件发到飞书

## 使用方式

```bash
cc-feishu-bridge send /path/to/file.png --config /path/to/.cc-feishu-bridge/config.yaml
```

一次可以发送多个文件（并发上传、并发发送，速度更快）：

```bash
cc-feishu-bridge send chart.png output.pdf --config /path/to/.cc-feishu-bridge/config.yaml
```

## 注意事项

- 使用绝对路径，不要用相对路径
- config.yaml 为当前项目 .cc-feishu-bridge/ 目录下的配置文件
- 飞书对文件大小有限制，单个文件不超过 30MB

---

## 飞书文档操作（lark-cli）

当你需要用飞书开放平台 API 操作文档、日历、联系人等资源时，Claude 需要先安装 Lark CLI 工具。

### 安装与授权（首次使用）

```bash
npm install -g @larksuite/cli
lark-cli auth login --recommend
lark-cli auth status
```

- `lark-cli auth login --recommend`：弹出授权链接，浏览器完成授权。`--recommend` 会自动请求推荐权限范围。
- `lark-cli auth status`：确认授权状态，显示已登录账号。

### 常用操作示例

```bash
# 查看帮助
lark-cli --help

# 查看日历事件
lark-cli calendar +agenda

# 搜索联系人
lark-cli contact +search-user --query "张三"

# 查看 Drive 文件列表
lark-cli drive files list

# 通用 API 调用
lark-cli api GET /open-apis/drive/v1/files
lark-cli api POST /open-apis/docx/v1/documents --data '{"title":"新文档"}'
```

### 权限说明

- `--recommend` 请求的是应用在开放平台已开通的推荐权限。
- 需要更多权限（如 wiki、sheets）时，先在 [飞书开放平台](https://open.feishu.cn/app) 为应用开通对应权限，再用 `lark-cli auth login --scope "wiki:doc:readonly sheets:readonly"` 重新授权。
"""
