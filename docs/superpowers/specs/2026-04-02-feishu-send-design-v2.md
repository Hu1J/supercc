# 飞书图片/文件发送设计（重设计）

## 概述

**项目:** `cc-feishu-bridge`
**功能:** Claude 主动调用 CLI 发送图片/文件到飞书
**原方案:** SDK 拦截 ToolResultBlock（不可靠，SDK 无 ImageBlock）
**新方案:** Claude 通过 skill 学会调用 `cc-feishu-bridge send`，主动发送文件

## 架构

```
Inbound（不变）                              Outbound（新方案）
──────────────                              ─────────────────
用户发图片/文件                               Claude 想把文件发给用户
  ↓                                           ↓
bridge 下载 → 保存本地                      Claude 调用 cc-feishu-bridge send xxx.png
  ↓                                           ↓
路径拼入 prompt                            send: 查 sessions.db → 拿 chat_id
  ↓                                           ↓
Claude 读取本地文件                         FeishuClient.send_image() 或 send_file()
```

**核心变化:** Outbound 不再依赖 SDK 消息解析，完全由 Claude 主动调用。

---

## 一、新增 CLI 子指令 `send`

### 用法

```bash
cc-feishu-bridge send <file_path> [<file_path>...] --config <config.yaml>
```

- `--config`：必选，指定该 bridge 实例的配置文件路径
- 支持同时发送多个文件
- 自动检测文件类型（图片 vs 文件）

### 完整流程

```
1. 解析参数：file_path, config.yaml
2. 加载 config.yaml → 拿 app_id, app_secret
3. 从 config 同目录的 sessions.db 查最近活跃用户的 chat_id
4. 读取每个 file_path 的二进制内容
5. 根据扩展名判断类型：
   - 图片 (png/jpg/gif/webp/bmp): upload_image() → send_image()
   - 文件 (pdf/doc/docx/xlsx/zip/txt 等): upload_file() → send_file()
6. 输出结果
```

### 错误处理

| 场景 | 处理 |
|------|------|
| 文件不存在 | 报错退出 |
| config.yaml 不存在 | 报错退出 |
| sessions.db 查不到 chat_id | 报错退出 |
| 上传失败 | 报错退出 |
| 文件大小超过 30MB | 报错退出 |

---

## 二、cc-feishu-send-file Skill

### 存放位置

源码目录：`skills/cc-feishu-send-file/skill.md`

### 安装方式

bridge 启动时，检查 `~/.claude/skills/cc-feishu-send-file/` 是否存在：
- 不存在 → 拷贝安装
- 存在但版本不一致 → 更新
- 版本一致 → 跳过

安装路径为 `~/.claude/skills/cc-feishu-send-file/skill.md`。

### Skill 内容

```markdown
---
name: cc-feishu-send-file
version: 1.0.0
description: |
  当你需要把本地图片或文件发送给飞书用户时使用。
  调用方式: cc-feishu-bridge send <文件路径> --config <config.yaml路径>
  示例: cc-feishu-bridge send screenshot.png --config /project/.cc-feishu-bridge/config.yaml
  支持图片: png, jpg, jpeg, gif, webp, bmp
  支持文件: pdf, doc, docx, xls, xlsx, zip, txt, csv 等
  config.yaml 路径为当前项目的 .cc-feishu-bridge/config.yaml
---

## 使用场景

- 你生成了图片（图表、截图、设计稿），需要发给用户
- 你生成了文件（报告、文档），需要发给用户
- 用户要求你把某个文件发到飞书

## 使用方式

```bash
cc-feishu-bridge send /path/to/file.png --config /path/to/.cc-feishu-bridge/config.yaml
```

## 注意事项

- 路径使用绝对路径
- config.yaml 为当前项目 .cc-feishu-bridge/ 目录下的配置文件
- 一次可以发送多个文件: `send file1.png file2.pdf --config ...`
```

---

## 三、FeishuClient 新增 API

### upload_file(file_bytes, file_name, file_type) → file_key

调用 `lark_oapi.file.create()`，返回 file_key。

### send_file(chat_id, file_key, file_name) → message_id

调用 `lark_oapi.message.create(msg_type="file", content={"file_key": ..., "file_name": ...})`

---

## 四、Session 表增加 chat_id 字段

每次 bridge 处理消息时，更新 sessions 表该用户的 `chat_id`。

send 命令查 sessions.db 时取出最近一条记录的 `chat_id` 作为发送目标。

### 数据库迁移

```sql
ALTER TABLE sessions ADD COLUMN chat_id TEXT;
```

---

## 五、Skill 安装逻辑

在 `main.py` 的 `start_bridge()` 中，初始化时检查并安装 skill。

版本号从 skill.md 的 frontmatter `version` 字段读取。

---

## 六、Inbound 处理（保持不变）

用户发图片/文件 → 下载保存到 `received_images/` 和 `received_files/` → 本地路径拼入 prompt → Claude 读取。

Inbound 和 outbound 完全独立，互不影响。
