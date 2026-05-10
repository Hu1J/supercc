# SuperCC 核心服务 + 插件架构重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 SuperCC 从单体应用重构为"核心服务 + 平台插件"架构，核心只做 AI 推理和业务状态管理，插件只做消息格式转换。

**Architecture:** 核心服务通过 WebSocket 持有 Worker Pool，按 chat_id 永久绑定 Worker；插件通过 WebSocket 连接核心，统一使用 Markdown 作为消息格式；Session 隔离键为 bot_id × project_path × platform × chat_id。

**Tech Stack:** Python asyncio + WebSocket + JSON-RPC 2.0 + SQLite

---

## 阶段划分

由于改动量巨大，分三个阶段：

### Phase 1：核心服务基础设施
构建 core/ 目录，完成 WebSocket Server、Worker Pool、Session 管理的核心骨架。

### Phase 2：飞书插件适配
改造飞书插件为连接核心的客户端，验证协议和集成流程。

### Phase 3：企业微信插件（新建）
接入企业微信插件，验证插件架构的可扩展性。

---

## Phase 1 实现计划

> 保存到：`docs/superpowers/plans/2026-05-10-core-plugin-architecture-plan-phase1.md`

### 核心文件清单

| 文件 | 职责 |
|------|------|
| `core/__init__.py` | 包入口 |
| `core/server.py` | WebSocket Server，路由表，JSON-RPC 协议处理 |
| `core/worker.py` | Worker Pool，按 chat_id 绑定 Worker，管理 ClaudeIntegration |
| `core/session.py` | Session 管理，sessions.db 操作 |
| `core/protocol.py` | 协议类型定义（使用 dataclass） |
| `core/model.py` | 模型配置（从 model_config.py 迁移） |
| `core/memory.py` | 记忆管理（从 memory_manager.py 迁移） |
| `core/cron.py` | Cron 调度（从 cron_scheduler.py 迁移） |
| `core/banner.py` | 启动 Banner（从 banner.py 迁移） |

### adapter/feishu/ 文件清单

| 文件 | 职责 |
|------|------|
| `adapter/__init__.py` | 包入口 |
| `adapter/feishu/ws_client.py` | 连接核心的 WebSocket 客户端 |

---

## Phase 2 实现计划

> 保存到：`docs/superpowers/plans/2026-05-10-core-plugin-architecture-plan-phase2.md`

### 内容

- 改造 `adapter/feishu/` 使其连接核心而非直接处理消息
- 实现格式渲染模块的对接
- 端到端验证

---

## Phase 3 实现计划

> 保存到：`docs/superpowers/plans/2026-05-10-core-plugin-architecture-plan-phase3.md`

### 内容

- 新建 `adapter/wecom/` 企业微信插件
- 验证插件架构的可扩展性

---

## 开发要求

1. **拉新分支开发**：`git checkout -b core-plugin-refactor`，不在 main 上做
2. **每个阶段完成后测试验证**再进入下一阶段
3. **迁移文件时保留 git 历史**：`git mv` 而非删除后新建

---

## 关键风险

1. **Worker 与 ClaudeIntegration 的进程边界**：当前 ClaudeIntegration 是子进程调用 CLI，重构后核心与 Worker 在同一进程
2. **sessions.db 路径变更**：`~/.supercc/sessions.db` 需要被核心和插件共享
3. **媒体路径**：本地图片路径在插件和核心间共享，需要约定统一的媒体目录

