# SuperCC 🐲

```
███████╗██╗  ██╗███████╗███████╗██████╗ ███████╗███████╗
██╔════╝██║  ██║██║  ██║██╔════╝██╔══██╗██╔════╝██╔════╝
███████╗██║  ██║███████║█████╗  ██████╔╝██║     ██║
╚════██║██║  ██║██╔════╝██╔══╝  ██╔══██╗██║     ██║
███████║███████║██║     ███████╗██║  ██║███████╗███████╗
╚══════╝╚══════╝╚═╝     ╚══════╝╚═╝  ╚═╝╚══════╝╚══════╝
```

![SuperCC Banner](supercc_poster_final.png)

**超级 Claude Code** — 让 Claude Code 在飞书、企业微信等 IM 平台中无缝运行。

> 自进化超级 AI · 越用越懂你

---

## 核心亮点

### 多平台接入
支持飞书、企业微信（WS 协议）。框架设计支持扩展更多平台。一个 Core，多个 Plugin 同时在线。

### 记忆自优化
每日凌晨自动精炼记忆库 — 合并冗余、精简重复、删除过时信息。记忆越用越精准。

### 技能自进化
每次对话后自动检查 skills 目录变化，有更新自动 git commit。技能可像代码一样版本管理。

### 记忆注入
每次对话自动检索相关记忆，注入上下文。无需重复描述背景，AI 始终知道你是谁、你在做什么。

### Cron 定时任务
标准 cron 表达式，精准定时触发 AI 执行任务，结果自动推送到 IM 平台。

### 多模型支持
内置 llmproxy（kimi/deepseek）、火山引擎 ARK、阿里云通义千问、智谱 GLM、MiniMax 等国内主流供应商，API Key 即插即用。

---

## 快速开始

### 安装

```bash
pip install -U pysupercc
```

### 启动

```bash
# 首次运行，自动进入安装引导（配置平台 → 配置模型 → 启动服务）
supercc onboard

# 直接前台运行（开发调试用）
supercc gateway run

# 后台运行 + 开机自启动（生产环境用）
supercc gateway start

# 查看运行状态
supercc gateway status

# 停止服务
supercc gateway stop

# 重启服务
supercc gateway restart
```

### 升级

```bash
# CLI 升级
supercc update

# 或在 IM 里发 /update 指令
```

### 配置

```bash
# 交互式配置菜单
supercc config
```

---

## 指令集

### 基础对话

| 指令 | 说明 |
|------|------|
| `/new` | 创建新会话 |
| `/status` | 查看当前运行状态 |
| `/stop` | 打断 Claude 当前正在执行的查询 |
| `/restart` | 热重启 SuperCC（不中断服务） |
| `/update` | 检查更新并自动升级 |
| `/help` | 查看所有可用指令 |

### 记忆系统

| 指令 | 说明 |
|------|------|
| `/memory` | 管理本地记忆库（查看/搜索/增删） |
| `/remember <内容>` | 主动添加记忆 |
| `/forget` | 清除当前会话上下文（记忆不受影响） |

### 定时任务

| 指令 | 说明 |
|------|------|
| `/cron` | 管理定时任务（创建/查看/删除/暂停） |
| `/cron list` | 查看所有定时任务 |
| `/cron logs <id>` | 查看任务执行日志 |

### 模型配置

| 指令 | 说明 |
|------|------|
| `/model` | 查看当前模型配置状态 |
| `/model switch <provider>` | 切换到已配置的供应商（如 `/model switch llmproxy`） |

### 技能管理

| 指令 | 说明 |
|------|------|
| `/skill <关键词>` | 搜索并安装 Skills |
| `/skills` | 查看已安装的 Skills 列表 |

### Git 操作

| 指令 | 说明 |
|------|------|
| `/git` | 查看当前项目 git 状态 |

### 消息推送控制

| 指令 | 说明 |
|------|------|
| `/verbose` | 查看当前消息推送配置 |
| `/verbose on\|off` | 开启/关闭全部消息推送 |
| `/verbose skill on\|off` | 🧰 Skill 自进化通知 |
| `/verbose mem on\|off` | 🧠 记忆自优化通知 |
| `/verbose step on\|off` | ⚙️ 过程消息（工具调用/中间步骤） |

---

## 工作原理

```
┌─────────────────────────────────────────────────────────┐
│                      飞书 / 企业微信                      │
│                   （用户通过 IM 对话）                      │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              SuperCC Plugin（WS Client）                │
│         FeishuPlugin / WeComPlugin                    │
│    接收 IM 消息 → 转发给 Core；Core 回复 → 推送回 IM   │
└────────────────────────┬────────────────────────────────┘
                         │  WebSocket
                         ▼
┌─────────────────────────────────────────────────────────┐
│              SuperCC Core（WS Server）                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────┐ │
│  │  记忆系统  │  │  技能进化  │  │  对话引擎  │  │ 定时  │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────┘ │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   Claude Code CLI                       │
│              （本地执行，真正的 AI 能力）                  │
└─────────────────────────────────────────────────────────┘
```

**架构说明**：
- **Core**：核心服务，运行 Claude Code，处理对话、记忆、技能、定时任务。通过 WebSocket 与各 Plugin 通信。
- **Plugin**：平台适配层（飞书/企业微信），负责接收平台消息、推送回复，各自独立运行，互不影响。
- **Gateway**：Core 的启动器，支持前台运行（`gateway run`）和后台服务（`gateway start`）。

---

## 配置文件

- `{project}/.supercc/config.json` — 主配置（平台凭证、Core 监听地址、认证方式）
- `{project}/.supercc/model.json` — 多模型 API 配置（per-project 隔离）
- `~/.supercc/memories.db` — 记忆数据库（SQLite + FTS5，跨项目共享）
- `{project}/.supercc/cron_jobs.json` — 定时任务配置（per-project）
- `~/.claude/skills/` — 全局 Skills 目录

---

## 目录结构

```
supercc/
├── main.py              # 入口点（CLI 分派）
├── onboard.py           # 首次安装引导
├── banner.py            # ASCII logo + 版本信息
├── config.py            # 配置读写
├── channels/            # 平台适配层
│   ├── feishu/          # 飞书适配器
│   └── wecom/           # 企业微信适配器（WS 协议）
├── core/                # 核心引擎
│   ├── commands/         # /指令实现
│   ├── claude/          # Claude Code 接口
│   ├── models/          # 多模型管理
│   └── cron_scheduler/  # 定时调度
├── gateway/             # Gateway（服务启动器）
│   ├── platform.py       # 跨平台服务安装（launchd/systemd/Task Scheduler）
│   ├── manager.py        # GatewayManager
│   └── cli.py           # CLI 处理器
└── install/             # 安装流程（onboard 调用的平台安装）
```

---

## 常见问题

**Q: 飞书机器人收不到消息？**
检查 `{project}/.supercc/config.json` 中的 `app_id`/`app_secret` 是否正确，机器人是否已启用，Feishu 应用是否已配置 WS 回调地址。

**Q: 群聊 @CC 时上下文消息注入不生效？**
确认飞书应用已开通 `im:message.group_msg` 权限，否则 `get_chat_history()` 无法拉取群聊历史。

**Q: 如何切换模型？**
发送 `/model` 查看当前配置，`/model switch <provider>` 切换供应商，或用 `supercc config` 进入交互式菜单。

**Q: 记忆不生效？**
发送 `/memory` 查看记忆库，确认相关记忆已录入。新会话会自动注入相关记忆。

**Q: 定时任务没执行？**
确认 SuperCC 正在运行，发送 `/cron list` 查看任务状态。

**Q: gateway start 和 gateway run 有什么区别？**
`gateway run` 是前台阻塞运行（适合开发调试），`gateway start` 是后台守护进程 + 开机自启动（适合生产环境）。

---

## 获取帮助

- 提交 Issue: [GitHub Issues](https://github.com/Hu1J/supercc/issues)
- 查看文档: [GitHub README](https://github.com/Hu1J/supercc)

---

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)。
