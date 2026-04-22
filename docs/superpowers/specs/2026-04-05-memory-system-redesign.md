# 记忆系统重新设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create the implementation plan.

**Goal:** 将记忆系统从三种类型简化为两种类型，统一字段结构为标题+内容+关键词，拆分为两张表。

**Architecture:**

- 两张独立的 SQLite 表：`user_preferences`（全局）和 `project_memories`（按项目隔离）
- FTS5 全文检索建在每张表上，关键词字段专用于检索
- 用户偏好通过 `inject_context()` 每次对话自动注入，不需要主动搜索
- 项目记忆通过 MCP 工具 `MemorySearch` 按需搜索，按项目路径隔离
- 旧表 `memories` 及其 FTS 表废弃，数据清空

**Tech Stack:** SQLite + FTS5, Python, MCP tools

**Prompt 引导语（MEMORY_SYSTEM_GUIDE）：**

```
【记忆系统使用指引】
遇到报错、构建失败、工具执行异常时，优先用 MemorySearch 搜索项目记忆。
解决问题后主动问用户："需要记住吗？" 用户确认后用 MemoryAdd 写入（标题+内容+关键词三样必填）。
用户说"记住 XXX"时，直接调用 MemoryAdd 写入。
```

更新位置：`cc_feishu_bridge/claude/memory_manager.py` 中的 `MEMORY_SYSTEM_GUIDE` 常量。

---

## 存储结构

数据库路径：`~/.cc-feishu-bridge/memories.db`

### 表1：`user_preferences`（用户偏好，全局）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PRIMARY KEY | UUID 前8位 |
| `title` | TEXT NOT NULL | 标题 |
| `content` | TEXT NOT NULL | 内容 |
| `keywords` | TEXT NOT NULL | 关键词（FTS5 检索用，逗号分隔） |
| `created_at` | TEXT NOT NULL | 创建时间（ISO） |
| `updated_at` | TEXT NOT NULL | 更新时间（ISO） |

FTS5 虚拟表：`user_preferences_fts`，建在 `title`, `content`, `keywords` 三列。

### 表2：`project_memories`（项目记忆，按项目隔离）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PRIMARY KEY | UUID 前8位 |
| `project_path` | TEXT NOT NULL | 项目路径（隔离键） |
| `title` | TEXT NOT NULL | 标题 |
| `content` | TEXT NOT NULL | 内容 |
| `keywords` | TEXT NOT NULL | 关键词（FTS5 检索用，逗号分隔） |
| `created_at` | TEXT NOT NULL | 创建时间（ISO） |
| `updated_at` | TEXT NOT NULL | 更新时间（ISO） |

FTS5 虚拟表：`project_memories_fts`，建在 `title`, `content`, `keywords` 三列。

---

## 行为

### 注入（inject_context）

每次对话时，读取 `user_preferences` 全表，格式化为字符串，注入到 CC 的 prompt 中。**不搜，直接全量注入**。

格式：
```
【用户偏好】
---
**标题**
内容

**标题**
内容
```

### 搜索（MemorySearch）

只搜 `project_memories` 表，按 `project_path` 精确匹配当前项目。搜索 `project_memories_fts`，返回 BM25 排序结果。

### 添加（MemoryAdd）

CC 调用时，**标题 + 内容 + 关键词三样必填**，缺一不可。
- 用户偏好：存入 `user_preferences`
- 项目记忆：存入 `project_memories`，自动填入当前 `project_path`

### 删除/清空

- `MemoryDelete`：按 ID 删除
- `MemoryClear`：按 `project_path` 删除该项目下所有 `project_memories`

---

## 旧数据处理

- 旧表 `memories` 和 `memories_fts` 废弃
- 首次启动新版本时执行 `DROP TABLE IF EXISTS memories` 和 `DROP TABLE IF EXISTS memories_fts`
- 旧数据全部清空，新系统从零开始
- 用户偏好（主人信息、狗蛋职责、发版流程等）由实现者通过 MemoryAdd 重新录入

---

## 涉及文件

- `cc_feishu_bridge/claude/memory_manager.py` — 完全重写，删旧表建新表
- `cc_feishu_bridge/claude/memory_tools.py` — 适配新 MCP 工具签名
- `cc_feishu_bridge/feishu/message_handler.py` — 适配新 `inject_context()` 接口
- `cc_feishu_bridge/main.py` — CLI 参数适配
- `tests/test_memory_manager.py` — 适配新测试

---

## 迁移步骤

1. `memory_manager.py` 初始化时检测旧表是否存在，若存在则 `DROP TABLE`
2. 创建新表 `user_preferences` + `user_preferences_fts`
3. 创建新表 `project_memories` + `project_memories_fts`
4. 旧数据清空，新系统就绪
