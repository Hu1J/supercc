# Claude Code 飞书桥接插件 — 扫码安装实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 MVP 基础上增加扫码安装体验：检测无配置文件时自动触发安装向导，终端打印二维码，用户飞书扫码后自动创建机器人并写入配置。

**Architecture:** 新增 `src/install/` 模块处理扫码安装流程，`main.py` 入口检测配置文件决定走安装还是启动服务。安装流程参考飞书 App Registration API（OAuth Device Flow 扫码创建 PersonalAgent）。

**Tech Stack:** Python 3.11+, qrcode, lark-oapi, aiohttp, claude-agent-sdk

---

## 新增文件结构

```
src/install/
├── __init__.py
├── api.py      # 飞书 App Registration API (init/begin/poll)
├── qr.py       # 终端二维码打印
└── flow.py     # 安装状态机
```

---

## Task 9: 改造 main.py — 检测配置并分发

**Files:**
- Modify: `src/main.py` (完整重写)
- Modify: `src/config.py` (新增 `save_config`)

- [ ] **Step 1: 创建 `src/install/__init__.py`**

```python
"""Install flow package."""
```

- [ ] **Step 2: 创建 `src/install/api.py`**

```python
"""Feishu App Registration API — init/begin/poll for creating a bot via QR scan."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class AppRegistrationResult:
    app_id: str
    app_secret: str
    user_open_id: str
    domain: str  # "feishu" or "lark"


@dataclass
class BeginResult:
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    user_code: Optional[str] = None


class FeishuInstallAPI:
    BASE_URL_FEISHU = "https://open.feishu.cn"
    BASE_URL_LARK = "https://open.larksuite.com"

    def __init__(self, env: str = "prod"):
        self.env = env
        self._base_url = self.BASE_URL_FEISHU

    def set_domain(self, is_lark: bool):
        self._base_url = self.BASE_URL_LARK if is_lark else self.BASE_URL_FEISHU

    async def init(self) -> dict:
        """Initialize app registration. Returns supported_auth_methods."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base_url}/oauth/v1/app_registration",
                data={"action": "init"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def begin(self) -> BeginResult:
        """Start app registration flow. Returns QR URI + device_code."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base_url}/oauth/v1/app_registration",
                data={
                    "action": "begin",
                    "archetype": "PersonalAgent",
                    "auth_method": "client_secret",
                    "request_user_info": "open_id",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        return BeginResult(
            device_code=data["device_code"],
            verification_uri=data["verification_uri"],
            verification_uri_complete=data["verification_uri_complete"],
            expires_in=data.get("expires_in", 600),
            interval=data.get("interval", 5),
            user_code=data.get("user_code"),
        )

    async def poll(self, device_code: str, timeout: int = 600) -> AppRegistrationResult:
        """
        Poll until user completes QR scan and registration.
        Returns client_id, client_secret, open_id when ready.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = time.monotonic()
            interval = 5

            while time.monotonic() - start < timeout:
                resp = await client.post(
                    f"{self._base_url}/oauth/v1/app_registration",
                    data={
                        "action": "poll",
                        "device_code": device_code,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                data = resp.json()

                if data.get("error"):
                    err = data["error"]
                    if err == "authorization_pending":
                        await asyncio.sleep(interval)
                        continue
                    elif err == "access_denied":
                        raise RuntimeError("用户拒绝了授权 (access_denied)")
                    elif err in ("expired_token", "authorization_timeout"):
                        raise RuntimeError("授权已过期，请重新扫码 (expired)")
                    else:
                        raise RuntimeError(f"授权失败: {err}")

                if data.get("client_id") and data.get("client_secret"):
                    is_lark = data.get("user_info", {}).get("tenant_brand") == "lark"
                    return AppRegistrationResult(
                        app_id=data["client_id"],
                        app_secret=data["client_secret"],
                        user_open_id=data.get("user_info", {}).get("open_id", ""),
                        domain="lark" if is_lark else "feishu",
                    )

                await asyncio.sleep(interval)

            raise RuntimeError("扫码超时，请重新运行安装命令")
```

- [ ] **Step 3: 创建 `src/install/qr.py`**

```python
"""Terminal QR code printing."""
from __future__ import annotations

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False


def print_qr(url: str) -> None:
    """Print QR code to terminal using qrcode library."""
    if not QRCODE_AVAILABLE:
        print(f"\n请用飞书扫码打开链接:\n{url}\n")
        return

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # Print as ASCII
    ascii_str = ""
    for row in img.getdata():
        for pixel in row:
            ascii_str += "  " if pixel else "██"
        ascii_str += "\n"
    print(ascii_str)
    print(f"\n或者直接打开: {url}\n")
```

- [ ] **Step 4: 创建 `src/install/flow.py`**

```python
"""Install flow state machine."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from src.install.api import FeishuInstallAPI, AppRegistrationResult
from src.install.qr import print_qr

logger = logging.getLogger(__name__)


async def run_install_flow(config_path: str = "config.yaml") -> AppRegistrationResult:
    """Run the full install flow: init → begin → QR → poll → save config."""
    print("\n🚀 开始安装 cc-feishu-bridge...\n")

    api = FeishuInstallAPI()

    # Step 1: Init
    print("正在初始化...")
    await api.init()
    print("初始化完成")

    # Step 2: Begin → get QR URL
    print("正在获取二维码...")
    begin_result = await api.begin()

    # Build QR URL with from=onboard tag
    qr_url = begin_result.verification_uri_complete
    if "?" in qr_url:
        qr_url += "&from=onboard"
    else:
        qr_url += "?from=onboard"

    # Step 3: Print QR
    print("\n" + "=" * 50)
    print("请使用飞书扫码完成配置（请确保已在飞书开放平台创建应用）")
    print("=" * 50 + "\n")
    print_qr(qr_url)
    print("等待扫码完成...\n")

    # Step 4: Poll for result
    try:
        result = await api.poll(begin_result.device_code, timeout=begin_result.expires_in)
    except RuntimeError as e:
        print(f"\n❌ 安装失败: {e}")
        raise

    # Step 5: Save config
    print(f"\n✅ 机器人创建成功！")
    print(f"   App ID: {result.app_id}")
    print(f"   用户 Open ID: {result.user_open_id}")
    print(f"   域名: {result.domain}")

    save_config(result, config_path)
    return result


def save_config(result: AppRegistrationResult, config_path: str) -> None:
    """Write the app credentials and defaults to config.yaml."""
    config = {
        "feishu": {
            "app_id": result.app_id,
            "app_secret": result.app_secret,
            "bot_name": "Claude",
            "domain": result.domain,
        },
        "auth": {
            "allowed_users": [result.user_open_id],
        },
        "claude": {
            "cli_path": "claude",
            "max_turns": 50,
            "approved_directory": str(Path.home() / "projects"),
        },
        "storage": {
            "db_path": "./data/sessions.db",
        },
        "server": {
            "host": "0.0.0.0",
            "port": 8080,
            "webhook_path": "/feishu/webhook",
        },
    }

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print(f"\n✅ 配置已保存到 {config_path}")
```

- [ ] **Step 5: 创建 `tests/test_install_api.py`**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.install.api import FeishuInstallAPI, AppRegistrationResult, BeginResult


@pytest.fixture
def api():
    return FeishuInstallAPI()


def test_default_domain_is_feishu(api):
    assert api._base_url == "https://open.feishu.cn"


def test_set_domain_lark(api):
    api.set_domain(is_lark=True)
    assert api._base_url == "https://open.larksuite.com"


def test_set_domain_feishu(api):
    api.set_domain(is_lark=False)
    assert api._base_url == "https://open.feishu.cn"


def test_begin_result_dataclass():
    result = BeginResult(
        device_code="device123",
        verification_uri="https://example.com/verify",
        verification_uri_complete="https://example.com/verify?code=user123",
        expires_in=600,
        interval=5,
    )
    assert result.device_code == "device123"
    assert result.expires_in == 600


def test_app_registration_result_dataclass():
    result = AppRegistrationResult(
        app_id="cli_xxx",
        app_secret="secret",
        user_open_id="ou_123",
        domain="feishu",
    )
    assert result.app_id == "cli_xxx"
    assert result.domain == "feishu"
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_install_api.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/install/ tests/test_install_api.py
git commit -m "feat: add install flow (api, qr, flow)"
```

---

## Task 10: 改造 main.py — 入口检测 + config.py 新增写入

**Files:**
- Modify: `src/main.py`
- Modify: `src/config.py`
- Modify: `requirements.txt`

- [ ] **Step 1: 添加 qrcode 到 `requirements.txt`**

```diff
+ qrcode>=7.4.0
```

- [ ] **Step 2: 更新 `src/config.py` — 新增 `save_config`**

Read the current `src/config.py` and add this method to the `Config` class or as a standalone function:

```python
def save_config(path: str, feishu_app_id: str, feishu_app_secret: str,
                domain: str, bot_name: str,
                allowed_users: list[str],
                claude_cli_path: str, claude_max_turns: int,
                claude_approved_directory: str,
                storage_db_path: str,
                server_host: str, server_port: int, server_webhook_path: str) -> None:
    """Save a complete config to a YAML file."""
    config = {
        "feishu": {
            "app_id": feishu_app_id,
            "app_secret": feishu_app_secret,
            "bot_name": bot_name,
            "domain": domain,
        },
        "auth": {
            "allowed_users": allowed_users,
        },
        "claude": {
            "cli_path": claude_cli_path,
            "max_turns": claude_max_turns,
            "approved_directory": claude_approved_directory,
        },
        "storage": {
            "db_path": storage_db_path,
        },
        "server": {
            "host": server_host,
            "port": server_port,
            "webhook_path": server_webhook_path,
        },
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
```

- [ ] **Step 3: 重写 `src/main.py`**

```python
"""CLI entry point — detects config and routes to install or start."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from src.config import load_config, save_config
from src.feishu.client import FeishuClient
from src.feishu.message_handler import MessageHandler
from src.security.auth import Authenticator
from src.security.validator import SecurityValidator
from src.claude.integration import ClaudeIntegration
from src.claude.session_manager import SessionManager
from src.format.reply_formatter import ReplyFormatter

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"


async def webhook_handler(request: web.Request) -> web.Response:
    handler: MessageHandler = request.app["handler"]
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    message = handler.feishu.parse_incoming_message(body)
    if not message:
        return web.Response(status=200, text="OK")

    asyncio.create_task(handler.handle(message))
    return web.Response(status=200, text="OK")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")


def create_app(config):
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
    )
    authenticator = Authenticator(allowed_users=config.auth.allowed_users)
    validator = SecurityValidator(approved_directory=config.claude.approved_directory)
    claude = ClaudeIntegration(
        cli_path=config.claude.cli_path,
        max_turns=config.claude.max_turns,
        approved_directory=config.claude.approved_directory,
    )
    session_manager = SessionManager(db_path=config.storage.db_path)
    formatter = ReplyFormatter()

    handler = MessageHandler(
        feishu_client=feishu,
        authenticator=authenticator,
        validator=validator,
        claude=claude,
        session_manager=session_manager,
        formatter=formatter,
        approved_directory=config.claude.approved_directory,
    )

    app = web.Application()
    app["handler"] = handler
    app.router.add_post(config.server.webhook_path, webhook_handler)
    app.router.add_get("/health", health_handler)
    return app


async def run_server(config_path: str):
    config = load_config(config_path)
    app = create_app(config)
    logger.info(f"Starting server on {config.server.host}:{config.server.port}")
    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        print=None,
    )


def detect_config(config_path: str) -> bool:
    """Check if config file exists and is non-empty."""
    p = Path(config_path)
    return p.exists() and p.stat().st_size > 0


async def interactive_install(config_path: str):
    """Run the QR-code install flow, then start server."""
    from src.install.flow import run_install_flow
    result = await run_install_flow(config_path)
    # After install flow saves config, load and start server
    await run_server(config_path)


def main():
    parser = argparse.ArgumentParser(description="Claude Code Feishu Bridge")
    parser.add_argument(
        "--config",
        type=str,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if detect_config(args.config):
        logger.info(f"Config found at {args.config}, starting server...")
        asyncio.run(run_server(args.config))
    else:
        logger.info(f"No config found at {args.config}, running install flow...")
        asyncio.run(interactive_install(args.config))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 添加 pytest 依赖 to requirements.txt**

```diff
+ pytest>=7.0
+ pytest-asyncio>=0.21
```

- [ ] **Step 5: Run tests**

Run: `python -c "from src.main import main; print('import OK')"`
Expected: "import OK"

- [ ] **Step 6: Commit**

```bash
git add src/main.py src/config.py requirements.txt
git commit -m "feat: update main.py to auto-detect config and trigger install flow"
```

---

## Task 11: 最终验证

- [ ] **Step 1: 安装新增依赖**

Run: `pip install qrcode>=7.4.0 httpx>=0.25.0 pytest-asyncio>=0.21`

- [ ] **Step 2: 运行所有测试**

Run: `pytest -v`
Expected: All 26+ tests pass

- [ ] **Step 3: 验证 import**

Run: `python -c "from src.install.api import FeishuInstallAPI; from src.install.flow import run_install_flow; print('OK')"`
Expected: "OK"

- [ ] **Step 4: Commit**

```bash
git add -a && git commit -m "feat: complete v2 with QR install flow"
```

---

## 依赖汇总（新增）

| 包 | 版本 | 用途 |
|----|------|------|
| `qrcode` | >=7.4.0 | 终端二维码打印 |
| `httpx` | >=0.25.0 | Async HTTP client（用于飞书安装 API） |

---

## 自检清单

1. **Spec 覆盖:** 扫码安装流程、main.py 入口分发、config 写入全部覆盖 ✓
2. **Placeholder 扫描:** 无 TBD/TODO ✓
3. **类型一致性:** `FeishuInstallAPI`, `BeginResult`, `AppRegistrationResult` 接口一致 ✓
4. **测试覆盖:** install/api.py 有单元测试 ✓
