# 进程隔离 + 系统服务守护 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Feishu 和 WeCom 拆成独立进程，Core 只跑 WsServer + CronScheduler，实现真正的进程隔离，任何 Channel 崩溃不影响其他 Channel 和 Core。

**Architecture:**
```
独立进程（系统服务守护，跨平台）
├── supercc-main  — Core WsServer + CronScheduler
├── supercc-feishu — FeishuWSClient + FeishuCoreWSClient
└── supercc-wecom  — WeComWSClient + WeComCoreWSClient
```

每个进程崩溃后自动重启，工业级生产方案。

**跨平台支持：**
- **Linux**: systemd user service（`Restart=unless-stopped`）
- **macOS**: launchd LaunchAgent（`KeepAlive`）
- **Windows**: NSSM（Non-Sucking Service Manager）或 Windows Service

**复用已有的 gateway-service skill**：`supercc/gateway/platform.py` 中已有完整的跨平台服务安装/卸载逻辑（install_systemd / install_launchd / install_nssm），本次只需扩展支持多进程场景。

---

## Core 端口设计

**端口从 config.json 读取，不写死代码。** 原因：同一台机器可能跑多个 SuperCC 实例（不同项目），每个实例需要独立端口。

```
config.json
{
  "core": {
    "host": "127.0.0.1",
    "port": 28888
  }
}
```

- 端口起始值：**28888**（每个项目一个端口）
- plugin 入口从 `config.json` 读取 `config.core.port`，构造 `ws://127.0.0.1:{port}`
- main.py 启动 Core 时也从 `config.json` 读取端口，不再写死 8765

---

## 文件结构

```
supercc/
├── main.py                          # Core only (WsServer + CronScheduler)
├── plugin/feishu/__main__.py       # 新增: 飞书插件入口
├── plugin/wecom/__main__.py         # 新增: 企业微信插件入口
└── gateway/
    ├── platform.py                 # 已有: 跨平台服务管理（需扩展多进程支持）
    ├── manager.py                   # 已有: GatewayManager
    └── cli.py                       # 已有: gateway CLI

deploy/
├── systemd/                         # Linux
│   ├── supercc-main.service
│   ├── supercc-feishu.service
│   └── supercc-wecom.service
├── launchd/                         # macOS
│   ├── com.supercc.main.plist
│   ├── com.supercc.feishu.plist
│   └── com.supercc.wecom.plist
└── windows/                         # Windows
    └── install.bat                  # NSSM 注册脚本
```

---

## 任务 1: 拆分 main.py — 提取 CoreOnly 入口

**Files:**
- Modify: `supercc/main.py` — 提取 `--core-only` 模式

- [ ] **Step 1: 读 main.py 的 start_bridge() 结构**

确认 WsServer 和 CronScheduler 在 `start_bridge()` 中的位置（当前在 426-446 行和 553-566 行）。

- [ ] **Step 2: 重构 start_bridge()**

将 `start_bridge()` 拆分为两个函数：

```python
def start_core_only(config_path: str, data_dir: str):
    """仅启动 Core (WsServer + CronScheduler)，不启动任何 Channel。"""
    # 1. 获取锁 (复用现有代码)
    lock_file = os.path.join(data_dir, ".instance.lock")
    lock = filelock.FileLock(lock_file, timeout=1)
    lock.acquire()

    # 2. 初始化 config, model env 等 (复用现有代码)
    config = init_config(config_path)
    ...

    # 3. 启动 Core WsServer (daemon thread)
    def run_core_server():
        ...

    import threading
    core_thread = threading.Thread(target=run_core_server, daemon=True)
    core_thread.start()

    # 4. 启动 CronScheduler
    cron_scheduler = CronScheduler(config, data_dir)
    set_cron_scheduler(cron_scheduler, config)
    cron_scheduler.start()

    # 5. 注册 cleanup signal handler
    def cleanup(signum, frame):
        ...
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 6. 阻塞主线程（让 daemon thread 一直运行）
    while True:
        time.sleep(3600)


def start_bridge(config_path: str, data_dir: str):
    """完整启动（向后兼容），调用 start_core_only + 启动插件进程。"""
    # 调用 subprocess 启动各插件进程（见任务 5）
    ...
```

- [ ] **Step 3: 添加 `--core-only` CLI 参数**

在 `main()` 的 subparsers 中添加：

```python
core_only_parser = subparsers.add_parser("core-only", help="Start Core only (internal)")
```

- [ ] **Step 4: 验证**

```bash
python -m supercc main --core-only --help
```

- [ ] **Step 5: 提交**

```bash
git add supercc/main.py
git commit -m "refactor(main): extract start_core_only() for process isolation"
```

---

## 任务 2: 创建飞书插件入口

**Files:**
- Create: `supercc/plugin/feishu/__init__.py`
- Create: `supercc/plugin/feishu/__main__.py`

- [ ] **Step 1: 创建 __init__.py**

```python
"""Feishu plugin package."""
```

- [ ] **Step 2: 创建 __main__.py**

```python
"""Feishu 插件独立进程入口。

Usage:
    python -m supercc.adapter.feishu
    # 或通过 console script:
    supercc-feishu
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys

# 将项目根目录加入 sys.path（确保能 import supercc）
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from supercc.config import init_config
from supercc.adapter.feishu.client import FeishuClient
from supercc.adapter.feishu.ws_client import FeishuWSClient
from supercc.adapter.feishu.core_client import FeishuCoreWSClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feishu-plugin")


async def main():
    config_path = os.environ.get("SUPERCC_CONFIG", "")
    data_dir = os.environ.get("SUPERCC_DATA", "")
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"

    config = init_config(config_path)
    feishu = FeishuClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
        bot_name=config.channels.feishu.bot_name,
        data_dir=data_dir,
    )

    core_client = FeishuCoreWSClient(
        core_url=core_url,
        feishu_client=feishu,
        bot_id=config.channels.feishu.bot_open_id,
        project_path=config.claude.approved_directory,
        groups=config.channels.feishu.groups,
        allowed_users=config.channels.feishu.allowed_users,
    )

    async def on_message(msg):
        await core_client.send_message(msg)

    ws_client = FeishuWSClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
        bot_name=config.channels.feishu.bot_name,
        bot_open_id=config.channels.feishu.bot_open_id,
        domain=config.channels.feishu.domain,
        on_message=on_message,
        config_path=config_path,
    )

    # 连接到 Core
    await core_client.connect()
    logger.info("[Feishu] Connected to core at %s", core_url)

    # 启动 WS 接收飞书消息（阻塞）
    ws_client.start()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: 验证**

```bash
python -m supercc.adapter.feishu --help 2>&1 | head -5
# 预期：ImportError 或正常启动（取决于环境变量）
```

- [ ] **Step 4: 提交**

```bash
git add supercc/plugin/feishu/
git commit -m "feat(feishu): extract feishu plugin as standalone process"
```

---

## 任务 3: 创建企业微信插件入口

**Files:**
- Create: `supercc/plugin/wecom/__init__.py`
- Create: `supercc/plugin/wecom/__main__.py`

- [ ] 同任务 2，镜像实现 WeCom 版本

```bash
git add supercc/plugin/wecom/
git commit -m "feat(wecom): extract wecom plugin as standalone process"
```

---

## 任务 4: 扩展 gateway/platform.py — 支持多进程服务

**Files:**
- Modify: `supercc/gateway/platform.py`

已有跨平台服务管理，需要扩展支持多进程场景（3 个独立服务）。

- [ ] **Step 1: 读现有 platform.py 了解当前结构]

```bash
grep -n "def install_\|def uninstall_\|def start_\|def stop_" supercc/gateway/platform.py
```

- [ ] **Step 2: 添加服务类型常量]

在 `supercc/gateway/platform.py` 中添加：

```python
class ServiceType:
    MAIN = "main"
    FEISHU = "feishu"
    WECOM = "wecom"
```

- [ ] **Step 3: 扩展 install_nssm() 支持服务类型]

NSSM 注册时传入 service_type，确保服务名不冲突：

```python
def install_nssm(data_dir: str, service_type: str):
    """注册 Windows 服务。service_type: 'main' | 'feishu' | 'wecom'"""
    slug = _project_slug(data_dir)
    service_name = f"SuperCC-{service_type}-{slug}"
    exe = sys.executable
    if service_type == "main":
        cmd = [exe, "-m", "supercc", "main", "--core-only"]
    elif service_type == "feishu":
        cmd = [exe, "-m", "supercc.adapter.feishu"]
    elif service_type == "wecom":
        cmd = [exe, "-m", "supercc.plugin.wecom"]
    # 调用 nssm install <service_name> <exe> <args...>
    ...
```

- [ ] **Step 4: 提交]

```bash
git add supercc/gateway/platform.py
git commit -m "feat(gateway): extend platform.py to support multi-process services"
```

---

## 任务 5: 创建跨平台部署配置

**Files:**
- Create: `deploy/systemd/`, `deploy/launchd/`, `deploy/windows/`

### 5a. systemd（Linux）

- [ ] **Step 1: supercc-main.service**

```ini
[Unit]
Description=SuperCC Core (WsServer + CronScheduler)
After=network.target

[Service]
Type=simple
User=%u
WorkingDirectory=%h/.supercc
Environment="SUPERCC_CONFIG=%h/.supercc/config.json"
Environment="SUPERCC_DATA=%h/.supercc"
ExecStart=/usr/bin/python3 -m supercc main --core-only
Restart=unless-stopped
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 2: supercc-feishu.service**

```ini
[Unit]
Description=SuperCC Feishu Plugin
After=network.target supercc-main.service
Requires=supercc-main.service

[Service]
Type=simple
User=%u
Environment="SUPERCC_CONFIG=%h/.supercc/config.json"
Environment="SUPERCC_DATA=%h/.supercc"
Environment="SUPERCC_CORE_URL=ws://127.0.0.1:28888"
ExecStart=/usr/bin/python3 -m supercc.adapter.feishu
Restart=unless-stopped
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 3: supercc-wecom.service**

同上，调整 service name 和 ExecStart 即可。

### 5b. launchd（macOS）

- [ ] **Step 4: com.supercc.main.plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.supercc.main</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>supercc</string>
        <string>main</string>
        <string>--core-only</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${HOME}/.supercc</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SUPERCC_CONFIG</key>
        <string>${HOME}/.supercc/config.json</string>
        <key>SUPERCC_DATA</key>
        <string>${HOME}/.supercc</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/.supercc/launchd-main.out.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.supercc/launchd-main.err.log</string>
</dict>
</plist>
```

- [ ] **Step 5: com.supercc.feishu.plist**

```xml
<!-- 同上，调整 Label, ProgramArguments, 日志路径 -->
<!-- ProgramArguments: /usr/bin/python3 -m supercc.adapter.feishu -->
```

### 5c. Windows（NSSM）

- [ ] **Step 6: deploy/windows/install.bat**

```bat
@echo off
REM 安装 SuperCC 多进程服务（NSSM）
set PYTHON=%PYTHON_PATH%
set SERVICE_DIR=%USERPROFILE%\.supercc

echo Installing SuperCC Main service...
nssm install SuperCC-Main "%PYTHON%" "-m supercc main --core-only"
nssm set SuperCC-Main AppEnvironmentExtra "SUPERCC_CONFIG=%SERVICE_DIR%\config.json" "SUPERCC_DATA=%SERVICE_DIR%"
nssm set SuperCC-Main DisplayName "SuperCC Core"
nssm set SuperCC-Main Start SERVICE_DELAYED_AUTO_START

echo Installing SuperCC Feishu service...
nssm install SuperCC-Feishu "%PYTHON%" "-m supercc.adapter.feishu"
REM 插件进程从 SUPERCC_CONFIG 读取 config.json 中的 core.port，构造 ws://127.0.0.1:{port}
REM 不需要传 SUPERCC_CORE_URL 环境变量
nssm set SuperCC-Feishu DisplayName "SuperCC Feishu Plugin"
nssm set SuperCC-Feishu Start SERVICE_DELAYED_AUTO_START
nssm set SuperCC-Feishu DependOnService SuperCC-Main

REM WeCom 同理...
```

- [ ] **Step 7: 提交**

```bash
git add deploy/
git commit -m "feat(deploy): add cross-platform service configs (systemd/launchd/nssm)"
```

---

## 任务 6: 修改 main.py 启动逻辑 — 子进程模式

**Files:**
- Modify: `supercc/main.py`

- [ ] **Step 1: 添加 spawn-plugin-process 辅助函数**

```python
import subprocess
import sys
import os

def _spawn_plugin_process(module_name: str, config_path: str, data_dir: str, core_url: str):
    """启动一个插件子进程。返回 Popen 对象。"""
    env = {
        **os.environ,
        "SUPERCC_CONFIG": config_path,
        "SUPERCC_DATA": data_dir,
        "SUPERCC_CORE_URL": core_url,
    }
    return subprocess.Popen(
        [sys.executable, "-m", module_name],
        env=env,
        cwd=os.getcwd(),
        start_new_session=True,
    )


def start_bridge(config_path: str, data_dir: str):
    """完整启动：Core + 插件进程（跨平台通用）。"""
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"

    # 1. 先启动 Core（同步，等待就绪）
    import threading, time
    core_ready = threading.Event()

    def run_core():
        start_core_only(config_path, data_dir)

    core_thread = threading.Thread(target=run_core, daemon=True)
    core_thread.start()

    # 等待 Core 就绪（简单方案：sleep 2秒）
    time.sleep(2)

    # 2. 启动飞书插件
    feishu_proc = _spawn_plugin_process(
        "supercc.adapter.feishu", config_path, data_dir, core_url
    )
    logger.info("[Bridge] Feishu plugin started (pid=%d)", feishu_proc.pid)

    # 3. 启动企业微信插件（如已配置）
    if _is_wecom_enabled(config_path):
        wecom_proc = _spawn_plugin_process(
            "supercc.plugin.wecom", config_path, data_dir, core_url
        )
        logger.info("[Bridge] WeCom plugin started (pid=%d)", wecom_proc.pid)

    # 4. 阻塞主进程（让插件进程运行）
    core_thread.join()
```

- [ ] **Step 2: 提交**

```bash
git add supercc/main.py
git commit -m "feat(bridge): support spawning plugin as subprocess processes"
```

---

## 任务 7: 端到端验证

- [ ] **Step 1: Linux 验证**

```bash
# 安装 systemd service
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now supercc-main.service
systemctl --user enable --now supercc-feishu.service
systemctl --user status supercc-main.service
```

- [ ] **Step 2: macOS 验证**

```bash
# 安装 launchd
cp deploy/launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.supercc.main.plist
launchctl load ~/Library/LaunchAgents/com.supercc.feishu.plist
```

- [ ] **Step 3: 进程隔离验证**

```bash
# 杀掉某个插件进程，观察是否自动重启
kill -9 $(pgrep -f "supercc.adapter.feishu")
sleep 6
pgrep -f "supercc.adapter.feishu"  # 应该重新出现
```

---

## 任务 8: 更新 Skill

**Files:**
- Modify: `.supercc/skills/channel-restart-supervisor/SKILL.md`（已在上次更新）

---

## 检查清单

| 任务 | 状态 | 验证命令 |
|------|------|----------|
| 拆分 main.py | ⏳ | `python -m supercc main --core-only --help` |
| 飞书插件入口 | ⏳ | `python -m supercc.adapter.feishu` |
| 企业微信插件入口 | ⏳ | `python -m supercc.plugin.wecom` |
| platform.py 多进程扩展 | ⏳ | `grep -n "ServiceType" supercc/gateway/platform.py` |
| systemd service | ⏳ | `ls deploy/systemd/` |
| launchd plist | ⏳ | `ls deploy/launchd/` |
| Windows install.bat | ⏳ | `ls deploy/windows/` |
| 子进程启动 | ⏳ | `ps aux \| grep supercc` |
| 端到端验证 | ⏳ | — |
