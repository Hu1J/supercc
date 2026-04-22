# cc-feishu-bridge 记忆系统语义搜索改造设计

## 背景与目标

cc-feishu-bridge 当前记忆系统基于 SQLite FTS5 + jieba bm25 关键词检索，无法理解语义（"修 bug" 找不到"调试错误"）。

改造目标：
1. **语义搜索**：引入 qmd-py 做 BM25 + 向量语义混合搜索，大幅提升项目记忆检索准确率
2. **MCP 工具化**：项目记忆通过 MCP 工具由 Agent 主动查询
3. **多用户支持**：用户偏好区分飞书用户（`user_open_id`），解决当前全局共享的问题

## 技术选型

- **qmd[mvp]**：BM25 + sentence-transformers 向量混合搜索 + 内置 MCP Server，纯 Python（py3-none-any.whl），本地离线可用
- **数据目录**：`~/.cc-feishu-bridge/`（与现有配置统一管理）
  - `qmd-index.sqlite`：qmd 的向量索引数据库
  - `qmd-config.json`：qmd collection 配置
- **历史数据**：改造前的 SQLite project_memories 数据不再迁移，改造后新记忆全量走 qmd

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                   cc-feishu-bridge                       │
│                                                          │
│  inject_context()  ─────────────────────────▶            │
│  (每次对话前调用)  用户偏好全量注入（按 user_open_id 过滤）│
│                                                          │
│  MCP 工具层 (memory_tools.py)                            │
│  MemoryAddProj  ───▶ qmd collection write                 │
│  MemoryDeleteProj ──▶ qmd collection remove               │
│  MemoryUpdateProj ─▶ qmd collection update                │
│  MemoryListProj  ───▶ qmd collection list                │
│  MemorySearchProj ─▶ qmd query (语义+关键词混合)           │
│                                                          │
│  用户偏好 CRUD → SQLite (user_preferences 表)             │
└─────────────────────────────────────────────────────────┘
           │                              │
           ▼                              ▼
  ~/.cc-feishu-bridge/          ~/.cc-feishu-bridge/
  memories.db (用户偏好)          qmd-index.sqlite (项目记忆)
  (原有逻辑不变)                 qmd-config.json
```

## 数据存储分工

### 用户偏好（SQLite → `memories.db`）

保持现有 SQLite 方案不变，新增 `user_open_id` 字段区分飞书用户：

```sql
CREATE TABLE user_preferences (
    id           TEXT PRIMARY KEY,
    user_open_id TEXT NOT NULL,   -- 新增：飞书用户 open_id
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    keywords     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
```

- `inject_context()` 注入时按当前飞书 `user_open_id` 过滤
- MCP 工具（MemoryAddUser 等）新增 `user_open_id` 参数

### 项目记忆（qmd → `qmd-index.sqlite`）

qmd collection 配置（`~/.cc-feishu-bridge/qmd-config.json`）：

```json
{
  "dbPath": "~/.cc-feishu-bridge/qmd-index.sqlite",
  "config": {
    "project_memories": {
      "path": "~/.cc-feishu-bridge/memory-docs",
      "pattern": "**/*.md"
    }
  }
}
```

文档格式：每条记忆存为一个 markdown 文件，路径含 project_path 隔离：

```
~/.cc-feishu-bridge/memory-docs/
  /Users/x/project-a/
    abc123.md   → "# 记忆标题\n内容..."
    def456.md
  /Users/x/project-b/
    ...
```

> 注：qmd 支持直接 API 增删改文档，无需依赖文件系统。collection 路径配置仅用于初始化和批量导入。

## MCP 工具设计

### 用户偏好工具（SQLite，不变，新增 user_open_id）

| 工具 | 行为 | 变化 |
|------|------|------|
| `MemoryAddUser` | 写 SQLite | 新增 `user_open_id` 字段 |
| `MemoryDeleteUser` | 删 SQLite | 按 id 删除，无需改 |
| `MemoryUpdateUser` | 改 SQLite | 按 id 修改，无需改 |
| `MemoryListUser` | 读 SQLite | 新增 `user_open_id` 过滤 |
| `MemorySearchUser` | FTS5 搜索 | 新增 `user_open_id` 过滤 |

### 项目记忆工具（qmd MCP Server）

| 工具 | 行为 | 底层调用 |
|------|------|---------|
| `MemoryAddProj` | 新增记忆 | qmd document add |
| `MemoryDeleteProj` | 删除记忆 | qmd document remove |
| `MemoryUpdateProj` | 更新记忆 | qmd document update |
| `MemoryListProj` | 列出记忆 | qmd document list |
| `MemorySearchProj` | **语义+关键词混合搜索** | qmd query（核心能力） |

qmd MCP Server 启动方式：bridge 启动时作为本地 subprocess 启动 `qmd mcp --stdio`，通过 stdio 通信。

## inject_context 改造

改造前：全量注入所有用户偏好（无用户区分）

改造后：按 `user_open_id` 过滤后注入

```python
def inject_context(self, user_open_id: str, project_path: Optional[str]) -> str:
    prefs = self.get_preferences_by_user(user_open_id)  # 新增方法
    # ... 格式不变
```

调用处：需要从飞书消息中取 `user_open_id`，传入 `inject_context()`。

## 降级策略

- **qmd 未安装 / 启动失败**：`MemorySearchProj` 返回"语义搜索暂不可用，请描述需要什么记忆"，Agent 降级提示用户手动补充
- **历史记忆**：改造前写入的 project_memories 在 qmd 中无记录，Agent 查不到，视为正常损耗
- **用户偏好**：SQLite 方案不变，无降级风险

## 项目记忆搜索结果格式化

qmd query 返回的结果是 `{docid, content, score, collection}`，需要格式化为：

```
[项目记忆] **记忆标题**
  内容摘要（截取前 200 字）...
  项目: /path/to/project
  相关度: 0.85
```

## 实施阶段

### 阶段一：基础设施
- 安装 qmd[mvp] 依赖
- 配置 qmd 数据目录（`~/.cc-feishu-bridge/`）
- bridge 启动时启动 qmd MCP subprocess
- 验证 qmd MCP 通信正常

### 阶段二：用户偏好多用户改造
- SQLite 表新增 `user_open_id` 字段（加 migration）
- MCP 工具新增 `user_open_id` 参数
- `inject_context()` 按 `user_open_id` 过滤
- 所有调用处传入 `user_open_id`

### 阶段三：项目记忆迁移到 qmd
- `MemoryAddProj` → 写 qmd collection
- `MemoryDeleteProj` → 删 qmd doc
- `MemoryUpdateProj` → 更新 qmd doc
- `MemoryListProj` → 读 qmd collection list
- `MemorySearchProj` → 调用 qmd query，格式化结果

### 阶段四：清理与测试
- 删除 project_memories SQLite 表（或保留备用）
- 整体功能测试
- inject_context 和 MCP 工具联动测试

## 风险与注意事项

1. **qmd[mvp] 依赖较重**：包含 torch + sentence-transformers，首次安装耗时较长，CI/CD 构建需要注意
2. **qmd MCP subprocess 管理**：需要处理进程启动失败、超时、崩溃重建等异常
3. **用户 open_id 传递**：所有调用路径都要能拿到 `user_open_id`，需要确认飞书消息中能取到
4. **向后兼容**：历史用户（无 `user_open_id` 字段）需要 migration 脚本

## 文件改动范围

- `cc_feishu_bridge/claude/memory_manager.py` — 新增 qmd 适配层，用户偏好加 user_open_id
- `cc_feishu_bridge/claude/memory_tools.py` — MCP 工具适配 qmd，参数新增 user_open_id
- `cc_feishu_bridge/main.py` — 启动 qmd MCP subprocess
- `pyproject.toml` — 新增 qmd[mvp] 依赖
- 新增 `cc_feishu_bridge/claude/qmd_adapter.py` — qmd CLI 调用封装层
