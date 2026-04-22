"""Feishu/Lark Open Platform client for receiving and sending messages."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _stream_to_buffer(stream) -> bytes:
    """Consume a Readable stream into a bytes buffer."""
    chunks = []

    def add_chunk(chunk):
        chunks.append(chunk)

    def done(_):
        pass

    def error(e):
        raise e

    stream.on("data", add_chunk)
    stream.on("end", done)
    stream.on("error", error)

    # Synchronous read for use with asyncio.to_thread
    result = b"".join(chunks)
    return result


def _extract_buffer_from_response(response) -> bytes:
    """Extract binary buffer from lark-oapi response.

    The Feishu SDK can return binary data in several shapes:
      - A Buffer directly
      - An ArrayBuffer
      - A response object with .data as Buffer/ArrayBuffer
      - A response object with .getReadableStream()
      - A response object with .writeFile(path)
      - An async iterable / iterator
      - A Node.js Readable stream
    """
    import io

    # Direct Buffer
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    # ArrayBuffer
    if isinstance(response, memoryview):
        return bytes(response)

    resp = response
    content_type = None
    if hasattr(resp, "headers"):
        content_type = resp.headers.get("content-type") or resp.headers.get("Content-Type")

    # Response with .data as Buffer or ArrayBuffer
    if hasattr(resp, "data"):
        data = resp.data
        if isinstance(data, bytes):
            return data
        if isinstance(data, memoryview):
            return bytes(data)
        if isinstance(data, io.BytesIO):
            return data.getvalue()
        # .data might be a readable stream
        if callable(getattr(data, "pipe", None)):
            return _stream_to_buffer(data)

    # Response with .getReadableStream()
    if callable(getattr(resp, "get_readable_stream", None)):
        try:
            stream = resp.get_readable_stream()
            return _stream_to_buffer(stream)
        except Exception:
            pass

    # Response with .getvalue() — e.g. .data.file.getvalue()
    if callable(getattr(resp, "getvalue", None)):
        try:
            return resp.getvalue()
        except Exception:
            pass

    # Node.js Readable stream (has .pipe method)
    if callable(getattr(resp, "pipe", None)):
        return _stream_to_buffer(resp)

    raise RuntimeError(
        f"[feishu] Unable to extract binary data from response: "
        f"unrecognised format (type={type(response).__name__})"
    )


@dataclass
class IncomingMessage:
    """Parsed incoming message from Feishu."""
    message_id: str
    chat_id: str
    user_open_id: str
    content: str           # processed text content
    message_type: str      # "text", "image", "file", "audio", etc.
    create_time: str
    parent_id: str = ""    # 被引用消息的 ID（用户引用/回复某条消息时）
    thread_id: str = ""    # 所在线程的 ID
    raw_content: str = ""  # 原始 JSON 字符串（用于调试和记忆增强）
    # 群聊相关字段
    is_group_chat: bool = False      # 是否群聊（来自 message.chat_type）
    chat_type: str = "p2p"         # 'p2p' | 'group'
    mention_bot: bool = False        # 机器人是否被 @CC（来自 mentions[] 数组）
    mention_ids: list[str] = field(default_factory=list)  # 所有被 @ 的用户 open_id 列表
    group_name: str = ""            # 群名称（群聊时）


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        bot_name: str = "Claude",
        data_dir: str = "",
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_name = bot_name
        self.data_dir = data_dir
        self._client = None

    def _get_client(self):
        if self._client is None:
            import lark_oapi as lark
            self._client = (
                lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
        return self._client

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send a text message to a chat. Returns message_id."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(json.dumps({"text": text}))
                .msg_type("text")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(
            client.im.v1.message.create,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Failed to send message: {response.msg}")
        return response.data.message_id

    async def get_message(self, message_id: str) -> dict | None:
        """Fetch a message by ID. Returns a plain dict or None on failure.

        The returned dict has the shape:
            {"msg_type": str, "content": str, "sender_id": str}
        """
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.GetMessageRequest.builder()
            .message_id(message_id)
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.get, request)
        if not response.success():
            logger.warning(f"get_message({message_id}) failed: {response.msg}")
            return None
        # Try .message first (newer lark-oapi), fall back to .items[0] (older or image msgs)
        msg_obj = getattr(response.data, "message", None) if response.data else None
        if msg_obj is None and response.data:
            items = getattr(response.data, "items", None)
            if items:
                msg_obj = items[0] if items else None
        if not msg_obj:
            logger.warning(f"get_message({message_id}): no message in response.data")
            return None
        return {
            "msg_type": getattr(msg_obj, "msg_type", ""),
            "content": getattr(msg_obj.body, "content", "") if msg_obj.body else "",
            "sender_id": getattr(msg_obj.sender, "id", "") if msg_obj.sender else "",
        }

    async def add_typing_reaction(self, message_id: str, emoji_type: str = "OK") -> str | None:
        """Add a typing emoji reaction to a message (Feishu typing indicator).

        Feishu has no dedicated typing REST API. The official plugin uses a
        'Typing' emoji reaction on the user's message instead.
        Silently returns None on failure — this is best-effort.

        Args:
            message_id: The message ID to react to.
            emoji_type: The emoji type. Defaults to "OK" (processing start).
                Use "DONE" for processing complete.
        """
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                lark.im.v1.CreateMessageReactionRequestBody.builder()
                .reaction_type(
                    lark.im.v1.model.emoji.Emoji.builder()
                    .emoji_type(emoji_type)
                    .build()
                )
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.to_thread(
                client.im.v1.message_reaction.create,
                request,
            )
            if response.success():
                return response.data.reaction_id
        except Exception:
            pass
        return None

    async def remove_typing_reaction(self, message_id: str, reaction_id: str) -> None:
        """Mark processing as complete by adding DONE reaction (keeps OK reaction).

        Does NOT remove the 'OK' reaction — both OK and DONE coexist when
        processing is complete. Silently ignores failures.
        """
        try:
            await self.add_typing_reaction(message_id, emoji_type="DONE")
        except Exception:
            pass

    async def download_media(self, message_id: str, file_key: str, msg_type: str = "image") -> bytes:
        """Download media (image/file) from a Feishu message."""
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(msg_type)
            .build()
        )
        try:
            response = await asyncio.to_thread(client.im.v1.message_resource.get, request)
            if not response.success():
                raise RuntimeError(f"Failed to download media: {response.msg}")
            # lark-oapi returns response.file as BytesIO — use .read()
            return response.file.read()
        except Exception as e:
            logger.error(f"download_media error: {e}")
            raise

    async def upload_image(self, image_bytes: bytes, image_type: str = "message") -> str:
        """Upload an image to Feishu and return the image_key."""
        import io
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateImageRequest.builder()
            .request_body(
                lark.im.v1.CreateImageRequestBody.builder()
                .image(io.BytesIO(image_bytes))
                .image_type(image_type)
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.to_thread(client.im.v1.image.create, request)
            if not response.success():
                raise RuntimeError(f"Failed to upload image: {response.msg}")
            logger.info(f"Uploaded image: {response.data.image_key}")
            return response.data.image_key
        except Exception as e:
            logger.error(f"upload_image error: {e}")
            raise

    async def send_image(self, chat_id: str, image_key: str) -> str:
        """Send an image message to a Feishu chat."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(json.dumps({"image_key": image_key}))
                .msg_type("image")
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.to_thread(client.im.v1.message.create, request)
            if not response.success():
                raise RuntimeError(f"Failed to send image: {response.msg}")
            logger.info(f"Sent image to {chat_id}: {response.data.message_id}")
            return response.data.message_id
        except Exception as e:
            logger.error(f"send_image error: {e}")
            raise

    async def upload_file(self, file_bytes: bytes, file_name: str, file_type: str | None) -> str:
        """Upload a file to Feishu and return the file_key."""
        import io
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateFileRequest.builder()
            .request_body(
                lark.im.v1.CreateFileRequestBody.builder()
                .file(io.BytesIO(file_bytes))
                .file_name(file_name)
                .file_type(file_type or "stream")
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.to_thread(client.im.v1.file.create, request)
            if not response.success():
                logger.error(f"upload_file raw response: {response}")
                raise RuntimeError(f"Failed to upload file: {response.msg}")
            logger.info(f"Uploaded file: {response.data.file_key} ({file_name})")
            return response.data.file_key
        except Exception as e:
            logger.error(f"upload_file error: {e}")
            raise

    async def send_file(self, chat_id: str, file_key: str, file_name: str) -> str:
        """Send a file message to a Feishu chat."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(json.dumps({"file_key": file_key, "file_name": file_name}))
                .msg_type("file")
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.to_thread(client.im.v1.message.create, request)
            if not response.success():
                raise RuntimeError(f"Failed to send file: {response.msg}")
            logger.info(f"Sent file {file_name} to {chat_id}: {response.data.message_id}")
            return response.data.message_id
        except Exception as e:
            logger.error(f"send_file error: {e}")
            raise

    async def send_interactive(self, chat_id: str, card: dict, reply_to_message_id: str) -> str:
        """Send an interactive card message, replying to a specific message."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Failed to send card: {response.msg}")
        return response.data.message_id

    async def send_text_reply(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: str,
    ) -> str:
        """Send a text message as a threaded reply to a specific message."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": text}))
                .msg_type("text")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Failed to reply: {response.msg}")
        logger.info(f"Replied to {reply_to_message_id} in chat {chat_id}: {response.data.message_id}")
        return response.data.message_id

    async def send_post_reply(
        self,
        chat_id: str,
        markdown_text: str,
        reply_to_message_id: str,
        log_reply: bool = True,
    ) -> str:
        """Send a markdown message as a threaded reply using Feishu post format.

        The text is rendered with Feishu's built-in markdown renderer (bold, code,
        tables, links, etc.) inside a rich text bubble.
        """
        import json
        import lark_oapi as lark
        client = self._get_client()
        content_payload = json.dumps({
            "zh_cn": {
                "content": [[{"tag": "md", "text": markdown_text}]]
            }
        })
        request = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .content(content_payload)
                .msg_type("post")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Failed to reply (post): {response.msg}")
        if log_reply:
            logger.info(f"Replied post to {reply_to_message_id} in chat {chat_id}: {response.data.message_id}")
        return response.data.message_id

    async def send_post(
        self,
        chat_id: str,
        markdown_text: str,
    ) -> str:
        """Send a markdown message as a new message using Feishu post format.

        Unlike send_post_reply, this does NOT require a reply_to_message_id —
        it creates a new standalone message in the chat.
        """
        import json
        import lark_oapi as lark
        client = self._get_client()
        content_payload = json.dumps({
            "zh_cn": {
                "content": [[{"tag": "md", "text": markdown_text}]]
            }
        })
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(content_payload)
                .msg_type("post")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"Failed to send post: {response.msg}")
        logger.info(f"Sent post to chat {chat_id}: {response.data.message_id}")
        return response.data.message_id

    async def send_interactive_card(
        self,
        chat_id: str,
        markdown_text: str,
    ) -> str:
        """Send a markdown message as a new Feishu Interactive Card.

        Used for content with fenced code blocks or tables that benefit from
        the wide-screen card layout. Creates a new standalone message.
        """
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": markdown_text}]
            }
        }
        return await self.send_card(chat_id, card)

    async def send_card(
        self,
        chat_id: str,
        card: dict,
    ) -> str:
        """Send an interactive card as a new standalone message (no reply_id needed)."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"Failed to send card: {response.msg}")
        logger.info(f"Sent card to chat {chat_id}: {response.data.message_id}")
        return response.data.message_id

    async def send_interactive_reply(
        self,
        chat_id: str,
        markdown_text: str,
        reply_to_message_id: str,
        log_reply: bool = True,
    ) -> str:
        """Send a markdown message as a threaded reply using Feishu Interactive Card.

        Used for content containing fenced code blocks or markdown tables — these render
        more richly inside a wide-screen card than in a post bubble.
        """
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": markdown_text}]
            }
        }
        return await self.send_interactive(chat_id, card, reply_to_message_id)

    async def send_edit_diff_card(
        self,
        chat_id: str,
        card: dict,
        reply_to_message_id: str,
        log_reply: bool = True,
    ) -> str:
        """Send a pre-built colored diff card as a threaded reply."""
        msg_id = await self.send_interactive(chat_id, card, reply_to_message_id)
        if log_reply:
            logger.info(f"Replied diff card to {reply_to_message_id} in chat {chat_id}: {msg_id}")
        return msg_id

    async def send_image_reply(
        self,
        chat_id: str,
        image_key: str,
        reply_to_message_id: str,
    ) -> str:
        """Send an image message as a threaded reply."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .content(json.dumps({"image_key": image_key}))
                .msg_type("image")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Failed to reply image: {response.msg}")
        return response.data.message_id

    async def send_file_reply(
        self,
        chat_id: str,
        file_key: str,
        file_name: str,
        reply_to_message_id: str,
    ) -> str:
        """Send a file message as a threaded reply."""
        import json
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .content(json.dumps({"file_key": file_key, "file_name": file_name}))
                .msg_type("file")
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Failed to reply file: {response.msg}")
        return response.data.message_id

    def _extract_file_info(self, content_str: str) -> tuple[str, str]:
        """Extract original filename and file_type from file message content."""
        import json
        try:
            content = json.loads(content_str)
            name = content.get("file_name", "file")
            ftype = content.get("file_type", "bin")
            return name, ftype
        except Exception:
            return "file", "bin"

    def parse_incoming_message(self, body: dict) -> IncomingMessage | None:
        """Parse webhook payload into IncomingMessage."""
        try:
            event = body.get("event", {})
            if not event:
                return None

            message = event.get("message", {})
            sender = event.get("sender", {})

            return IncomingMessage(
                message_id=message.get("message_id", ""),
                chat_id=message.get("chat_id", ""),
                user_open_id=sender.get("sender_id", {}).get("open_id", ""),
                content=self._extract_content(message),
                message_type=message.get("msg_type", "text"),
                create_time=message.get("create_time", ""),
                parent_id=message.get("parent_id", ""),
                thread_id=message.get("thread_id", ""),
            )
        except Exception as e:
            logger.error(f"Failed to parse incoming message: {e}")
            return None

    def _extract_content(self, message) -> str:
        """Extract text content from a message.

        Handles both plain dicts (from get_chat_history v0.4.2 API) and
        lark-oapi Message objects (from get_chat_history with newer API).
        """
        # Get msg_type — try dict access first, then lark-oapi attribute
        if isinstance(message, dict):
            msg_type = message.get("msg_type", "")
            content_str = message.get("content", "{}")
        else:
            # lark-oapi Message object
            msg_type = getattr(message, "msg_type", "") or ""
            body = getattr(message, "body", None)
            content_str = getattr(body, "content", "{}") if body else "{}"
        try:
            import json
            content = json.loads(content_str)
            if msg_type == "text":
                return content.get("text", "")
            elif msg_type == "post":
                return content.get("text", "")
            return str(content)
        except Exception:
            return content_str

    async def get_chat_history(
        self,
        chat_id: str,
        limit: int = 20,
        sort_type: str = "ByCreateTimeDesc",
    ) -> list[dict]:
        """Fetch recent messages from a group chat via Feishu API.

        Returns a list of message dicts with keys: message_id, chat_id, msg_type,
        content, create_time, sender.
        """
        import lark_oapi as lark
        client = self._get_client()
        request = (
            lark.im.v1.ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .page_size(limit)
            .sort_type(sort_type)
            .build()
        )
        try:
            resp = await asyncio.to_thread(client.im.v1.message.list, request)
            logger.debug(f"[GROUP_HISTORY][API] chat_id={chat_id} code={resp.code} msg={getattr(resp, 'msg', '')}")
            if not resp.success():
                logger.warning(f"get_chat_history failed: code={resp.code} msg={getattr(resp, 'msg', '')}")
                return []
            # Log raw resp.data fields for debugging
            if resp.data:
                logger.debug(f"[GROUP_HISTORY][API] resp.data fields: {[k for k in dir(resp.data) if not k.startswith('_')]}")
                logger.debug(f"[GROUP_HISTORY][API] resp.data items={getattr(resp.data, 'items', None)}")
                logger.debug(f"[GROUP_HISTORY][API] resp.data has_more={getattr(resp.data, 'has_more', None)}")
                logger.debug(f"[GROUP_HISTORY][API] resp.data page_token={getattr(resp.data, 'page_token', None)}")
            items = resp.data.items if resp.data and hasattr(resp.data, 'items') else []
            logger.debug(f"[GROUP_HISTORY][API] chat_id={chat_id} returned {len(items)} messages")
            return items
        except Exception as e:
            logger.warning(f"get_chat_history error: {e}")
            return []
