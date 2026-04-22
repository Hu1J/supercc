# cc-feishu-bridge 队列、回复链、引用检测、音频支持

**日期**: 2026-04-02
**状态**: 设计中
**目标**: 对标官方飞书插件体验

---

## 1. 全局消息队列

### 问题
当前 bridge 在 Claude 正在处理消息时，用户新发的消息只提示"正在回复中"然后丢弃，导致排队消息永远得不到处理。

### 目标
全局串行队列：所有用户的所有消息进入统一队列，先进先出依次处理，和官方插件行为一致。

### 设计

在 `MessageHandler` 中新增 `asyncio.Queue` 作为消息队列，替代当前的 `_active_task` 单一任务机制：

```python
class MessageHandler:
    def __init__(self, ...):
        ...
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    async def handle(self, message: IncomingMessage) -> HandlerResult:
        """将消息入队，立即返回。"""
        await self._queue.put(message)
        # 确保 Worker 在运行
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
        return HandlerResult(success=True)

    async def _worker_loop(self) -> None:
        """串行出队并处理消息。"""
        while True:
            try:
                message = await self._queue.get()
                try:
                    await self._process_message(message)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker loop error")

    async def _process_message(self, message: IncomingMessage) -> None:
        """处理单条消息（鉴权、命令、查询）。"""
        ...
```

### 关键行为

- `handle()` 立即返回，不等待处理完成
- Worker 循环串行出队，保证全局有序
- 用户在 Claude 忙碌时发的消息进入队列，不丢弃，不提示"正在回复中"
- 队列无上限（`maxsize=0`），避免因队列满导致丢消息
- `/stop` 命令只中断当前正在执行的任务，不清空队列

### 重入性移除

原来的 re-entrant 检查（`_active_task` / `_active_user_id`）不再需要，因为队列天然串行化。相关代码删除。

---

## 2. 飞书回复链（reply_to_message_id）

### 问题
当前 bridge 发送消息用 `im.v1.message.create`，AI 的回复和用户消息没有视觉关联。

### 目标
让 AI 的回复作为用户消息的 Threaded Reply，视觉上形成对话流。

### 设计

在 `FeishuClient` 中新增 `send_text_reply()` 方法：

```python
async def send_text_reply(
    self,
    chat_id: str,
    text: str,
    reply_to_message_id: str,
) -> str:
    """发送文本消息作为对某条消息的回复。"""
    import json
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
    return response.data.message_id
```

同样为 `send_image_reply()`、`send_file_reply()`、`send_interactive_reply()` 添加对应方法。

`MessageHandler` 中的 `send_text` 调用全部替换为 `send_text_reply`，传入用户的 `message_id` 作为 `reply_to_message_id`。

### 保留 `send_text` 的原因

`send_text`（非回复模式）仍保留，用于某些不需要回复链的场景（如系统通知、内部日志推送），接口不变。

---

## 3. 消息引用（quote）检测

### 问题
用户引用（回复）某条消息后发送，AI 无法感知被引用的内容。

### 飞书数据模型

用户在飞书客户端"引用"一条消息发送时，Webhook 事件 payload 中 `message` 对象包含：

```json
{
  "message_id": "om_xxx_new",
  "parent_id": "om_xxx_original",
  "root_id": "om_xxx_root",
  "thread_id": "om_xxx_thread",
  "chat_id": "oc_xxx",
  "msg_type": "text",
  "content": "{\"text\":\"新的消息内容\"}"
}
```

- `parent_id`: 被直接回复的那条消息的 ID（即引用目标）
- `root_id`: 所在线程的根消息 ID
- `thread_id`: 线程 ID

### 设计

**Step 1: 解析 `parent_id`**

在 `IncomingMessage` 中新增字段，并在 `parse_incoming_message` 中提取：

```python
@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    user_open_id: str
    content: str
    message_type: str
    create_time: str
    parent_id: str = ""       # 被引用的消息 ID（用户引用发送时）
    thread_id: str = ""
```

**Step 2: 获取被引用消息内容**

新增 `FeishuClient.get_message()` 方法：

```python
async def get_message(self, message_id: str) -> dict | None:
    """获取指定 message_id 的消息详情。"""
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.GetMessageRequest.builder()
        .message_id(message_id)
        .build()
    )
    try:
        response = await asyncio.to_thread(client.im.v1.message.get, request)
        if response.success():
            return response.data.items[0] if response.data.items else None
    except Exception:
        pass
    return None
```

**Step 3: 在 `_process_message` 中拼接引用内容**

```python
quoted_content = ""
if message.parent_id:
    quoted_msg = await self.feishu.get_message(message.parent_id)
    if quoted_msg:
        sender_name = quoted_msg.get("sender", {}).get("name", "")
        quoted_text = self._extract_content(quoted_msg)
        if sender_name:
            quoted_content = f"[引用消息: {message.parent_id}] {sender_name}: {quoted_text}"
        else:
            quoted_content = f"[引用消息: {message.parent_id}] {quoted_text}"

full_prompt = (
    f"{quoted_content}\n{message.content}"
    if quoted_content
    else message.content
)
```

格式与官方插件一致：`[引用消息: message_id] senderName: content` 或无发送者时 `[引用消息: message_id] content`。

**Step 4: 错误处理**

如果获取引用消息失败（如消息已删除），静默降级，只发用户实际发送的内容，不报错，不影响流程。

---

## 4. 音频消息支持

### 问题
当前 bridge 拒绝 audio 类型消息。

### 飞书数据模型

audio 类型消息的 `content` 格式为：

```json
{"file_key": "xxx", "duration": 12345}
```

- `file_key`: 音频资源标识（Opus/M4A 格式）
- `duration`: 时长（毫秒）

### 设计

**Step 1: 允许 audio 类型**

在 `_process_message` 的消息类型检查中，将 `"audio"` 加入允许列表：

```python
if message.message_type not in ("text", "image", "file", "audio"):
    return HandlerResult(success=True, response_text="暂不支持该消息类型，请发送文字消息。")
```

**Step 2: 解析 audio content**

```python
elif message.message_type == "audio":
    try:
        content = json.loads(message.content)
        file_key = content.get("file_key", "")
        duration_ms = content.get("duration", 0)
        if file_key:
            data = await self.feishu.download_media(
                message.message_id, file_key, msg_type="audio"
            )
            import os
            audio_dir = os.path.join(self.data_dir, "received_audio")
            os.makedirs(audio_dir, exist_ok=True)
            save_path = os.path.join(audio_dir, f"{message.message_id}.opus")
            with open(save_path, "wb") as f:
                f.write(data)
            duration_s = duration_ms / 1000 if duration_ms else None
            duration_str = f" ({duration_s:.1f}s)" if duration_s else ""
            media_prompt_prefix = f"[Audio: {save_path}{duration_str}]"
    except Exception as e:
        logger.warning(f"Failed to process audio: {e}")
        media_prompt_prefix = ""
```

**Step 3: 和 text 消息一样处理引用和 prompt 拼接**

音频消息和文本消息走同一套 `_process_message` 流程，引用检测同样生效。

**Step 4: 保存路径**

音频文件保存到 `{data_dir}/received_audio/` 目录，和图片/文件目录结构一致。

---

## 文件变更摘要

| 文件 | 变更 |
|------|------|
| `cc_feishu_bridge/feishu/message_handler.py` | 队列重写；`_process_message` 替代原 `handle`；新增引用拼接；audio 支持 |
| `cc_feishu_bridge/feishu/client.py` | 新增 `send_*_reply` 系列方法；`get_message()` 方法；`IncomingMessage` 新增 `parent_id`、`thread_id` 字段；`_extract_content` 提取为公共方法 |

---

## 测试要点

1. 快速连续发两条消息，验证队列顺序正确
2. 引用消息发送，验证 prompt 包含引用内容
3. 发送音频消息，验证文件保存和 prompt 格式正确
4. 验证 AI 回复挂到用户消息下方（Threaded Reply）
5. `/stop` 不清空队列，只中断当前任务
