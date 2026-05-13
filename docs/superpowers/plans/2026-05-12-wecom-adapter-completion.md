# 企业微信适配器功能补全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将企业微信适配器从"骨架实现"补全到与飞书适配器功能对齐的水平。

**Architecture:** 在 `WeComCoreWSClient` 中引入 StreamAccumulator 流式缓冲（P0），按 message_id 隔离并发多消息；逐步补充 @mention、Typing Indicator、授权卡片、后台任务（P1/P2）。

**Tech Stack:** Python asyncio, 企业微信 WebSocket长连接, 企业微信模板卡片

---

## 文件结构

```
supercc/adapter/wecom/
├── client.py          # HTTP API 客户端（已有 text/markdown/file/image）
├── core_client.py     # Thin Client（本次主要修改）
├── core_protocol.py   # 消息格式转换（补充 mention_ids 解析）
├── ws_client.py       # WebSocket 长连接（已有）
└── format/
    └── __init__.py    # 空目录（暂不需要独立 formatter）
```

---

## Task 1: StreamAccumulator 流式缓冲（P0）

**Files:**
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 添加 StreamAccumulator 类和实例变量**

在 `WeComCoreWSClient.__init__` 中添加：
```python
self._accumulator_by_msg_id: dict[str, _WeComStreamAccumulator] = {}
self._pending_message_ids: dict[str, str] = {}  # req_id → message_id
```

添加 `_WeComStreamAccumulator` 类（在 `WeComCoreWSClient` 之下作为内部类或模块级）：
```python
class _WeComStreamAccumulator:
    """缓冲流式文本，1.5s idle flush + tool_call 时立即 flush。"""
    def __init__(self, chat_id: str, message_id: str, send_fn, flush_timeout: float = 1.5):
        self.chat_id = chat_id
        self._message_id = message_id
        self._send = send_fn
        self._flush_timeout = flush_timeout
        self._buffer = ""
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None
        self.sent_something = False

    async def add_text(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            self._buffer += text
            if self._timer_task:
                self._timer_task.cancel()
            self._timer_task = asyncio.create_task(self._flush_after(self._flush_timeout))

    async def flush(self) -> None:
        async with self._lock:
            if self._timer_task:
                self._timer_task.cancel()
                self._timer_task = None
            if self._buffer:
                text = self._buffer
                self._buffer = ""
                if text.strip():
                    await self._send(self.chat_id, self._message_id, text)
                    self.sent_something = True

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._buffer:
                    text = self._buffer
                    self._buffer = ""
                    if text.strip():
                        await self._send(self.chat_id, self._message_id, text)
                        self.sent_something = True
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 2: 重写 `_handle_core_message` 处理流式和 tool_call**

替换现有 `_handle_core_message`：
```python
async def _handle_core_message(self, data: dict):
    if "id" in data:
        # Response：flush accumulator，然后唤醒 Future
        req_id = str(data.get("id"))
        msg_id = self._pending_message_ids.pop(req_id, None)
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id.pop(msg_id)
            await acc.flush()
        if req_id in self._pending_responses:
            fut = self._pending_responses.pop(req_id)
            fut.set_result(data.get("result"))
        return

    method = data.get("method", "")
    params = data.get("params", {})

    if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
        await self._render_and_send(params)
    elif method == Event.TOOL_CALL:
        await self._handle_tool_call(params)
```

- [ ] **Step 3: 重写 `_render_and_send` 使用 accumulator**

替换现有 `_render_and_send`：
```python
async def _render_and_send(self, params: dict):
    content = params.get("content", "")
    chat_id = params.get("chat_id", "")
    message_id = params.get("message_id", "")

    if not content:
        return

    if message_id:
        if message_id not in self._accumulator_by_msg_id:
            self._accumulator_by_msg_id[message_id] = _WeComStreamAccumulator(
                chat_id=chat_id,
                message_id=message_id,
                send_fn=lambda cid, mid, text: self._do_send_markdown(cid, text),
                flush_timeout=1.5,
            )
        await self._accumulator_by_msg_id[message_id].add_text(content)
    else:
        # 无 message_id 时直接发送（兼容无流式 ID 的场景）
        await self._do_send_markdown(chat_id, content)
```

- [ ] **Step 4: 添加 `_do_send_markdown` 和 `_handle_tool_call`**

在 `_render_and_send` 之后添加：
```python
async def _do_send_markdown(self, chat_id: str, text: str) -> None:
    """实际发送 Markdown 到企业微信。"""
    try:
        await self.wecom.send_markdown(chat_id, text)
    except Exception as e:
        logger.warning(f"[WeComCore] send_markdown failed: {e}")

async def _handle_tool_call(self, params: dict):
    """tool_call 事件：先 flush accumulator，再处理工具调用。"""
    tool_name = params.get("tool_name", "")
    tool_input = params.get("tool_input", {})
    tool_call_id = params.get("tool_call_id", "")
    chat_id = params.get("chat_id", "")
    message_id = params.get("message_id", "")

    # 先 flush 当前 message_id 的 accumulator
    if message_id and message_id in self._accumulator_by_msg_id:
        await self._accumulator_by_msg_id[message_id].flush()

    # 构建工具结果（WeCom 平台不执行实际工具，只是展示）
    result_content = f"[{tool_name}] 执行完成"

    await self._send_event(Event.TOOL_RESULT, {
        "tool_call_id": tool_call_id,
        "content": result_content,
        "chat_id": chat_id,
    })
```

- [ ] **Step 5: 更新 `send_message` 注册 message_id 映射**

在 `send_message` 的 `self._pending_responses[str(req.id)] = future` 之后添加：
```python
self._pending_message_ids[str(req.id)] = inbound.message_id
```

- [ ] **Step 6: Commit**

```bash
git add supercc/adapter/wecom/core_client.py
git commit -m "feat(wecom): P0 — 添加 StreamAccumulator 流式缓冲和 tool_call 处理"
```

---

## Task 2: @mention 支持（P1）

**Files:**
- Modify: `supercc/adapter/wecom/core_protocol.py`
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 解析 WeCom 入站消息的 mention_ids**

WeCom 入站消息格式（群聊 @mention）：
```json
{
  "msgtype": "text",
  "text": { "content": "@_user_1 你好" },
  "mentioned_list": ["userid1", "userid2"]
}
```

更新 `incoming_to_inbound` 中的 `extra` 字段：
```python
extra={
    "raw": str(msg),
    "is_group_chat": msg.get("chattype") == "group",
    "mention_bot": self._check_mention_bot(msg),  # 见下方
    "mention_ids": msg.get("mentioned_list", []),
    "group_name": "",
    "chat_type": msg.get("chattype", "single"),
},
```

添加辅助方法（在 `incoming_to_inbound` 之外作为模块级函数）：
```python
def _check_mention_bot(msg: dict) -> bool:
    """检测消息是否 @ 了机器人。"""
    mentioned_list = msg.get("mentioned_list", [])
    bot_id = msg.get("aibotid", "")
    return bot_id in mentioned_list
```

- [ ] **Step 2: Commit**

```bash
git add supercc/adapter/wecom/core_protocol.py
git commit -m "feat(wecom): P1 — 解析 mention_ids 和 mention_bot 标志"
```

---

## Task 3: Typing Indicator（P1）

**Files:**
- Modify: `supercc/adapter/wecom/client.py`
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 在 WeComClient 添加"正在输入"文本消息**

WeCom 没有专门的 typing API，通过发送临时文本来模拟：

```python
async def send_typing_indicator(self, chat_id: str) -> str:
    """发送'正在输入...'提示（WeCom 模板卡片实现）。"""
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
```

- [ ] **Step 2: 在 WeComCoreWSClient 中集成 typing indicator**

在 `send_message` 发送请求前调用 `send_typing_indicator`：
```python
async def send_message(self, msg: dict) -> dict:
    # 发送前显示 typing 提示
    try:
        await self.wecom.send_typing_indicator(msg.get("chatid", ""))
    except Exception:
        pass  # typing indicator 失败不影响主流程

    # ... 现有逻辑不变 ...
```

- [ ] **Step 3: Commit**

```bash
git add supercc/adapter/wecom/client.py supercc/adapter/wecom/core_client.py
git commit -m "feat(wecom): P1 — 添加 typing indicator 临时文本提示"
```

---

## Task 4: 授权卡片（P1）

**Files:**
- Modify: `supercc/adapter/wecom/client.py`

- [ ] **Step 1: 添加授权卡片发送方法**

```python
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
                        "action_type": "url",
                        "weapp_remark": "请联系管理员授权后重试",
                    }
                ]
            },
        },
    }
    data = await _call_api("POST", url, params, body)
    if data.get("errcode") != 0:
        raise RuntimeError(f"WeCom authorization card failed: {data}")
    return data.get("msgid", "")
```

- [ ] **Step 2: Commit**

```bash
git add supercc/adapter/wecom/client.py
git commit -m "feat(wecom): P1 — 添加授权引导卡片"
```

---

## Task 5: 幂等性（message_id 去重）（P2）

**Files:**
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 添加已发送 message_id 集合**

在 `WeComCoreWSClient.__init__` 中添加：
```python
self._sent_message_ids: set[str] = set()  # 幂等性
```

- [ ] **Step 2: 在 `_do_send_markdown` 中添加去重检查**

```python
async def _do_send_markdown(self, chat_id: str, message_id: str, text: str) -> None:
    """实际发送 Markdown 到企业微信（带幂等性）。"""
    if message_id in self._sent_message_ids:
        logger.info(f"[WeComCore] message {message_id} already sent, skipping")
        return
    try:
        await self.wecom.send_markdown(chat_id, text)
        self._sent_message_ids.add(message_id)
    except Exception as e:
        logger.warning(f"[WeComCore] send_markdown failed: {e}")
```

更新 `_render_and_send` 中的 lambda 以传递 message_id：
```python
self._accumulator_by_msg_id[message_id] = _WeComStreamAccumulator(
    chat_id=chat_id,
    message_id=message_id,
    send_fn=lambda cid, mid, text: self._do_send_markdown(cid, mid, text),  # 传递 mid
    flush_timeout=1.5,
)
```

- [ ] **Step 3: Commit**

```bash
git add supercc/adapter/wecom/core_client.py
git commit -m "feat(wecom): P2 — 添加 message_id 幂等性去重"
```

---

## Task 6: SkillNudge 和 Memory Review 通知（P2）

**Files:**
- Modify: `supercc/adapter/wecom/core_client.py`

- [ ] **Step 1: 在 WeComCoreWSClient 中添加后台任务触发方法**

在 `_handle_core_message` 的 `Event.RESPONSE` 处理中，当检测到工具调用次数超过阈值时触发：

```python
async def _trigger_background_tasks(self, chat_id: str, message_id: str, tool_count: int, response_content: str):
    """触发后台任务（SkillNudge、Memory Review）。"""
    if tool_count > 0:
        asyncio.create_task(self._do_skill_nudge(chat_id, tool_count))
    asyncio.create_task(self._do_memory_review(chat_id, response_content))

async def _do_skill_nudge(self, chat_id: str, tool_count: int):
    """发送技能推荐通知。"""
    try:
        content = f"🧰 你在本次对话中使用了 {tool_count} 个工具调用。想了解相关技能吗？"
        await self.wecom.send_text(chat_id, content)
    except Exception as e:
        logger.warning(f"[WeComCore] skill_nudge failed: {e}")

async def _do_memory_review(self, chat_id: str, response_content: str):
    """发送记忆回顾提示。"""
    try:
        # 简单的记忆回顾提示（实际内容由 memory_manager 决定）
        content = "📝 对话结束。你想保存这次重要的信息到记忆吗？"
        await self.wecom.send_text(chat_id, content)
    except Exception as e:
        logger.warning(f"[WeComCore] memory_review failed: {e}")
```

- [ ] **Step 2: Commit**

```bash
git add supercc/adapter/wecom/core_client.py
git commit -m "feat(wecom): P2 — 添加 SkillNudge 和 Memory Review 后台任务触发"
```

---

## Task 7: Group Context 注入（P2）

**Files:**
- Modify: `supercc/adapter/wecom/core_protocol.py`

- [ ] **Step 1: 补充群聊上下文信息**

WeCom 群聊消息可能包含 `group_chat` 字段（群名等）。更新 `incoming_to_inbound` 的 extra：

```python
extra={
    "raw": str(msg),
    "is_group_chat": msg.get("chattype") == "group",
    "mention_bot": _check_mention_bot(msg),
    "mention_ids": msg.get("mentioned_list", []),
    "group_name": msg.get("group_chat", {}).get("name", ""),
    "chat_type": msg.get("chattype", "single"),
    # 企业微信特有字段
    "room_id": msg.get("roomid", ""),
    "user_id": msg.get("from", {}).get("userid", ""),
},
```

- [ ] **Step 2: Commit**

```bash
git add supercc/adapter/wecom/core_protocol.py
git commit -m "feat(wecom): P2 — 补充群聊上下文（group_name, room_id, user_id）"
```

---

## Task 8: 更新 wecom-adapter-gap-analysis skill

**Files:**
- Modify: `.supercc/skills/wecom-adapter-gap-analysis/SKILL.md`

- [ ] **Step 1: 更新 Skill 标记已实现的功能**

在功能缺失清单中，将已完成的 P0/P1/P2 标记为 ✅：
- StreamAccumulator → ✅
- @mention 支持 → ✅
- Typing Indicator → ✅
- 授权卡片 → ✅
- 幂等性 → ✅
- SkillNudge/Memory Review → ✅

添加"已实现"章节记录实现要点。

```bash
git add .supercc/skills/wecom-adapter-gap-analysis/SKILL.md
git commit -m "docs: 更新 wecom-adapter-gap-analysis skill，标记已实现功能"
```
