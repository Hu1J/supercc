"""Protocol type definitions for core <-> plugin communication via WebSocket + JSON-RPC 2.0."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional


class MessageRole(str, Enum):
    """消息角色：插件视角。"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageType(str, Enum):
    """消息类型。"""
    TEXT = "text"
    IMAGE = "image"      # 本地路径，Markdown 嵌入: ![alt](local_path)
    FILE = "file"        # 本地路径
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


# ── Session Key ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionKey:
    """
    Session 隔离四元组: bot_id × project_path × platform × chat_id

    所有 Worker Pool / Session 查找均用此 key。
    """
    bot_id: str                    # 机器人 ID（飞书 bot_id / 企业微信 agent_id）
    project_path: str              # 项目路径（绝对路径）
    platform: str                  # 平台: feishu / wecom / qq / wechat / ...
    chat_id: str                   # 平台会话 ID（群 ID 或 P2P 用户 ID）

    def __str__(self) -> str:
        return f"{self.bot_id}×{self.platform}×{self.chat_id}×{self.project_path}"


# ── Timezone (Beijing Time, UTC+8) ───────────────────────────────────────────

_CST = timezone(timedelta(hours=8))


def _cst_now() -> datetime:
    """北京时间（UTC+8）。"""
    return datetime.now(_CST)


def default_session_key() -> SessionKey:
    return SessionKey(bot_id="", project_path="", platform="", chat_id="")


# ── Inbound (plugin -> core) ──────────────────────────────────────────────────

@dataclass
class InboundMessage:
    """
    插件发给核心的消息。

    统一使用 Markdown 作为内容格式；图片/文件以本地路径嵌入。
    """
    event: str                     # "message" | "stream_chunk" | "tool_result" | "ping"
    session_key: SessionKey
    message_id: str                # 平台原生 message_id
    role: MessageRole
    content: str                   # Markdown 格式的文本内容
    message_type: MessageType = MessageType.TEXT
    media_path: Optional[str] = None  # 本地媒体路径（图片/文件）
    user_open_id: Optional[str] = None  # 发送者平台 open_id
    thread_id: Optional[str] = None  # 平台 thread_id（消息流）
    timestamp: datetime = field(default_factory=_cst_now)
    extra: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""  # 插件注入的系统级指令，追加到 system prompt 末尾
    group_context: str = ""  # 群聊上下文（历史、引用），非指令时注入 prompt 最前面


@dataclass
class ToolCall:
    """工具调用。"""
    tool_name: str
    tool_input: dict[str, Any]
    tool_call_id: str


@dataclass
class ToolResult:
    """工具执行结果。"""
    tool_call_id: str
    content: str                   # Markdown 格式结果
    is_error: bool = False


@dataclass
class PingEvent:
    """Ping 心跳。"""
    pass


# ── Outbound (core -> plugin) ─────────────────────────────────────────────────

@dataclass
class OutboundMessage:
    """
    核心发给插件的消息。

    插件负责将 Markdown 渲染为平台原生格式。
    """
    event: str                     # "response" | "stream_chunk" | "tool_call" | "error" | "pong"
    session_key: SessionKey
    message_id: str                # 核心生成的流转 message_id
    content: str                   # Markdown 格式内容
    message_type: MessageType = MessageType.TEXT
    media_path: Optional[str] = None  # 本地媒体路径
    thread_id: Optional[str] = None
    timestamp: datetime = field(default_factory=_cst_now)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRequest:
    """
    核心请求插件执行工具。

    插件负责将工具调用渲染为平台原生格式，执行后通过 Inbound.tool_result 传回。
    """
    event: str = "tool_call"
    session_key: SessionKey = field(default_factory=default_session_key)
    message_id: str = ""
    tool_call: ToolCall = field(default_factory=ToolCall)
    thread_id: Optional[str] = None
    timestamp: datetime = field(default_factory=_cst_now)
    extra: dict[str, Any] = field(default_factory=dict)


# ── JSON-RPC 2.0 Frames ──────────────────────────────────────────────────────

@dataclass
class JsonRpcRequest:
    """
    JSON-RPC 2.0 Request。

    method: "message" | "stream_chunk" | "tool_result" | "ping"
           | "subscribe" | "unsubscribe" | "worker_status"
    """
    jsonrpc: str = "2.0"
    id: Optional[int | str] = None
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"jsonrpc": self.jsonrpc, "method": self.method, "params": self.params}
        if self.id is not None:
            d["id"] = self.id
        # platform 提升到顶层，方便 server 做认证检查
        if "platform" in self.params:
            d["platform"] = self.params["platform"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JsonRpcRequest":
        return cls(
            jsonrpc=d.get("jsonrpc", "2.0"),
            id=d.get("id"),
            method=d.get("method", ""),
            params=d.get("params", {}),
        )


@dataclass
class JsonRpcResponse:
    """JSON-RPC 2.0 Response (success or error)."""
    jsonrpc: str = "2.0"
    id: Optional[int | str] = None
    result: Optional[Any] = None
    error: Optional["JsonRpcError"] = None

    def to_dict(self) -> dict:
        d = {"jsonrpc": self.jsonrpc}
        if self.id is not None:
            d["id"] = self.id
        if self.error is not None:
            d["error"] = self.error.to_dict()
        else:
            d["result"] = self.result
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JsonRpcResponse":
        err = None
        if "error" in d:
            err = JsonRpcError.from_dict(d["error"])
        return cls(
            jsonrpc=d.get("jsonrpc", "2.0"),
            id=d.get("id"),
            result=d.get("result"),
            error=err,
        )


@dataclass
class JsonRpcError:
    """JSON-RPC 2.0 Error object."""
    code: int
    message: str
    data: Optional[Any] = None

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JsonRpcError":
        return cls(
            code=d.get("code", -32603),
            message=d.get("message", "Internal error"),
            data=d.get("data"),
        )


# ── Command Result ─────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """
    核心执行斜杠命令后的返回结果。

    所有内容都在 content 字段（markdown 文本），Plugin 用 should_use_card() 判断如何渲染。
    event 用于平台特有操作的信号（restart、update、switch）。
    """
    content: str = ""               # 纯文本内容
    event: str = "command"          # "command" | "restart" | "update" | "switch" | ...
    extra: dict = field(default_factory=dict)  # 额外参数（如 /switch 的 target_path）


# ── Error Codes ────────────────────────────────────────────────────────────────

class ErrorCode:
    """JSON-RPC error codes."""
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # 业务错误码（核心专用）
    SESSION_NOT_FOUND = 1001
    WORKER_NOT_AVAILABLE = 1002
    PLUGIN_DISCONNECTED = 1003
    BOT_ID_MISMATCH = 1004


# ── Event names ────────────────────────────────────────────────────────────────

class Event:
    """WebSocket event name constants."""
    # Inbound (plugin -> core)
    MESSAGE = "message"
    STREAM_CHUNK = "stream_chunk"
    TOOL_RESULT = "tool_result"
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"

    # Outbound (core -> plugin)
    RESPONSE = "response"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    PONG = "pong"
    WORKER_STATUS = "worker_status"
    BACKGROUND_COMPLETE = "background_complete"  # SkillNudge/Memory Review 等后台任务完成通知
