# Gateway CLI 重构 + Plugin→Core WS 认证

## 目标

1. **Gateway 统一入口**：废弃 `supercc` 和 `supercc start`，所有运行模式通过 `supercc gateway` 访问
2. **移除 filelock**：改用纯 PID 文件做实例检测，简化 restart 流程
3. **Plugin→Core WS 双向认证**：Token 和账号密码两种静态凭证模式

## 一、Gateway CLI 架构

### 1.1 命令体系

| 命令 | 行为 | 适用场景 |
|------|------|----------|
| `supercc gateway run` | 前台阻塞（直接 `asyncio.run(start_bridge())`） | 开发调试、容器内 |
| `supercc gateway start` | 后台常驻服务（launchd/systemd） | 生产环境、开机自启 |
| `supercc gateway stop` | 停止服务（等进程退出后清理锁文件） | — |
| `supercc gateway status` | 查看运行状态 | — |
| `supercc gateway install` | 安装平台服务（--force 刷新凭证） | 首次部署 |
| `supercc gateway uninstall` | 卸载服务并停止 | 清理部署 |
| `supercc gateway restart` | 热重启当前实例（os.execvp 或 SIGUSR1） | 配置变更后生效 |
| `supercc update` | 版本更新（升级 SuperCC 本身） | 版本升级 |
| `supercc config` | 管理模型配置（add/switch/delete/list，回车进交互） | — |
| `supercc config core` | 管理 core 认证配置（token/username/password） | — |

### 1.2 废弃命令

- `supercc`（裸命令）→ 改用 `supercc gateway run`
- `supercc start` → 改用 `supercc gateway start`
- `supercc switch` → 移除，切换项目请用 `cd target && supercc gateway run/start`
- `supercc restart` → 改用 `supercc gateway restart`

`supercc update` 保留，作为独立版本更新命令。

### 1.3 两种运行模式的本质区别

两种模式下内部都是 `asyncio.run(start_bridge())`，区别仅在于启动方式：

| | `gateway run` | `gateway start` |
|--|--|--|
| 启动方式 | 直接前台运行，无系统服务 | 注册 launchd/systemd 服务，由系统托管 |
| Ctrl+C | 退出 | 无效（后台进程） |
| 重启后保持 | 否 | 是（系统服务） |

### 1.4 daemon 字段机制

启动后自动检测是否被系统服务托管：

1. 检查系统服务是否存在（`launchctl list com.supercc.main.xxx` / `systemctl show`）
2. 将检测结果写入 `config.json` 的 `daemon: true/false`
3. `gateway restart/update` 时根据 `daemon` 值决定路径：
   - `daemon: false` → os.execvp 自己替换
   - `daemon: true` → 发 SIGUSR1 给当前实例，通知系统服务重启

### 1.5 服务名称

保持 hash 方案：项目路径 MD5 前8位（如 `com.supercc.main.a1b2c3d4`）。

- 用户无需关心服务名
- hash 隔离保证同机多项目不冲突
- 项目路径变化会导致新服务（旧服务需手动清理）

## 二、Filelock 移除方案

### 2.1 为什么移除 filelock

- `os.execvp` 后 PID 不变，PID 文件内容仍然有效，不需要更新
- restart 流程简化为：`unlink PID → os.execvp`（两步）
- filelock 的 release + unlink 步骤在 restart 时变成多余

### 2.2 PID 文件检测逻辑

```
1. 读 {data_dir}/supercc.pid
2. PID 存在 → os.kill(pid, 0) 确认进程存活
   → 进程存活 → 报错"已有实例在运行"
   → 进程已死 → 删除 stale PID 文件 → 继续
3. 写新 PID 到文件
4. 正常启动
```

### 2.3 restart 时的 PID 文件处理

```
unlink pid 文件 → os.execvp → PID 不变，文件已清 → 新进程正常
```

## 三、Plugin→Core WS 认证

### 3.1 两种认证模式（平级，都存在 config.json）

**Token 模式**：
```json
{
  "core": {
    "token": "plugin-connect-token-xxx"
  }
}
```

**账号密码模式**：
```json
{
  "core": {
    "username": "admin",
    "password": "预共享密码"
  }
}
```

### 3.2 Core WS 服务器认证流程

连接建立后，plugin 必须先发送 auth 消息：

```json
// Token 模式
{
  "type": "auth",
  "token": "plugin-connect-token-xxx"
}

// 账号密码模式
{
  "type": "auth",
  "username": "admin",
  "password": "预共享密码"
}
```

Core 验证逻辑（静态字符串比较）：
- Token 模式：`auth.token == config.core.token`
- 账号密码模式：`auth.username == config.core.username AND auth.password == config.core.password`

验证失败 → 关闭连接。
验证成功 → 发送 `{"type": "auth_ok"}` → 正式开始通信。

### 3.3 Token 生成

首次启动时自动生成，存储在 config.json 中。`gateway install --force` 重新生成。

### 3.4 Plugin WS 客户端认证

连接 core WS 时，plugin 从 config.json 读取凭证并发送 auth 消息。

### 3.5 onboard 流程中的认证模式选择

onboard 流程新增 auth 模式选择步骤：
- 让用户选择使用 **Token 认证**、**账号密码认证**，或**两者同时启用**
- 可多选（两者都启用时 plugin 任选一种认证方式）
- 用户不提供 token 则自动生成
- 后续可通过 `supercc config core` 修改

### 3.6 `supercc config core` 子命令

```bash
supercc config core token [TOKEN]     # 设置 token（不提供则自动生成）
supercc config core username <username>  # 设置账号
supercc config core password <password>  # 设置密码
supercc config core show             # 显示当前配置（不暴露密码）
supercc config core delete           # 删除 core 认证配置
```

## 四、实现计划

### 阶段一：Gateway CLI 重构

1. `config.py`：添加 `daemon` 字段
2. `start_bridge()`：启动后自动检测系统服务是否存在，更新 `config.daemon`
3. `gateway/cli.py`：新增 `run_gateway_run()`
4. `main.py`：删除 `switch_parser`，gateway handler 加 `run` 分支
5. `core/server.py`：删除 switch event 分支
6. `core/commands/switch.py`：删除
7. `core/commands/__init__.py`：移除 SwitchHandler 导出

### 阶段二：Filelock 移除

1. `core/commands/restart_impl.py`：移除 filelock 相关代码
2. `main.py start_bridge()`：移除 filelock，只用 PID 文件
3. `gateway/manager.py`：确认只依赖 PID 文件

### 阶段三：Plugin→Core WS 认证

1. `config.py`：添加 `core.token`、`core.username`、`core.password` 字段
2. `core/server.py`：添加 auth 消息处理
3. `adapter/feishu/core_client.py`：发送 auth 消息
4. `adapter/wecom/core_client.py`：发送 auth 消息

### 阶段四：onboard 认证模式选择 + config core

1. `onboard.py`：新增 auth 模式选择步骤（token / username+password，可多选）
2. `main.py`：新增 `config core` 子命令（token/username/password 的增删改查）
3. `config.py`：添加 `core.token`、`core.username`、`core.password` 到 `CoreConfig` dataclass

## 五、验证方法

1. `python3 -c "from supercc.main import start_bridge; from supercc.gateway.cli import run_gateway_run; print('import OK')"`
2. `python3 -m supercc gateway run` → 前台模式，无 PID 文件
3. `python3 -m supercc gateway start` → `python3 -m supercc gateway status` → 后台模式
4. `python3 -m supercc gateway restart` → os.execvp 热重启（daemon=false）或 SIGUSR1（daemon=true）
5. `python3 -m supercc restart` → 报错 "unrecognized arguments"
6. `python3 -m supercc update` → 版本更新（保留命令）
6. `python3 -m supercc onboard` → 能选择 auth 模式（token / username+password）
7. `python3 -m supercc config core token` → 自动生成 token 并显示
8. `python3 -m supercc config core show` → 显示当前 core 认证配置
