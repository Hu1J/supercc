# Plugin MCP Server 架构设计

> 目标：让 core 的 ClaudeIntegration 能调用平台特有 MCP 工具（FeishuFile、GetChatMembers 等）
> 状态：设计阶段

## 背景

`claude_agent_sdk` 支持通过 `mcp_servers` dict 配置连接 MCP server：

```python
mcp_servers = {
    "SuperCC": supercc_server,           # 内置 core MCP
    "codex": codex_mcp_config,          # Codex MCP
    "feishu": feishu_mcp_config,        # 飞书平台 MCP（待新增）
}
client = ClaudeSDKClient(options=..., mcp_servers=mcp_servers)
```

MCP server 配置格式：
```python
{
    "type": "stdio",       # 或 "http"
    "command": "python",
    "args": ["-m", "supercc.adapter.feishu.mcp_server", "--data-dir", "/path/to/data"],
}
```

## 核心设计

Plugin 作为**独立 OS 子进程**运行（通过 systemd/launchd 启动），通过环境变量 `SUPERCC_DATA` 获取配置路径。

Plugin 启动时同时**启动一个本地 MCP server 子进程**，该子进程：
1. 读取 plugin 的凭证（从 `SUPERCC_DATA` 下的 config.json）
2. 创建 FeishuClient 并注册 MCP 工具
3. 通过 stdio 与 core 的 `claude_agent_sdk` 通信

```
┌─────────────────────────────────────────────────┐
│ core 进程 (supercc-main)                         │
│                                                 │
│  ClaudeIntegration._init_options()              │
│    mcp_servers = {                             │
│      "SuperCC": <core MCP>,                    │
│      "feishu": {                              │
│        "type": "stdio",                        │
│        "command": "python",                   │
│        "args": ["-m", "supercc.adapter.feishu.mcp_server", "--data-dir", "/path"] │
│      }                                         │
│    }                                           │
│                    ↕ stdio                     │
└─────────────────────────────────────────────────┘
         ▲
         │ subprocess spawn
         │
┌─────────────────────────────────────────────────┐
│ plugin 子进程 (supercc-feishu)                   │
│                                                 │
│  MCP server: FeishuClient + FeishuFile tools   │
│  - feishu_send_file                            │
│  - get_chat_members                            │
│  - feishu_chat_history                         │
└─────────────────────────────────────────────────┘
```

## 关键设计决策

### 1. Plugin MCP Server 作为 plugin 进程的子进程

Plugin 进程（`FeishuCoreWSClient`）持有 FeishuClient，直接创建 MCP server 并 spawn 为子进程：

```python
# supercc/adapter/feishu/mcp_server.py（新增）
async def main():
    """Plugin MCP server: 暴露飞书特有工具给 core 调用。"""
    from supercc.adapter.feishu.client import FeishuClient
    from supercc.adapter.feishu.mcp_tools import feishu_file_tool, get_chat_members_tool, feishu_chat_history_tool

    config_path = sys.argv[sys.argv.index("--data-dir") + 1]
    # 读取 config.json 中的 feishu credentials
    config = json.load(open(config_path))

    feishu = FeishuClient(
        app_id=config["channels"]["feishu"]["app_id"],
        app_secret=config["channels"]["feishu"]["app_secret"],
        ...
    )

    server = create_sdk_mcp_server(
        name="SuperCC-Feishu",
        version="1.0.0",
        tools=[feishu_file_tool, get_chat_members_tool, feishu_chat_history_tool],
    )
    await server.run(stdio=True)
```

### 2. MCP server 命令行参数

Plugin MCP server 通过 `--data-dir` 参数接收数据目录路径（包含 config.json），不直接接收凭证：

```python
# FeishuCoreWSClient.__init__() 中
self._mcp_server_process = subprocess.Popen(
    [sys.executable, "-m", "supercc.adapter.feishu.mcp_server",
     "--data-dir", self._data_dir],
    cwd=project_root,
)
```

### 3. mcp_servers 配置的初始化

Plugin 启动后通知 core 自己的 MCP server 命令：

```python
# FeishuCoreWSClient.connect() 中
async def connect(self):
    await self._ws.connect(...)
    # 通知 core 自己的 MCP server 已启动
    await self._send_event("mcp_server_ready", {
        "platform": "feishu",
        "mcp_config": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "supercc.adapter.feishu.mcp_server", "--data-dir", self._data_dir],
        }
    })
```

Core 存储在 `SessionManager` 或 `WorkerPool` 中。

### 4. ClaudeIntegration._init_options() 接入 plugin MCP

```python
# integration.py _init_options() 中
plugin_mcp_config = self._get_plugin_mcp_config(channel)
if plugin_mcp_config:
    mcp_servers["feishu"] = plugin_mcp_config
```

## Plugin 应暴露的 MCP 工具

| 工具 | 入参 | 说明 |
|------|------|------|
| `FeishuFile` | `file_path`, `chat_id` | 上传本地文件到飞书并发送 |
| `FeishuImage` | `image_path`, `chat_id` | 上传图片到飞书并发送 |
| `GetChatMembers` | `chat_id`（可选） | 获取群成员列表含 mention 标签 |
| `GetChatHistory` | `chat_id`, `start_time`, `end_time`, `keyword` | 获取群历史消息 |

## 实现步骤

### Phase 1: 迁移现有工具到 plugin MCP server

1. **创建 `supercc/adapter/feishu/mcp_tools.py`**
   - 从 `supercc/claude/feishu_file_tools.py` 迁移工具定义
   - 使用 `create_sdk_mcp_server` 创建 MCP server
   - Tool 函数通过参数接收 `FeishuClient` 实例（使用 contextvar 或全局变量）

2. **修改 `supercc/plugin/feishu/__main__.py`**
   - `FeishuCoreWSClient` 启动时 spawn MCP server 子进程
   - 子进程运行 `mcp_tools.main()`

3. **修改 `integration.py`**
   - `_init_options()` 接收 `plugin_mcp_configs: dict`
   - 启动时 core 从 plugin 接收 MCP server 配置

### Phase 2: 整合到 WorkerPool

1. **修改 `WorkerPool.acquire()`**
   - `acquire(key)` 时从 plugin 接收并存储 MCP server 配置
   - 传递给 `ClaudeIntegration._init_options()`

2. **Plugin 注册 MCP server**
   - `FeishuCoreWSClient.connect()` 发送 `mcp_server_ready` 消息
   - core 存储在 `WorkerPool._plugin_mcp_configs[key]` 中

### Phase 3: 清理旧代码

1. **删除 `supercc/claude/feishu_file_tools.py`**（迁移完成后）
2. **删除 `supercc/claude/feishu_history_tools.py`**（迁移完成后）
3. **从 `supercc_tools.py` 删除飞书工具导入**（已完成）

## 凭证传递方式

Plugin MCP server 子进程与 plugin 主进程共享同一个 `SUPERCC_DATA/config.json`：

```
plugin 主进程                     MCP server 子进程
  │                                   │
  │ 读取 config.json ─────────────────┘
  │ 拥有 FeishuClient                 │
  │                                   │ (子进程) 读取同一 config.json
  │                                   │ 创建新的 FeishuClient 实例
  │                                   │ 注册为 MCP tools
  │                                   │
  │                          stdio ←→  claude_agent_sdk (core)
```

**关键**：MCP server 子进程是 plugin 主进程的子进程，可以共享文件系统的 config.json。不需要额外的 IPC 机制。

## 错误处理

- **MCP server 启动失败**：Plugin 主进程记录错误，核心功能（对话）继续，但 FeishuFile 等工具不可用
- **MCP server 崩溃**：Plugin 主进程收到子进程退出信号，重启子进程
- **凭证过期**：MCP server 子进程需要支持热更新凭证（通过定期 re-read config.json）
