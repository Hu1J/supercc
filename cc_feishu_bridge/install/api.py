"""Feishu App Registration API — init/begin/poll for creating a bot via QR scan."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import logging

logger = logging.getLogger(__name__)


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
    """
    Feishu App Registration API using OAuth Device Flow.

    Endpoints:
      - App registration : POST /oauth/v1/app/registration (init/begin/poll)

    Brand-aware base URLs:
      - Feishu accounts : https://accounts.feishu.cn
      - Lark  accounts  : https://accounts.larksuite.com
    """

    BASE_ACCOUNTS_FEISHU = "https://accounts.feishu.cn"
    BASE_ACCOUNTS_LARK = "https://accounts.larksuite.com"

    def __init__(self, app_id: str = "", app_secret: str = "", brand: str = "feishu"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.brand = brand  # "feishu" or "lark"
        self._accounts_base = self.BASE_ACCOUNTS_LARK if brand == "lark" else self.BASE_ACCOUNTS_FEISHU
        self._client: Optional[httpx.AsyncClient] = None

    def _accounts_url(self, path: str) -> str:
        return f"{self._accounts_base}{path}"

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a shared client with cookie persistence (for nonce tracking)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                cookies=httpx.Cookies(),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close the shared HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def init(self) -> dict:
        """Initialize app registration. Returns { nonce, supported_auth_methods }."""
        client = await self._get_client()
        resp = await client.post(
            self._accounts_url("/oauth/v1/app/registration"),
            data={"action": "init"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()

    async def begin(self) -> BeginResult:
        """
        Start app registration flow.
        The server associates this call with the nonce from init() via session cookie.
        Returns QR URI + device_code.
        """
        client = await self._get_client()
        resp = await client.post(
            self._accounts_url("/oauth/v1/app/registration"),
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
        client = await self._get_client()
        start = time.monotonic()
        interval = 5

        while time.monotonic() - start < timeout:
            resp = await client.post(
                self._accounts_url("/oauth/v1/app/registration"),
                data={
                    "action": "poll",
                    "device_code": device_code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = resp.json()

            # authorization_pending means user hasn't completed yet — keep polling
            if data.get("error") == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            elif data.get("error") == "access_denied":
                raise RuntimeError("用户拒绝了授权 (access_denied)")
            elif data.get("error") in ("expired_token", "authorization_timeout"):
                raise RuntimeError("授权已过期，请重新扫码 (expired)")
            elif data.get("error"):
                raise RuntimeError(f"授权失败: {data['error']}")

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
