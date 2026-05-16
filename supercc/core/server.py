"""WebSocket Server with JSON-RPC 2.0 routing for core <-> plugin communication."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import secrets
import threading
import traceback
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
            logger.error("[Router] Handler error for %s\n%s", req.method, traceback.format_exc())
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
        self._restart_lock = threading.Lock()  # 防止并发 restart/update
        self._plugin_authenticated: dict[str, bool] = {}  # platform -> authenticated

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
        conn = _conn_var.get()
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
        conn = _conn_var.get()
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
        pool = self._executor.pool if self._executor else None
        if pool:
            return await pool.stats()
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
            system_prompt=params.get("system_prompt", ""),
            group_context=params.get("group_context", ""),
            extra={
                "raw": params.get("raw", ""),
                "is_group_chat": params.get("is_group_chat", False),
                "mention_bot": params.get("mention_bot", False),
                "mention_ids": params.get("mention_ids", []),
                "group_name": params.get("group_name", ""),
                "chat_type": params.get("chat_type", "p2p"),
                **params.get("extra", {}),  # 合并 platform 特有字段（group_members等）
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
            # TOOL_CALL 事件需要 extra（tool_name、tool_input）传给 plugin
            if msg.extra is not None:
                params["extra"] = msg.extra
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
        # Auth 消息处理
        msg_type = raw.get("type") or raw.get("method")
        if msg_type == "auth":
            token = raw.get("token", "")
            username = raw.get("username", "")
            password = raw.get("password", "")

            from supercc.config import get_config
            cfg = get_config()
            auth_ok = False
            if token and secrets.compare_digest(token, cfg.core.token):
                auth_ok = True
            elif username and password:
                if secrets.compare_digest(username, cfg.core.username) and secrets.compare_digest(password, cfg.core.password):
                    auth_ok = True

            if auth_ok:
                platform = raw.get("platform", "unknown")
                self._plugin_authenticated[platform] = True
                await conn.ws.send(json.dumps({"type": "auth_ok"}))
            else:
                await conn.ws.send(json.dumps({"type": "auth_failed"}))
                await conn.ws.close()
            return

        # 非 auth 消息检查是否已认证
        # 两种消息格式：
        # 1. 旧格式: {"type": "auth", "token": ..., "platform": "feishu"}
        # 2. JSON-RPC 格式: {"jsonrpc": "2.0", "id": ..., "method": "feishu.message", "params": {...}}
        # auth 消息在 platform 字段认证，JSON-RPC 请求通过 method 路由，不需要检查 platform
        if raw.get("type") == "auth":
            platform = raw.get("platform", "unknown")
            if platform not in self._plugin_authenticated:
                await conn.ws.send(json.dumps({"type": "error", "message": "not authenticated"}))
                return

        try:
            req = JsonRpcRequest.from_dict(raw)
        except Exception:
            resp = JsonRpcResponse(
                error=JsonRpcError(code=ErrorCode.PARSE_ERROR, message="Invalid JSON-RPC")
            )
            try:
                await conn.ws.send(json.dumps(resp.to_dict()))
            except Exception:
                pass
            return

        # 订阅 connect 方法走快速路径（ping 走注册的处理程序 core.ping）
        if req.method == "connect" and req.method not in self.router._handlers:
            resp = JsonRpcResponse(id=req.id, result={"status": "ok"})
            try:
                await conn.ws.send(json.dumps(resp.to_dict()))
            except Exception:
                pass
            return

        # 设置 task-local 连接上下文，供 push_fn 使用
        token = _conn_var.set(conn)
        try:
            resp = await self.router.dispatch(req)
        except Exception:
            logger.error("[WsServer] error in dispatch(%s):\n%s", req.method, traceback.format_exc())
            resp = JsonRpcResponse(
                error=JsonRpcError(code=ErrorCode.INTERNAL_ERROR, message="Internal error")
            )
        finally:
            _conn_var.reset(token)

        if resp is not None:
            try:
                await conn.ws.send(json.dumps(resp.to_dict()))
            except Exception:
                logger.warning("[WsServer] failed to send response, connection may be dead")

            # 响应发出后，检查是否需要 restart/update/switch
            event: str = ""
            if resp.result is not None and isinstance(resp.result, dict):
                event = resp.result.get("event", "")
            if event in ("restart", "update"):
                # 防止并发
                if not self._restart_lock.acquire(blocking=False):
                    logger.warning("[WsServer] restart already in progress, skipping")
                    return

                project_path = req.params.get("project_path", "")
                extra = resp.result.get("extra", {}) if resp.result else {}
                target_path = extra.get("target_path", "") or project_path

                # restart/update 用 os.execvp 原地替换进程
                from supercc.core.commands.restart_impl import _cleanup_and_replace
                _cleanup_and_replace(event, target_path)
                # 以下代码永不执行
                return

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

        # websockets 15.x: handler receives (connection,) only, no path param.
        # Path is captured via process_request and looked up in _conn_path.
        self._conn_path: dict[int, str] = {}

        async def _ws_handler(connection: Any):
            """WebSocket 连接处理器（15.x 签名，无 path 参数）。"""
            path = self._conn_path.pop(id(connection), "/unknown/unknown")
            parts = path.strip("/").split("/")
            plugin_id = parts[0] if len(parts) > 0 else "unknown"
            platform = parts[1] if len(parts) > 1 else plugin_id

            conn = await self._register_connection(connection, plugin_id, platform)
            conn_id = None
            for cid, c in self._connections.items():
                if c is conn:
                    conn_id = cid
                    break

            try:
                async for raw_msg in connection:
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        resp = JsonRpcResponse(
                            error=JsonRpcError(code=ErrorCode.PARSE_ERROR, message="Invalid JSON")
                        )
                        await connection.send(json.dumps(resp.to_dict()))
                        continue

                    asyncio.create_task(self._handle_client_message(conn, data))

            except websockets.exceptions.ConnectionClosed:
                logger.info(f"[WsServer] Connection closed: {conn_id}")
            finally:
                if conn_id:
                    await self._unregister_connection(conn_id)

        async def _process_request(connection: Any, request: Any) -> Any | None:
            """Capture URI path from HTTP upgrade request (15.x 兼容)。"""
            # request.path is a str attribute of the Request dataclass
            self._conn_path[id(connection)] = getattr(request, "path", "/unknown/unknown")
            return None

        self._server = await websockets.serve(
            _ws_handler,
            self.host,
            self.port,
            reuse_address=True,
            process_request=_process_request,
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

                asyncio.create_task(self._handle_client_message(conn, data))

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[WsServer] Connection closed: {conn_id}")
        finally:
            if conn_id:
                await self._unregister_connection(conn_id)
