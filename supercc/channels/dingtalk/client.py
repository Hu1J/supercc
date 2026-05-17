"""钉钉消息发送客户端（aiohttp 直连 session webhook 实现）。"""
from __future__ import annotations

import aiohttp
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

DINGTALK_WEBHOOK_RE = re.compile(r"^https://(?:api|oapi)\.dingtalk\.com/")


class DingTalkClient:
    """
    钉钉消息发送客户端。

    封装 session webhook 发送，支持：
    - send_text：发送 Markdown 文本消息
    - send_markdown：发送 Markdown 消息（通过 webhook）
    """

    def __init__(self, app_key: str, app_secret: str):
        self._app_key = app_key
        self._app_secret = app_secret
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP session。"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def close(self) -> None:
        """关闭 HTTP session。"""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    async def send_text(
        self,
        session_webhook: str,
        text: str,
        chat_id: str = "",
    ) -> str:
        """发送 Markdown 消息到指定 session webhook。

        Args:
            session_webhook: 钉钉会话的 session webhook（从入站消息中获取）
            text: Markdown 格式的文本内容
            chat_id: 会话 ID（用于日志）

        Returns:
            消息 ID（如果发送成功）
        """
        if not session_webhook or not DINGTALK_WEBHOOK_RE.match(session_webhook):
            logger.warning("[DingTalk] Invalid session_webhook: %s", session_webhook[:50] if session_webhook else "None")
            return ""

        normalized = self._normalize_markdown(text[:20000])

        payload = {
            "msg": {
                "msgtype": "markdown",
                "markdown": {
                    "title": "Claude",
                    "text": normalized,
                }
            }
        }

        try:
            sess = await self._get_session()
            async with sess.post(session_webhook, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status < 300:
                    data = await resp.json()
                    msg_id = data.get("msgId", "") or data.get("messageId", "") or ""
                    logger.info("[DingTalk] send_text success: chat_id=%s, msg_id=%s", chat_id[:20] if chat_id else "?", msg_id[:20] if msg_id else "OK")
                    return str(msg_id)
                else:
                    body = await resp.text()
                    logger.warning("[DingTalk] send_text failed HTTP %d: %s", resp.status, body[:200])
                    return ""
        except aiohttp.TimeoutError:
            logger.warning("[DingTalk] send_text timeout: chat_id=%s", chat_id[:20] if chat_id else "?")
            return ""
        except Exception as exc:
            logger.error("[DingTalk] send_text error: %s", exc)
            return ""

    async def send_markdown(
        self,
        session_webhook: str,
        title: str,
        content: str,
        chat_id: str = "",
    ) -> str:
        """发送 Markdown 消息（通用接口）。

        Args:
            session_webhook: 钉钉会话的 session webhook
            title: 消息标题
            content: Markdown 内容
            chat_id: 会话 ID

        Returns:
            消息 ID
        """
        if not session_webhook or not DINGTALK_WEBHOOK_RE.match(session_webhook):
            logger.warning("[DingTalk] Invalid session_webhook for send_markdown")
            return ""

        normalized = self._normalize_markdown(content[:20000])

        payload = {
            "msg": {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": normalized,
                }
            }
        }

        try:
            sess = await self._get_session()
            async with sess.post(session_webhook, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status < 300:
                    data = await resp.json()
                    return str(data.get("msgId", "") or data.get("messageId", "") or "")
                else:
                    body = await resp.text()
                    logger.warning("[DingTalk] send_markdown failed HTTP %d: %s", resp.status, body[:200])
                    return ""
        except Exception as exc:
            logger.error("[DingTalk] send_markdown error: %s", exc)
            return ""

    async def send_text_direct(
        self,
        chat_id: str,
        text: str,
        token: str = "",
    ) -> str:
        """直接发送到 chat_id（需要 access token，不推荐）。

        推荐使用 send_text(session_webhook=...) 方式。
        """
        # 这个方法保留用于未来 OpenAPI 调用
        logger.debug("[DingTalk] send_text_direct not implemented, use session_webhook")
        return ""

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        """Normalize markdown for DingTalk's parser.

        DingTalk's markdown renderer has quirks:
        - Numbered lists need blank line before them
        - Indented code blocks may render incorrectly
        """
        lines = text.split("\n")
        out = []
        for i, line in enumerate(lines):
            # Ensure blank line before numbered list items
            is_numbered = re.match(r"^\d+\.\s", line.strip())
            if is_numbered and i > 0:
                prev = lines[i - 1]
                if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                    out.append("")
            # Dedent fenced code blocks
            if line.strip().startswith("```") and line != line.lstrip():
                indent = len(line) - len(line.lstrip())
                line = line[indent:]
            out.append(line)
        return "\n".join(out)
