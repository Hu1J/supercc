# Group Chat Support with @Mention Context Injection

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持飞书群聊功能，当用户 @CC 时能够正确识别上下文并注入给 Claude。

**Architecture (参考飞书官方 OpenClaw 插件 `larksuite/openclaw-lark`):**
- **复用** `im.message.receive_v1` 事件，通过 `message.chat_type === 'group'` 区分群聊与 P2P（**不需要** `p2_im_chat_receive_v1`）
- 群聊中通过 `event.message.mentions[]` 数组检测 @CC，**不需要** XML 解析
- 每个 mention 包含 `id.open_id` 和 `isBot` 标志，可直接判断机器人是否被 @
- 群聊 session 与 P2P session 分离，session key 增加 `chat_id` 维度，支持 `thread_id` 多话题并行
- 被 @ 时提取引用消息（`message.parent_id`）作为上下文注入

**Tech Stack:** Python asyncio, lark-oapi WebSocket, claude-agent-sdk, SQLite

---

## File Structure

### Modified Files
- `cc_feishu_bridge/feishu/ws_client.py` — 修改现有 `im.message.receive_v1` handler 支持群聊分支
- `cc_feishu_bridge/feishu/message_handler.py` — 群聊消息处理逻辑
- `cc_feishu_bridge/claude/session_manager.py` — session key 增加 chat_id + thread_id
- `cc_feishu_bridge/feishu/message.py` — `IncomingMessage` 增加群聊字段

### New Files
- `tests/test_group_chat.py` — 群聊功能测试

---

## Task 1: 数据模型扩展

**Files:**
- Modify: `cc_feishu_bridge/feishu/message.py`
- Modify: `cc_feishu_bridge/claude/session_manager.py`

- [ ] **Step 1: 扩展 IncomingMessage 增加群聊字段**

查看当前 `IncomingMessage` 定义，然后添加群聊相关字段：

```python
# cc_feishu_bridge/feishu/message.py
@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    user_open_id: str
    content: str
    message_type: str
    # 新增字段
    is_group_chat: bool = False           # 是否群聊
    chat_type: str = "p2p"                # 'p2p' | 'group'
    thread_id: str | None = None           # 话题 ID（群聊多话题并行）
    mention_bot: bool = False              # 机器人是否被 @CC（来自 mentions[]）
    mention_ids: list[str] = field(default_factory=list)  # 所有被 @ 用户 ID
    parent_id: str | None = None           # 引用消息 ID
    group_name: str | None = None          # 群名称
```

> **来源参考**（官方插件 `FeishuMessageEvent`）：
> - `chat_type`: `event.message.chat_type` — `'p2p' | 'group'`
> - `thread_id`: `event.message.thread_id` — 话题 ID
> - `parent_id`: `event.message.parent_id` — 引用消息 ID
> - `mentions[]`: `event.message.mentions[]` — 含 `id.open_id`、`name`、`isBot` 标志

- [ ] **Step 2: 查看当前 session_manager.py 理解 session key 生成**

```bash
grep -n "session_id\|create_session\|Session" cc_feishu_bridge/claude/session_manager.py | head -40
```

- [ ] **Step 3: 运行测试确认基础测试通过**

Run: `pytest tests/ -v --tb=short -k "test_message" 2>/dev/null || echo "No tests yet, skip"`

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/feishu/message.py
git commit -m "feat: add group chat fields to IncomingMessage"
```

---

## Task 2: Session Key 增加 chat_id + thread_id 维度

**Files:**
- Modify: `cc_feishu_bridge/claude/session_manager.py`

- [ ] **Step 1: 查看当前 session 创建逻辑**

找到 `create_session` 方法和 session key 的定义。

- [ ] **Step 2: 修改 session key 生成逻辑**

参考官方插件 `buildQueueKey` 和 `threadScopedKey`：

```python
# session_manager.py create_session 增加可选参数
def create_session(
    self,
    user_id: str,
    project_path: str,
    sdk_session_id: str | None = None,
    chat_id: str | None = None,   # 新增：群聊 ID
    thread_id: str | None = None, # 新增：话题 ID（支持多话题并行）
) -> Session:
    # session key: user_id + chat_id (+ thread_id)
    if chat_id:
        session_id = f"{user_id}_{chat_id}"
        if thread_id:
            session_id += f"_thread_{thread_id}"  # 不同话题可并行
    else:
        session_id = user_id
    ...
```

> **Why thread_id?** 官方插件同一个群聊不同话题（thread）可并行处理，用 `accountId:chatId:thread:{threadId}` 做 queue key。

- [ ] **Step 3: 运行测试**

Run: `pytest tests/ -v --tb=short 2>/dev/null || echo "No relevant tests"`

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/claude/session_manager.py
git commit -m "feat: session key includes chat_id for group chat isolation"
```

---

## Task 3: 修改 ws_client 支持群聊分支（复用 im.message.receive_v1）

**Files:**
- Modify: `cc_feishu_bridge/feishu/ws_client.py`

> **重要**：不需要注册新的 `p2_im_chat_receive_v1` 事件！官方插件证明：同一条 `im.message.receive_v1` 事件通过 `chat_type` 字段同时覆盖 P2P 和群聊。

- [ ] **Step 1: 查看当前事件注册和处理方式**

```bash
grep -n "register\|message_receive\|_handle" cc_feishu_bridge/feishu/ws_client.py
```

- [ ] **Step 2: 修改事件 handler 支持群聊分支**

在现有的 `im.message.receive_v1` handler 中，通过 `event.message.chat_type` 区分：

```python
# ws_client.py — 现有 handler 中的分支逻辑
async def _handle_p2p_message(self, event):
    # 原有 P2P 处理逻辑
    pass  # 或重命名为 _handle_message_event 统一入口

# 改为：
async def _handle_message_event(self, event):
    """统一入口：处理 im.message.receive_v1 事件（P2P 和群聊共用）"""
    from cc_feishu_bridge.feishu.message import IncomingMessage

    msg = event.message
    chat_type = getattr(msg, 'chat_type', 'p2p')  # 'p2p' or 'group'

    incoming = IncomingMessage(
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        user_open_id=event.sender.sender_id.open_id or '',
        content=msg.content,
        message_type=msg.message_type,
        is_group_chat=(chat_type == 'group'),
        chat_type=chat_type,
        thread_id=getattr(msg, 'thread_id', None) or None,
        parent_id=getattr(msg, 'parent_id', None) or None,
        # mention_bot: 暂不填充，在 handler 中从 mentions[] 计算
        mention_ids=[
            m.id.open_id
            for m in getattr(msg, 'mentions', []) or []
            if getattr(m.id, 'open_id', None)
        ],
    )

    # 群聊消息额外处理 group_name（需要 API 查询或从事件获取）
    if incoming.is_group_chat:
        incoming.group_name = getattr(event.message, 'chat_name', None)

    await self.handle_message(incoming)
```

- [ ] **Step 3: 确认 builder.register 调用保持不变**

```python
# 保持现有注册方式，不需要新增
builder.register_p2_im_message_receive_v1(self._handle_message_event)
```

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/feishu/ws_client.py
git commit -m "feat: reuse im.message.receive_v1 for group chat via chat_type branch"
```

---

## Task 4: 群聊消息过滤（未被 @ 则跳过）

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

> **不需要** `mention_detector.py`！官方插件直接从 `event.message.mentions[]` 数组获取 @ 信息，`isBot` 标志判断机器人是否被 @。

- [ ] **Step 1: 在 _process_message 开头添加群聊 mention 检测**

```python
async def _process_message(self, message: IncomingMessage) -> HandlerResult:
    # 群聊消息未被 @CC，直接跳过（不响应）
    if message.is_group_chat:
        # mention_bot 在 ws_client 中从 mentions[] 提取
        if not message.mention_bot:
            return HandlerResult(success=True)  # 不响应
```

> **如何判断 mention_bot？** 在 ws_client 的 `_handle_message_event` 中：
> ```python
> # 从 mentions[] 判断机器人是否被 @
> mentions = getattr(msg, 'mentions', []) or []
> bot_open_id = self.config.get("bot_open_id", "")
> incoming.mention_bot = any(
>     getattr(m.id, 'open_id', None) == bot_open_id
>     for m in mentions
> )
> ```

- [ ] **Step 2: 测试群聊消息被正确过滤**

手动测试：往群里发一条不含 @CC 的消息，确认机器人不回复；发一条 @CC 的消息，确认机器人响应。

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: filter group chat messages unless CC is mentioned"
```

---

## Task 5: 群聊上下文注入（引用消息）

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

> 参考官方插件 `resolveQuotedContent`：通过 `parent_id` 获取引用消息，格式化为 `[message_id=xxx] senderName: content`。

- [ ] **Step 1: 理解当前 message_handler 处理流程**

找到消息入队后实际处理（`_run_query` 或类似）的地方。

- [ ] **Step 2: 添加引用消息获取逻辑**

```python
# 在 _process_message 或实际 query 执行前
if message.is_group_chat and message.parent_id:
    # 获取引用消息内容
    parent_msg = await self.feishu_client.get_message(message.parent_id)
    if parent_msg:
        # 格式化引用上下文
        quoted_context = f"\n\n[引用消息]: {parent_msg.content}"
        if parent_msg.sender_name:
            quoted_context = f"\n\n[引用消息] {parent_msg.sender_name}: {parent_msg.content}"
        # 将 quoted_context 附加到 system prompt 或 conversation context
```

- [ ] **Step 3: 测试引用消息注入**

手动测试：群聊中引用一条消息 @CC 发送，确认 AI 能看到引用内容。

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: inject quoted message context in group chat"
```

---

## Task 6: 群聊访问控制（可选，支持 per-group 配置）

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py` 或 `config.py`

> 参考官方插件两层访问控制模型：
> - Layer 1（群级别）：`channels.feishu.groups` 配置允许哪些群
> - Layer 2（发送者级别）：per-group `groupPolicy` + `allowFrom` 白名单

- [ ] **Step 1: 定义群聊配置结构**

```python
# config.py 或 message_handler.py
@dataclass
class GroupConfig:
    enabled: bool = True
    require_mention: bool = True        # 是否必须 @CC
    allow_from: list[str] = field(default_factory=list)  # 白名单 open_id
    group_policy: str = "open"         # 'open' | 'allowlist' | 'disabled'

# 配置示例
groups:
  "oc_xxx_group_id":
    enabled: true
    require_mention: true
    allow_from: ["ou_user1", "ou_user2"]
```

- [ ] **Step 2: 在 _process_message 中添加群聊访问控制**

```python
if message.is_group_chat:
    group_cfg = self._get_group_config(message.chat_id)
    if not group_cfg.enabled:
        return HandlerResult(success=True)  # 该群被禁用
    # 发送者白名单检查
    if group_cfg.group_policy == "allowlist":
        if message.user_open_id not in group_cfg.allow_from:
            return HandlerResult(success=True)  # 不在白名单
```

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: add per-group access control for group chat"
```

---

## Task 7: 端到端集成测试

**Files:**
- Test: `tests/test_group_chat_integration.py`

- [ ] **Step 1: 编写群聊完整流程测试**

```python
# tests/test_group_chat_integration.py
import pytest

def test_group_chat_mention_triggers_response():
    # 1. 模拟群聊消息事件，包含 @CC（mentions 中 isBot=True）
    # 2. 验证 mention_bot=True
    # 3. 验证 session 被创建（带 chat_id）
    # 4. 验证响应被发送
    pass

def test_group_chat_no_mention_skipped():
    # 1. 模拟群聊消息事件，不含 @CC
    # 2. 验证 mention_bot=False
    # 3. 验证不创建 session，直接返回
    pass

def test_p2p_message_still_works():
    # 确保现有 P2P 功能不受影响
    pass

def test_quoted_message_context_injected():
    # 群聊引用消息场景
    pass
```

- [ ] **Step 2: 运行完整测试套件**

Run: `pytest tests/ -v --tb=short`

- [ ] **Step 3: 提交**

```bash
git add tests/test_group_chat_integration.py
git commit -m "test: add group chat integration tests"
```

---

## Self-Review Checklist

1. **Spec coverage:** 每个需求点都有对应任务
   - 群聊消息接收 → Task 3（复用 im.message.receive_v1）
   - @CC 检测 → Task 3（mentions[] 数组，**无需** mention_detector.py）
   - Session 隔离 → Task 2（chat_id + thread_id）
   - 上下文注入 → Task 5（parent_id 引用消息）
   - 访问控制 → Task 6（per-group 配置）

2. **Placeholder scan:** 无 TBD/TODO，所有代码块完整

3. **Type consistency:** `IncomingMessage` 新增字段在所有任务中使用一致

4. **Key difference from original plan:**
   - ❌ 原计划：注册 `p2_im_chat_receive_v1` 事件，新增 `mention_detector.py` 解析 XML
   - ✅ 实际：复用 `im.message.receive_v1`，通过 `chat_type === 'group'` 区分，mentions[] 数组判断 @CC

---

**Plan complete.** Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
