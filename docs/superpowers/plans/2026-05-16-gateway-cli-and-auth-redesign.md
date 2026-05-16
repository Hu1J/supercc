# Gateway CLI 重构 + Plugin→Core WS 认证 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构 SuperCC CLI 为 gateway 子命令体系，移除 filelock 改用 PID 文件，实现 Plugin→Core WS 认证。

**Architecture:**
- CLI 统一走 `supercc gateway run/start/stop/status/install/uninstall/restart`
- 独立版本更新：`supercc update`
- 模型配置：`supercc config` + `supercc config core`
- 进程自检测 daemon 模式：启动后检查系统服务是否存在，写入 `config.daemon`
- restart 时根据 `daemon` 值决定 os.execvp（前台）还是 SIGUSR1（服务托管）
- Plugin→Core WS 认证：两种平级静态凭证模式（Token / 账号密码）

**Tech Stack:** Python asyncio, launchd/systemd, filelock → PID file, JSON config

---

## 文件结构

```
supercc/
├── main.py                          # gateway handler、switch 删除、filelock 移除
├── config.py                        # CoreConfig 加 daemon/token/username/password
├── core/server.py                   # 删除 switch event、添加 auth 处理
├── core/commands/
│   ├── __init__.py                  # 移除 SwitchHandler
│   ├── switch.py                    # 删除
│   └── restart_impl.py              # 移除 filelock 引用
├── gateway/
│   └── cli.py                       # 新增 run_gateway_run()、run_gateway_restart()
├── onboard.py                        # 新增 auth 模式选择
└── adapter/feishu/core_client.py    # 发送 auth 消息
    adapter/wecom/core_client.py     # 发送 auth 消息
```

---

## 阶段一：Gateway CLI 重构

### Task 1: `gateway/cli.py` 新增 `run_gateway_run()`

**Files:**
- Modify: `supercc/gateway/cli.py`

- [ ] **Step 1: 添加 `run_gateway_run()` 函数**

```python
def run_gateway_run() -> None:
    """gateway run 子命令：前台阻塞运行，不获取 filelock。
    适合开发调试，Ctrl+C 退出。
    """
    from supercc.config import resolve_config_path, init_config
    cfg_path, data_dir = resolve_config_path()
    init_config(cfg_path)
    import asyncio
    from supercc.main import start_bridge
    asyncio.run(start_bridge(cfg_path, data_dir, foreground=True))
```

- [ ] **Step 2: 添加 `run_gateway_restart()` 函数**

```python
def run_gateway_restart() -> None:
    """gateway restart 子命令：热重启当前实例。"""
    from supercc.core.commands.restart_impl import run_restart_cli
    import sys
    try:
        for step in run_restart_cli(None):
            print(f"[{step.step}/{step.total}] {step.label}: {step.detail or ''}")
            if step.status == "final":
                break
    except Exception as e:
        print(f"Restart failed: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 3: Commit**

```bash
git add supercc/gateway/cli.py
git commit -m "feat(gateway): 新增 run_gateway_run 和 run_gateway_restart"
```

---

### Task 2: `main.py` gateway handler 支持 `run` 和 `restart`

**Files:**
- Modify: `supercc/main.py:1746-1767`

- [ ] **Step 1: 更新 gateway handler，添加 run 和 restart 分支**

在 `if command == "gateway":` 块中：

```python
if command == "gateway":
    from supercc.gateway.cli import (
        run_gateway_install,
        run_gateway_start,
        run_gateway_stop,
        run_gateway_status,
        run_gateway_uninstall,
        run_gateway_run,
        run_gateway_restart,
    )
    action = getattr(args, "gateway_action", None)
    if action == "install":
        run_gateway_install()
    elif action == "start":
        run_gateway_start()
    elif action == "stop":
        run_gateway_stop()
    elif action == "status":
        run_gateway_status()
    elif action == "uninstall":
        run_gateway_uninstall()
    elif action == "run":
        run_gateway_run()
    elif action == "restart":
        run_gateway_restart()
    else:
        run_gateway_status()
    return
```

- [ ] **Step 2: 在 gateway subparsers 中注册 run 和 restart**

找到 `gateway_subparsers` 定义处（约 line 1589），在 `ca_uninstall = ...` 后添加：

```python
ca_restart = gateway_subparsers.add_parser("restart", help="热重启当前实例")
ca_run = gateway_subparsers.add_parser("run", help="前台阻塞运行（开发调试用）")
```

（注意：已有 run 和 restart parser 定义，只需确认 action handler 正确路由即可）

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "feat(gateway): gateway handler 支持 run 和 restart action"
```

---

### Task 3: `main.py` 删除 `switch_parser`

**Files:**
- Modify: `supercc/main.py:1499-1509`

- [ ] **Step 1: 删除 switch_parser 定义**

删除：
```python
switch_parser = subparsers.add_parser(
    "switch",
    help="Switch to another project's SuperCC instance",
)
switch_parser.add_argument(
    "target",
    help="Target project directory (absolute or relative path)",
)
```

- [ ] **Step 2: 删除 switch command handler**

删除（约 line 1531-1534）：
```python
if command == "switch":
    from supercc.main import run_switch_cli
    ok = run_switch_cli(args)
    return
```

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "refactor: 删除 switch 命令（切换项目改用 cd target && supercc gateway run）"
```

---

### Task 4: `core/server.py` 删除 switch event 分支

**Files:**
- Modify: `supercc/core/server.py`

- [ ] **Step 1: 找到 switch event 分支并删除**

约在 `_handle_client_message` 方法中，删除：
```python
if event == "switch":
    argv = ["supercc", "switch", target_path]
    subprocess.Popen(argv, cwd=os.getcwd(), start_new_session=True, ...)
    return
```

同时将 `event in ("restart", "update", "switch")` 改为 `event in ("restart", "update")`

- [ ] **Step 2: Commit**

```bash
git add supercc/core/server.py
git commit -m "feat(server): 移除 switch event 处理分支"
```

---

### Task 5: 删除 `switch.py` 和移除 `SwitchHandler` 导出

**Files:**
- Delete: `supercc/core/commands/switch.py`
- Modify: `supercc/core/commands/__init__.py`

- [ ] **Step 1: 删除 switch.py**

```bash
rm supercc/core/commands/switch.py
```

- [ ] **Step 2: 从 __init__.py 移除 SwitchHandler**

找到 `from .switch import SwitchHandler` 并删除，同时删除 `SwitchHandler,` 的导出。

- [ ] **Step 3: Commit**

```bash
git add supercc/core/commands/__init__.py
git rm supercc/core/commands/switch.py
git commit -m "refactor: 删除 SwitchHandler 和 switch.py"
```

---

### Task 6: `config.py` 添加 `daemon` 字段

**Files:**
- Modify: `supercc/config.py`

- [ ] **Step 1: 在 Config dataclass 添加 daemon 字段**

在 `Config` dataclass 中添加：
```python
daemon: bool = False
```

- [ ] **Step 2: 在 load_config 中读取 daemon**

在 `load_config` 返回前添加：
```python
daemon=raw.get("daemon", False),
```

- [ ] **Step 3: 在 _write_config_to_path 中写入 daemon**

在 `raw` dict 中添加：
```python
"daemon": cfg.daemon,
```

- [ ] **Step 4: Commit**

```bash
git add supercc/config.py
git commit -m "feat(config): 添加 daemon 字段"
```

---

## 阶段二：Filelock 移除

### Task 7: `main.py` start_bridge() 移除 filelock

**Files:**
- Modify: `supercc/main.py:357-421`

- [ ] **Step 1: 移除 filelock 导入**

```python
# 删除
import filelock
```

- [ ] **Step 2: 移除 _active_lock 定义**

```python
# 删除
_active_lock: "filelock.FileLock | None" = None
```

- [ ] **Step 3: 移除 start_bridge() 中的 filelock 获取逻辑**

删除约 line 359-366：
```python
lock_file = os.path.join(data_dir, ".instance.lock")
lock = filelock.FileLock(lock_file, timeout=1)
global _active_lock
_active_lock = lock
try:
    lock.acquire()
```

同时删除对应的 `finally: lock.release()` 块。

- [ ] **Step 4: 在 start_bridge() 结尾移除 _active_lock = None**

删除 `global _active_lock; _active_lock = None`

- [ ] **Step 5: 添加 daemon 检测逻辑**

在 `start_bridge()` 开头（init_config 之后）添加：
```python
# 检测是否被系统服务托管，写入 daemon 字段
from supercc.gateway.platform import _is_service_installed
is_daemon = _is_service_installed(data_dir)
cfg = get_config()
if cfg.daemon != is_daemon:
    cfg.daemon = is_daemon
    write_config(cfg)
```

- [ ] **Step 6: Commit**

```bash
git add supercc/main.py
git commit -m "refactor: 移除 filelock，改用 PID 文件，添加 daemon 自检测"
```

---

### Task 8: `core/commands/restart_impl.py` 移除 filelock 引用

**Files:**
- Modify: `supercc/core/commands/restart_impl.py`

- [ ] **Step 1: 移除 filelock 相关注释和逻辑**

约 line 134-137 删除：
```python
# Args:
#     file_lock: FileLock object acquired by main.py; released before
#                starting new process so the new instance can acquire it.
```

约 line 164-165 简化检查：
```python
# 原来
if not (os.path.exists(pid_file) and os.path.exists(instance_lock)):
# 改为
if not os.path.exists(pid_file):
```

约 line 212-215, 523-526 删除 filelock 相关注释。

- [ ] **Step 2: 简化 _cleanup_and_replace 中的 filelock release**

约 line 632-642：
```python
# 删除 _active_lock.release() 相关代码
# 直接 unlink pid 文件即可
pid_file = os.path.join(data_dir, "supercc.pid")
Path(pid_file).unlink(missing_ok=True)
# 删除 instance_lock 相关代码
```

- [ ] **Step 3: 修改函数签名，移除 file_lock 参数**

`run_restart_cli(file_lock=None)` 和 `run_update_cli(file_lock=None)` 保持兼容但不再使用。

- [ ] **Step 4: Commit**

```bash
git add supercc/core/commands/restart_impl.py
git commit -m "refactor(restart_impl): 移除 filelock 引用，简化 PID 文件处理"
```

---

## 阶段三：Plugin→Core WS 认证

### Task 9: `config.py` 添加 core 认证字段

**Files:**
- Modify: `supercc/config.py`

- [ ] **Step 1: 在 CoreConfig 添加 token/username/password 字段**

```python
@dataclass
class CoreConfig:
    host: str = "127.0.0.1"
    port: int = 28888
    token: str = ""           # Plugin 连接凭证
    username: str = ""        # 账号密码模式
    password: str = ""        # 账号密码模式
```

- [ ] **Step 2: 在 load_config 中读取**

```python
core_cfg = CoreConfig(
    host=raw.get("core", {}).get("host", "127.0.0.1"),
    port=raw.get("core", {}).get("port", 28888),
    token=raw.get("core", {}).get("token", ""),
    username=raw.get("core", {}).get("username", ""),
    password=raw.get("core", {}).get("password", ""),
)
```

- [ ] **Step 3: 在 _write_config_to_path 中写入**

```python
"core": {
    "host": cfg.core.host,
    "port": cfg.core.port,
    "token": cfg.core.token,
    "username": cfg.core.username,
    "password": cfg.core.password,
},
```

- [ ] **Step 4: Commit**

```bash
git add supercc/config.py
git commit -m "feat(config): 添加 core.token/username/password 认证字段"
```

---

### Task 10: `core/server.py` 添加 auth 消息处理

**Files:**
- Modify: `supercc/core/server.py`

- [ ] **Step 1: 在 WsServer.__init__ 或 reset 中初始化已认证标志**

```python
self._plugin_authenticated: dict[str, bool] = {}  # platform -> authenticated
```

- [ ] **Step 2: 在 _handle_client_message 开头添加 auth 处理**

```python
async def _handle_client_message(self, conn, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return

    # Auth 消息处理
    msg_type = data.get("type") or data.get("method")
    if msg_type == "auth":
        token = data.get("token", "")
        username = data.get("username", "")
        password = data.get("password", "")

        cfg = get_config()
        auth_ok = False
        if token and token == cfg.core.token:
            auth_ok = True
        elif username and password:
            if username == cfg.core.username and password == cfg.core.password:
                auth_ok = True

        if auth_ok:
            platform = data.get("platform", "unknown")
            self._plugin_authenticated[platform] = True
            await conn.ws.send(json.dumps({"type": "auth_ok"}))
        else:
            await conn.ws.send(json.dumps({"type": "auth_failed"}))
            await conn.ws.close()
        return

    # 非 auth 消息检查是否已认证
    platform = data.get("platform", "unknown")
    if platform not in self._plugin_authenticated:
        await conn.ws.send(json.dumps({"type": "error", "message": "not authenticated"}))
        return

    # 原有消息处理...
```

- [ ] **Step 3: Commit**

```bash
git add supercc/core/server.py
git commit -m "feat(core): 添加 WS auth 消息处理（Token/账号密码两种模式）"
```

---

### Task 11: Plugin core_client 发送 auth 消息

**Files:**
- Modify: `supercc/adapter/feishu/core_client.py`
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 在 FeishuCoreWSClient 连接成功后发送 auth**

在 `start_bridge()` 或 `_on_ws_connected` 中，发送 auth 消息：

```python
async def _send_auth(self):
    cfg = get_config()
    if cfg.core.token:
        await self.ws.send(json.dumps({
            "type": "auth",
            "token": cfg.core.token,
            "platform": "feishu"
        }))
    elif cfg.core.username and cfg.core.password:
        await self.ws.send(json.dumps({
            "type": "auth",
            "username": cfg.core.username,
            "password": cfg.core.password,
            "platform": "feishu"
        }))
```

- [ ] **Step 2: 同样处理 WeComCoreWSClient**

- [ ] **Step 3: Commit**

```bash
git add supercc/adapter/feishu/core_client.py supercc/adapter/wecom/core_client.py
git commit -m "feat(plugin): core_client 连接后发送 auth 消息"
```

---

## 阶段四：onboard 认证选择 + config core

### Task 12: `onboard.py` 新增 auth 模式选择

**Files:**
- Modify: `supercc/onboard.py`

- [ ] **Step 1: 在 onboard 流程中添加 auth 模式选择**

在配置存储之前，添加：

```python
print("\n=== Plugin→Core 认证方式 ===")
print("选择 plugin 连接 core WS 时的认证方式（可多选）：")
print("1. Token 认证（自动生成，推荐）")
print("2. 账号密码认证")

auth_choice = input("请选择（1/2/1,2）：").strip()

token = ""
username = ""
password = ""

if "1" in auth_choice:
    token = secrets.token_urlsafe(32)
    print(f"已生成 Token: {token}")

if "2" in auth_choice:
    username = input("请输入用户名：").strip()
    password = input("请输入密码：").strip()

# 将 auth 配置写入 config
# （在保存 config 之前设置）
if token:
    cfg.core.token = token
if username:
    cfg.core.username = username
if password:
    cfg.core.password = password
```

- [ ] **Step 2: 添加 secrets 导入**

```python
import secrets
```

- [ ] **Step 3: Commit**

```bash
git add supercc/onboard.py
git commit -m "feat(onboard): 添加 auth 模式选择步骤（Token/账号密码）"
```

---

### Task 13: `main.py` 添加 `config core` 子命令

**Files:**
- Modify: `supercc/main.py`

- [ ] **Step 1: 在 config subparsers 中注册 core 子命令**

约在 `ca_providers` 后添加：

```python
ca_core = config_subparsers.add_parser("core", help="管理 core 认证配置")
ca_core_subparsers = ca_core.add_subparsers(dest="core_action", help="Action")

ca_core_token = ca_core_subparsers.add_parser("token", help="设置 token（不提供则自动生成）")
ca_core_token.add_argument("token", nargs="?", default="", help="Token 值")

ca_core_username = ca_core_subparsers.add_parser("username", help="设置账号")
ca_core_username.add_argument("username", help="用户名")

ca_core_password = ca_core_subparsers.add_parser("password", help="设置密码")
ca_core_password.add_argument("password", help="密码")

ca_core_show = ca_core_subparsers.add_parser("show", help="显示当前配置")
ca_core_delete = ca_core_subparsers.add_parser("delete", help="删除认证配置")
```

- [ ] **Step 2: 在 _run_config_command 中添加 core action 处理**

```python
if action == "core":
    core_action = getattr(args, "core_action", None)
    cfg_path, _ = resolve_config_path()
    init_config(cfg_path)
    cfg = get_config()
    import secrets

    if core_action == "token":
        tok = getattr(args, "token", None) or ""
        if not tok:
            tok = secrets.token_urlsafe(32)
        cfg.core.token = tok
        write_config(cfg)
        print(f"Token 已设置: {tok}")
    elif core_action == "username":
        cfg.core.username = getattr(args, "username", "")
        write_config(cfg)
        print(f"Username 已设置: {cfg.core.username}")
    elif core_action == "password":
        cfg.core.password = getattr(args, "password", "")
        write_config(cfg)
        print("Password 已设置")
    elif core_action == "show":
        print(f"Token: {'已设置' if cfg.core.token else '未设置'}")
        print(f"Username: {cfg.core.username or '未设置'}")
        print(f"Password: {'已设置' if cfg.core.password else '未设置'}")
    elif core_action == "delete":
        cfg.core.token = ""
        cfg.core.username = ""
        cfg.core.password = ""
        write_config(cfg)
        print("Core 认证配置已清除")
    else:
        print("用法: supercc config core token/username/password/show/delete")
    return
```

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "feat(config): 添加 supercc config core 子命令"
```

---

## 验证

- [ ] `python3 -c "from supercc.main import start_bridge; from supercc.gateway.cli import run_gateway_run; print('import OK')"`
- [ ] `python3 -m supercc gateway` → 列出所有子命令（含 run/restart）
- [ ] `python3 -m supercc gateway run` → 前台模式启动
- [ ] `python3 -m supercc gateway restart` → 热重启
- [ ] `python3 -m supercc switch /path` → 报错 "unrecognized arguments"
- [ ] `python3 -m supercc config core token` → 自动生成并显示 token
- [ ] `python3 -m supercc config core show` → 显示当前配置
- [ ] `python3 -m supercc onboard` → 能看到 auth 模式选择步骤
