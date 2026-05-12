"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable
from wecom_aibot_sdk import generate_req_id

logger = logging.getLogger(__name__)


class WeComCoreWSClient:
    """
    企业微信插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 WeCom 消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，通过 SDK 的 reply_stream 渲染为企业微信格式并发送
    """

    def __init__(
        self,
        core_url: str,
        ws_client,          # WeComWSClient (SDK-based)
        wecom_client: WeComClient,
        bot_id: str,
        project_path: str,
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.ws_client = ws_client       # SDK WSClient
        self.wecom = wecom_client        # WeComClient (消息发送)
        self.bot_id = bot_id
        self.project_path = project_path
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        # req_id → (message_id, chat_id)
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0
        self._sent_message_ids: set[str] = set()       # 幂等性（背景任务主动发送去重）
        self._sent_notification_ids: set[str] = set()

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[WeComCore] Connected to core")
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """持续读取核心发来的消息。"""
        import websockets
        while self._running and self._ws:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)
                await self._handle_core_message(data)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception:
                logger.exception("[WeComCore] Error reading message")

    async def _handle_core_message(self, data: dict):
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
                # 流结束，清理 ws_client 中缓存的 frame
                self.ws_client.pop_frame(msg_id)
                # 触发后台任务
                result_data = data.get("result", {})
                tool_count = result_data.get("tool_call_count", 0) if isinstance(result_data, dict) else 0
                asyncio.create_task(self._trigger_background_tasks(chat_id, tool_count))
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)

    async def _render_and_send(self, params: dict):
        """通过 SDK reply_stream 发送流式回复。"""
        content = params.get("content", "")
        message_id = params.get("message_id", "")

        if not content:
            return

        if message_id:
            frame = self.ws_client.get_frame(message_id)
            stream_id = generate_req_id("stream")
            try:
                if frame:
                    # 有原始帧 → 用 reply_stream 流式回复
                    await self.ws_client.reply_stream(
                        frame=frame,
                        stream_id=stream_id,
                        content=content,
                        finish=True,
                    )
                else:
                    # 无帧 → 降级为主动发送
                    await self.wecom.send_markdown(message_id, content)
            except Exception as e:
                logger.warning(f"[WeComCore] reply_stream failed: {e}")
                # 流式失败，尝试降级
                try:
                    await self.wecom.send_markdown(message_id, content)
                except Exception:
                    pass
        else:
            # 无 message_id → 用 chat_id 主动发送
            chat_id = params.get("chat_id", "")
            if chat_id:
                try:
                    await self.wecom.send_markdown(chat_id, content)
                except Exception as e:
                    logger.warning(f"[WeComCore] send_markdown failed: {e}")

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：发送工具执行结果。"""
        tool_name = params.get("tool_name", "")
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        result_content = f"[{tool_name}] 执行完成"
        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": result_content,
            "chat_id": chat_id,
        })

    async def _check_group_permissions(self, inbound) -> bool:
        """检查群聊权限。返回 True=允许通过，False=已拦截（已发送授权卡片）。"""
        is_group = inbound.extra.get("is_group_chat", False)

        if is_group:
            entry = self._groups.get(inbound.session_key.chat_id)
            if entry is None:
                reason = "该群未配置使用权限，请联系管理员。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            if not getattr(entry, "enabled", True):
                reason = "该群已被禁用。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                reason = "请 @CC 我来使用 SuperCC。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            allow_from = getattr(entry, "allow_from", [])
            if allow_from and inbound.user_open_id not in allow_from:
                reason = "你在该群中没有使用权限。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            return True
        else:
            if self._allowed_users and inbound.user_open_id not in self._allowed_users:
                reason = "你不在允许使用列表中。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False
            return True

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心，并等待响应。"""
        inbound = incoming_to_inbound(msg, bot_id=self.bot_id, project_path=self.project_path)

        # 群聊权限校验
        if not await self._check_group_permissions(inbound):
            return {}

        req = JsonRpcRequest(
            id=self._next_id(),
            method="wecom.message",
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": inbound.user_open_id,
                "platform": inbound.session_key.platform,
                "project_path": inbound.session_key.project_path,
                "content": inbound.content,
                "message_type": inbound.message_type.value,
                "is_group_chat": inbound.extra.get("is_group_chat", False),
                "mention_bot": inbound.extra.get("mention_bot", False),
                "mention_ids": inbound.extra.get("mention_ids", []),
                "group_name": inbound.extra.get("group_name", ""),
                "extra": inbound.extra,
            },
        )

        # frame 已在 ws_client._handle_message 中通过 message_id 缓存
        # 这里不需要重复存储

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        self._pending_message_ids[str(req.id)] = (inbound.message_id, inbound.session_key.chat_id)
        await self._ws.send(json.dumps(req.to_dict()))
        result = await future
        return result or {}

    async def _send_event(self, method: str, params: dict):
        """发送 Event notification 到核心。"""
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._ws:
            await self._ws.send(json.dumps(frame))

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def _trigger_background_tasks(self, chat_id: str, tool_count: int):
        """触发后台任务（SkillNudge、Memory Review）。"""
        if tool_count > 0:
            asyncio.create_task(self._do_skill_nudge(chat_id, tool_count))
        asyncio.create_task(self._do_memory_review(chat_id))

    async def _do_skill_nudge(self, chat_id: str, tool_count: int):
        notif_id = f"{chat_id}:skill_nudge"
        if notif_id in self._sent_notification_ids:
            return
        self._sent_notification_ids.add(notif_id)
        try:
            content = f"🧰 你在本次对话中使用了 {tool_count} 个工具调用。想了解相关技能吗？"
            await self.wecom.send_text(chat_id, content)
        except Exception as e:
            logger.warning(f"[WeComCore] skill_nudge failed: {e}")

    async def _do_memory_review(self, chat_id: str):
        notif_id = f"{chat_id}:memory_review"
        if notif_id in self._sent_notification_ids:
            return
        self._sent_notification_ids.add(notif_id)
        try:
            content = "📝 对话结束。你想保存这次重要的信息到记忆吗？"
            await self.wecom.send_text(chat_id, content)
        except Exception as e:
            logger.warning(f"[WeComCore] memory_review failed: {e}")

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()
