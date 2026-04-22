# cc-feishu-bridge 记忆系统设计

**状态**: 草稿
**日期**: 2026-04-05
**负责人**: 姚日华

---

## 1. 背景与目标

### 1.1 核心痛点

Claude Code 在代码开发、构建、测试过程中，曾经遇到并成功解决的问题，下次再遇到时 AI 完全不记得。又得重新折腾一通。用户反复强调的事项，每次都要重新说一遍。

**目标**: 踩过的坑以后绝不再踩，反复强调的事不用再说第二遍。

### 1.2 设计原则

- **CC 主动检索为主**: 通过系统提示词引导 CC 遇到问题时主动调用 MemorySearch 工具
- **不依赖 CLI Hook**: bridge 是 WS 长连接，不走 Claude Code CLI，不使用 SessionStart 等生命周期 Hook
- **本地优先**: 所有记忆存储在本地 SQLite，不上传任何数据
- **FTS5 检索**: 用 SQLite 内置全文搜索，无需额外向量库依赖

---

## 2. 记忆类型与条目结构

### 2.1 四种记忆类型

| type | 说明 | 示例 |
|------|------|------|
| `problem_solution` | 踩过的坑 + 解决方案 | npm install 报错 → 删 node_modules 重装 |
| `project_context` | 项目背景知识 | 项目用 pnpm，禁用水菜 |
| `user_preference` | 用户偏好 | 用户要求所有代码用中文注释 |
| `reference` | 技术参考 | 某个 API 要用 v2 版本 |

### 2.2 记忆条目字段

```sql
CREATE TABLE memories (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK(type IN ('problem_solution','project_context','user_preference','reference')),
    status      TEXT NOT NULL DEFAULT 'active',   -- active | resolved
    title       TEXT NOT NULL,
    problem     TEXT,                              -- 问题描述（problem_solution 必填）
    root_cause  TEXT,                              -- 根因分析
    solution    TEXT NOT NULL,                     -- 解决方案
    tags        TEXT,                              -- 逗号分隔的标签列表，用于检索
    project_path TEXT,                             -- 关联项目路径（NULL 表示全局）
    user_id     TEXT,                             -- 关联用户（NULL 表示全局）
    file_context TEXT,                             -- 相关文件路径，逗号分隔
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_used_at TEXT,
    use_count   INTEGER DEFAULT 0
);

-- FTS5 全文搜索索引，覆盖问题、根因、解决方案、标签
CREATE VIRTUAL TABLE memories_fts USING fts5(
    title, problem, root_cause, solution, tags,
    content='memories',
    content_rowid='rowid'
);
```

---

## 3. 系统架构

```
飞书消息 / CC 执行中
        ↓
  MessageHandler.handle()
        ↓
  ┌─ 关键词触发 / 报错信号检测
  │   ↓
  │   MemoryManager.inject_context()
  │   └── 注入相关记忆到 system prompt 前缀
  │
  └─ CC 主动调用 MemorySearch 工具
          ↓
          MemoryManager.search()
          └── 返回检索结果，注入上下文
                  ↓
          CC 参考记忆 → 再决定是否自己研究
                  ↓
          成功后 → MemoryManager.extract_and_save()
          └── 调用 LLM 提取摘要，写入记忆库
```

---

## 4. 核心组件

### 4.1 MemoryManager (`cc_feishu_bridge/claude/memory_manager.py`)

| 方法 | 职责 |
|------|------|
| `search(query, project_path, limit)` | FTS5 全文检索，返回匹配记忆 |
| `inject_context(query, project_path, user_id)` | 构造记忆注入字符串，插入 system prompt 前缀 |
| `add(memory_entry)` | 新增一条记忆 |
| `update(id, fields)` | 更新记忆（增加 use_count、更新 last_used_at） |
| `delete(id)` | 删除记忆 |
| `extract_and_save(conversation_transcript, project_path, user_id)` | 调用 LLM 提取摘要，自动写入记忆库 |
| `get_by_project(project_path)` | 获取某项目的全部记忆 |

### 4.2 触发时机

#### CC 主动检索（主引擎）
通过系统提示词（system prompt）引导 CC 在遇到问题时调用 MemorySearch：

> 遇到报错、失败或不熟悉的问题时，**优先**使用 MemorySearch 工具查询本地记忆库，看看是否有已知解决方案。

CC 会在遇到问题时主动调用 bridge 暴露的 `memory_search` 工具。

#### 关键词触发（辅助）
在用户消息或 CC 执行结果中检测关键词：
- `error`、`bug`、`失败`、`crash`、`不工作`、`踩坑`
- `之前也是这样`、`试过`、`解决过`

检测到时自动注入相关记忆到下一轮 system prompt。

### 4.3 MemorySearch 工具

暴露为 Claude SDK 的工具之一：

```json
{
  "name": "MemorySearch",
  "description": "搜索本地记忆库，查找之前遇到过的问题和解决方案",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "问题描述或关键词"
      },
      "project_path": {
        "type": "string",
        "description": "当前项目路径（可选，空表示全局搜索）"
      }
    },
    "required": ["query"]
  }
}
```

### 4.4 记忆自动提取

当 CC 成功解决一个问题时（特别是报错修复），调用 LLM 提取摘要：

**提取 Prompt**:
```
从以下对话记录中提取值得记忆的信息：

{conversation}

如果这是一个问题解决过程，提取：
1. 问题描述
2. 根因
3. 解决方案
4. 相关标签（3-5个英文单词）
5. 相关文件

以 JSON 格式输出，如果没有值得记忆的内容返回 null。
```

---

## 5. 数据存储

- **位置**: `~/.cc-feishu-bridge/memories.db`（与 sessions.db 同目录）
- **SQLite + FTS5**: 内置全文搜索，无需额外依赖
- **中文支持**: FTS5 启用 CJK tokenization

---

## 6. 指令接口

通过飞书发送指令管理记忆：

| 指令 | 说明 |
|------|------|
| `/memory` | 显示当前项目记忆列表 |
| `/memory add <内容>` | 手动添加一条记忆 |
| `/memory search <关键词>` | 手动检索记忆 |
| `/memory delete <id>` | 删除一条记忆 |
| `/memory clear` | 清除当前项目全部记忆 |

---

## 7. 集成点

### 7.1 已有能力复用

| 组件 | 复用方式 |
|------|------|
| `SessionManager` | 复用同一 SQLite 连接池 |
| `integration.py` | 使用现有 LLM 调用能力做摘要提取 |
| `Session.project_path` | 记忆与项目路径关联 |
| `MessageHandler` | 在 `_run_query()` 前注入记忆上下文 |

### 7.2 改动范围

新增文件：
- `cc_feishu_bridge/claude/memory_manager.py` — 核心记忆管理
- `cc_feishu_bridge/claude/memory_tools.py` — MemorySearch 工具定义

修改文件：
- `cc_feishu_bridge/claude/integration.py` — 注册 MemorySearch 工具
- `cc_feishu_bridge/feishu/message_handler.py` — 添加 /memory 指令路由
- `cc_feishu_bridge/claude/session_manager.py` — 初始化 memories 表

---

## 8. 非功能要求

- **隐私**: 所有数据本地存储，不上传
- **性能**: FTS5 检索 < 50ms（1000条记忆规模）
- **可靠性**: 记忆提取失败不影响主流程，吞掉异常并记录日志
- **可清理**: 支持按时间、按项目、按类型删除记忆

---

## 9. 未来扩展方向（不纳入本期）

- 向量检索升级（SQLite → Qdrant/Chroma）
- 多用户记忆共享
- 记忆条目手动编辑（`/memory edit`）
- 记忆使用统计（哪些记忆被命中最多）
