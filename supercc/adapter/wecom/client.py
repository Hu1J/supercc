"""WeCom HTTP API 客户端：发送消息、媒体上传等。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


async def _call_api(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """通用 HTTP 调用。"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=body) as resp:
            return await resp.json()


class WeComClient:
    """WeCom HTTP API 客户端。"""

    BASE_URL = "https://qyapi.weixin.qq.com"

    def __init__(self, corp_id: str, agent_id: str, corp_secret: str):
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.corp_secret = corp_secret
        self._access_token: str | None = None

    async def _get_token(self) -> str:
        """获取 access_token。"""
        if self._access_token:
            return self._access_token
        url = f"{self.BASE_URL}/cgi-bin/gettoken"
        params = {"corpid": self.corp_id, "corpsecret": self.corp_secret}
        data = await _call_api("GET", url, {}, None)
        self._access_token = data["access_token"]
        return self._access_token

    async def send_text(self, chat_id: str, text: str) -> str:
        """发送文本消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text},
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send failed: {data}")
        return data.get("msgid", "")

    async def send_markdown(self, chat_id: str, content: str) -> str:
        """发送 Markdown 消息（企业微信原生支持）。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "markdown",
            "agentid": self.agent_id,
            "markdown": {"content": content},
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send markdown failed: {data}")
        return data.get("msgid", "")

    async def upload_media(self, file_data: bytes, file_name: str, media_type: str = "file") -> str:
        """上传临时媒体，返回 media_id。"""
        import aiohttp
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/media/upload"
        params = {"access_token": token, "type": media_type}
        form = aiohttp.FormData()
        form.add_field("media", file_data, filename=file_name, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params, data=form) as resp:
                data = await resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom upload failed: {data}")
        return data["media_id"]

    async def send_file(self, chat_id: str, media_id: str) -> str:
        """发送文件消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "file",
            "agentid": self.agent_id,
            "file": {"media_id": media_id},
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send file failed: {data}")
        return data.get("msgid", "")

    async def send_image(self, chat_id: str, media_id: str) -> str:
        """发送图片消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "image",
            "agentid": self.agent_id,
            "image": {"media_id": media_id},
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send image failed: {data}")
        return data.get("msgid", "")

    async def send_typing_indicator(self, chat_id: str) -> str:
        """发送'正在思考...'提示（WeCom 模板卡片实现）。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "template_card",
            "agentid": self.agent_id,
            "template_card": {
                "card_type": "text_notice",
                "source": {
                    "desc": "SuperCC",
                },
                "main_title": {
                    "title": "正在思考...",
                    "desc": "",
                },
            },
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            logger.warning(f"[WeCom] send_typing_indicator failed: {data}")
        return data.get("msgid", "")

    async def send_authorization_card(self, chat_id: str, reason: str) -> str:
        """发送权限不足引导卡片。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "template_card",
            "agentid": self.agent_id,
            "template_card": {
                "card_type": "button_interaction",
                "source": {
                    "desc": "SuperCC 权限",
                },
                "main_title": {
                    "title": "权限不足",
                    "desc": reason,
                },
                "action": {
                    "button_list": [
                        {
                            "name": "联系管理员",
                            "action_type": "click",
                            "remark": "请联系管理员授权后重试",
                        }
                    ]
                },
            },
        }
        data = await _call_api("POST", url, params, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom authorization card failed: {data}")
        return data.get("msgid", "")
