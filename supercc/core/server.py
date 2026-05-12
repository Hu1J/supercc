"""WebSocket Server with JSON-RPC 2.0 routing for core <-> plugin communication."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from supercc.core.protocol import (
    Event,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    SessionKey,
    ErrorCode,
)

logger = logging.getLogger(__name__)

# Task-local context variable：当前处理中的 WebSocket 连接
_conn_var: contextvars.ContextVar[Connection | None] = contextvars.ContextVar(
    "_conn_var", default=None
)

WsHandler = Callable[[JsonRpcRequest], Awaitable[Optional[JsonRpcResponse]]]
WsEventHandler = Callable[[dict], Awaitable[None]]


# ── 路由表 ─────────────────────────────────────────────────────────────────

class Router:
    """
    JSON-RPC 方法路由器。

    方法命名: "plugin.method" （点分隔，plugin 是插件名）
    核心方法: "core.subscribe" | "core.unsubscribe" | "core.worker_status"
    """

    def __init__(self):
        self._handlers: dict[str, WsHandler] = {}

    def add(self, method: str, handler: WsHandler):
        self._handlers[method] = handler

    async def dispatch(self, req: JsonRpcRequest) -> Optional[JsonRpcResponse]:
        handler = self._handlers.get(req.method)
        if handler is None:
            return JsonRpcResponse(
                id=req.id,
                error=JsonRpcError(
                    code=ErrorCode.METHOD_NOT_FOUND,
                    message=f"Method not found: {req.method}",
                ),
            )
        try:
            result = await handler(req)
            return JsonRpcResponse(id=req.id, result=result)
        except Exception as e:
            logger.exception(f"[Router] Handler error for {req.method}")
            return JsonRpcResponse(
                id=req.id,
                error=JsonRpcError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=str(e),
                ),
            )


# ── 连接状态 ────────────────────────────────────────────────────────────────

@dataclass
class Connection:
    """一个插件客户端的 WebSocket 连接状态。"""
    ws: Any                       # websockets.WebSocketServerProtocol
    plugin_id: str                # 插件标识: "feishu" | "wecom" | ...
    platform: str                 # 平台: "feishu" | "wecom" | ...
    subscribed_keys: set[SessionKey] = field(default_factory=set)
    alive: bool = True


# ── WebSocket Server ────────────────────────────────────────────────────────

class WsServer:
    """
    WebSocket JSON-RPC 2.0 服务器。

    协议：
    - 插件通过 WebSocket 连接核心，发送 JSON-RPC 2.0 Request
    - 核心通过同一 WebSocket 发送 Response 或 Event notification
    - Session Key 在 connect 时的 params 中传递

    JSON-RPC 帧:
    - Request:  {"jsonrpc": "2.0", "id": 1, "method": "...", "params": {...}}
    - Response: {"jsonrpc": "2.0", "id": 1, "result": {...}}
    - Error:    {"jsonrpc": "2.0", "id": 1, "error": {"code": ..., "message": "..."}}
    - Event:    {"jsonrpc": "2.0", "method": "...", "params": {...}}  (无 id)
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 executor: Any | None = None):
        self.host = host
        self.port = port
        self.router = Router()
        self._connections: dict[str, Connection] = {}  # connection_id -> Connection
        self._server: Optional[Any] = None
        self._running = asyncio.Event()
        self._executor = executor

        # 注册核心方法
        self._setup_core_methods()

    def _setup_core_methods(self):
        """注册核心内置方法。"""
        self.router.add("core.subscribe", self._handle_subscribe)
        self.router.add("core.unsubscribe", self._handle_unsubscribe)
        self.router.add("core.worker_status", self._handle_worker_status)
        self.router.add("core.ping", self._handle_ping)
        self.router.add("feishu.message", self._handle_message)
        self.router.add("feishu.notify", self._handle_notify)
        self.router.add("wecom.message", self._handle_wecom_message)
        self.router.add("wecom.notify", self._handle_notify)

    # ── 核心方法处理 ────────────────────────────────────────────────────────

    async def _handle_subscribe(self, req: JsonRpcRequest) -> dict:
        """插件订阅 SessionKey。"""
        conn = self._get_conn_by_req_id(req.id)
        if not conn:
            return {"error": "connection not found"}
        keys = req.params.get("keys", [])
        for k in keys:
            if isinstance(k, dict):
                key = SessionKey(
                    bot_id=k.get("bot_id", ""),
                    project_path=k.get("project_path", ""),
                    platform=k.get("platform", ""),
                    chat_id=k.get("chat_id", ""),
                )
                conn.subscribed_keys.add(key)
        return {"subscribed": len(conn.subscribed_keys)}

    async def _handle_unsubscribe(self, req: JsonRpcRequest) -> dict:
        """插件取消订阅 SessionKey。"""
        conn = self._get_conn_by_req_id(req.id)
        if not conn:
            return {"error": "connection not found"}
        keys = req.params.get("keys", [])
        for k in keys:
            if isinstance(k, dict):
                key = SessionKey(
                    bot_id=k.get("bot_id", ""),
                    project_path=k.get("project_path", ""),
                    platform=k.get("platform", ""),
                    chat_id=k.get("chat_id", ""),
                )
                conn.subscribed_keys.discard(key)
        return {"subscribed": len(conn.subscribed_keys)}

    async def _handle_worker_status(self, req: JsonRpcRequest) -> dict:
        """返回 Worker 池状态。"""
        from supercc.core.worker import WorkerPool
        pool = WorkerPool.instance() if hasattr(WorkerPool, 'instance') else None
        if pool:
            return asyncio.run_coroutine_threadsafe(pool.stats(), asyncio.get_event_loop())
        return {"total_workers": 0, "idle": 0, "busy": 0}

    async def _handle_ping(self, req: JsonRpcRequest) -> dict:
        """Ping-pong。"""
        return {"pong": True}

    async def _handle_message(self, req: JsonRpcRequest) -> dict:
        """处理来自飞书插件的消息。"""
        from supercc.core.protocol import InboundMessage, MessageRole, MessageType, _cst_now

        params = req.params

        # 重建 SessionKey
        key = SessionKey(
            bot_id=params.get("bot_id", ""),
            project_path=params.get("project_path", ""),
            platform=params.get("platform", "feishu"),
            chat_id=params.get("chat_id", ""),
        )

        # 转换 params → InboundMessage
        msg_type_str = params.get("message_type", "text")
        msg_type_map = {
            "text": MessageType.TEXT,
            "image": MessageType.IMAGE,
            "file": MessageType.FILE,
            "audio": MessageType.FILE,
        }
        msg_type = msg_type_map.get(msg_type_str, MessageType.TEXT)

        inbound = InboundMessage(
            event="message",
            session_key=key,
            message_id=params.get("message_id", ""),
            role=MessageRole.USER,
            content=params.get("content", ""),
            message_type=msg_type,
            media_path=None,
            user_open_id=params.get("user_open_id") or None,
            thread_id=params.get("thread_id") or None,
            timestamp=_cst_now(),
            extra={
                "raw": params.get("raw", ""),
                "is_group_chat": params.get("is_group_chat", False),
                "mention_bot": params.get("mention_bot", False),
                "mention_ids": params.get("mention_ids", []),
                "group_name": params.get("group_name", ""),
                "chat_type": params.get("chat_type", "p2p"),
            },
        )

        # 通过 executor 处理（push_fn 通过 WS 推送响应帧）
        if self._executor is None:
            raise RuntimeError("No executor configured")

        # push_fn：找到当前连接，发送 OutboundMessage 为 Event notification
        async def push_fn(msg: Any) -> None:
            conn = _conn_var.get()
            if conn is None:
                return
            params = {
                "chat_id": msg.session_key.chat_id,
                "message_id": msg.message_id,
                "content": msg.content,
                "event": msg.event,
            }
            frame = {"jsonrpc": "2.0", "method": msg.event, "params": params}
            try:
                await conn.ws.send(json.dumps(frame))
            except Exception:
                conn.alive = False

        result_outbound = await self._executor.execute(inbound, push_fn=push_fn)
        # 主响应已由 push_fn 发送，此处返回仅供 JSON-RPC 框架使用
        return {
            "message_id": result_outbound.message_id,
            "content": result_outbound.content,
            "event": result_outbound.event,
        }

    async def _handle_wecom_message(self, req: JsonRpcRequest) -> dict:
        """处理来自企业微信插件的消息（复用 feishu.message 逻辑）。"""
        return await self._handle_message(req)

    async def _handle_notify(self, req: JsonRpcRequest) -> dict:
        """轻量通知：plugin 通知 core 更新 session（群聊非@mention消息）。

        只更新 session 统计，不触发 AI 推理。
        """
        params = req.params
        from supercc.core.protocol import SessionKey

        key = SessionKey(
            bot_id=params.get("bot_id", ""),
            project_path=params.get("project_path", ""),
            platform=params.get("platform", "feishu"),
            chat_id=params.get("chat_id", ""),
        )
        user_open_id = params.get("user_open_id", "") or ""

        try:
            if self._executor and self._executor.sessions:
                session = self._executor.sessions.get_or_create_session(key, user_open_id)
                self._executor.sessions.update_session(
                    session_id=session.session_id,
                    message_increment=1,
                    update_last_message=True,
                )
        except Exception:
            pass

        return {}

    # ── 连接管理 ──────────────────────────────────────────────────────────

    def _get_conn_by_req_id(self, req_id: Any) -> Optional[Connection]:
        """通过请求 ID 找到对应的 Connection。"""
        for conn in self._connections.values():
            # 简单实现：每个连接维护一个 pending_request_ids set
            if hasattr(conn, '_pending_ids') and req_id in conn._pending_ids:
                return conn
        return None

    async def _register_connection(self, ws: Any, plugin_id: str, platform: str) -> Connection:
        conn_id = secrets.token_hex(8)
        conn = Connection(
            ws=ws,
            plugin_id=plugin_id,
            platform=platform,
        )
        self._connections[conn_id] = conn
        logger.info(f"[WsServer] Plugin {plugin_id} connected ({conn_id})")
        return conn

    async def _unregister_connection(self, conn_id: str):
        if conn_id in self._connections:
            del self._connections[conn_id]
            logger.info(f"[WsServer] Connection {conn_id} disconnected")

    # ── 消息处理 ──────────────────────────────────────────────────────────

    async def _handle_client_message(self, conn: Connection, raw: dict):
        """处理来自插件的消息。"""
        try:
            req = JsonRpcRequest.from_dict(raw)
        except Exception:
            resp = JsonRpcResponse(
                error=JsonRpcError(code=ErrorCode.PARSE_ERROR, message="Invalid JSON-RPC")
            )
            await conn.ws.send(json.dumps(resp.to_dict()))
            return

        # 订阅 connect/info 方法走快速路径
        if req.method in ("connect", "ping"):
            resp = JsonRpcResponse(id=req.id, result={"status": "ok"})
            await conn.ws.send(json.dumps(resp.to_dict()))
            return

        # 设置 task-local 连接上下文，供 push_fn 使用
        token = _conn_var.set(conn)
        try:
            resp = await self.router.dispatch(req)
        finally:
            _conn_var.reset(token)

        if resp is not None:
            await conn.ws.send(json.dumps(resp.to_dict()))

    async def _send_event(self, conn: Connection, method: str, params: dict):
        """向插件发送 Event notification（无 id）。"""
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await conn.ws.send(json.dumps(frame))
        except Exception:
            conn.alive = False

    async def broadcast_to_subscribed(self, key: SessionKey, method: str, params: dict):
        """向所有订阅了 key 的插件发送 Event。"""
        for conn in self._connections.values():
            if key in conn.subscribed_keys:
                await self._send_event(conn, method, params)

    # ── 服务器生命周期 ────────────────────────────────────────────────────

    async def start(self):
        """启动 WebSocket 服务器。"""
        import websockets
        self._server = await websockets.serve(
            self._ws_handler,
            self.host,
            self.port,
        )
        self._running.set()
        logger.info(f"[WsServer] Listening on ws://{self.host}:{self.port}")

    async def stop(self):
        """停止 WebSocket 服务器。"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._running.clear()
        logger.info("[WsServer] Stopped")

    async def _ws_handler(self, ws: Any, path: str):
        """WebSocket 连接处理器。"""
        # 从 path 解析 plugin_id 和 platform（格式: /feishu/feishu）
        parts = path.strip("/").split("/")
        plugin_id = parts[0] if len(parts) > 0 else "unknown"
        platform = parts[1] if len(parts) > 1 else plugin_id

        conn = await self._register_connection(ws, plugin_id, platform)
        conn_id = None
        for cid, c in self._connections.items():
            if c is conn:
                conn_id = cid
                break

        try:
            async for raw_msg in ws:
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    resp = JsonRpcResponse(
                        error=JsonRpcError(code=ErrorCode.PARSE_ERROR, message="Invalid JSON")
                    )
                    await ws.send(json.dumps(resp.to_dict()))
                    continue

                await self._handle_client_message(conn, data)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[WsServer] Connection closed: {conn_id}")
        finally:
            if conn_id:
                await self._unregister_connection(conn_id)
