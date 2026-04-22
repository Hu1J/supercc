# cc-feishu-bridge 通过 OpenRouter 调用时 400 错误分析

## 问题现象

cc-feishu-bridge 运行一段时间后，所有请求均返回 400 错误，无法与飞书用户通讯：

```
API Error: 400 {"error":{"message":"Provider returned error","code":400,
"metadata":{"raw":"{\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",
\"message\":\"messages.1.content.0: Invalid `signature` in `thinking` block\"},
\"request_id\":\"req_011CaBHGWDkZd9cQKUjsmhZf\"}",
"provider_name":"Azure","is_byok":false}}}
```

特征：
- cost = 0.0（请求未被处理即被拒绝）
- 每次请求均失败，重启 bridge 后仍然失败
- 同一时间，原生 Claude Code CLI 工作正常

## 根本原因

### 1. Thinking Block 的 Signature 机制

Anthropic API 在启用 extended thinking 时，Claude 的响应会包含 `thinking` block，每个 thinking block 都带有一个加密 `signature` 字段。当后续请求将历史消息（包含 thinking block）发回给 API 时，Anthropic 会**校验 signature 的完整性**。

### 2. OpenRouter 代理破坏了 Signature

请求链路：

```
cc-feishu-bridge → claude_agent_sdk → Claude CLI → OpenRouter → Anthropic API (Azure)
```

OpenRouter 作为中间代理，在转发请求/响应时**修改或损坏了 thinking block 中的 signature 字段**。

### 3. Session 历史积累触发问题

cc-feishu-bridge 使用 `continue_conversation=True`，每次新消息都会恢复之前的 session（即 `--continue` 模式）。CLI 会将完整对话历史（包含之前所有 thinking block）重新发送给 API。

时间线：
1. Bridge 启动，创建新 session → 正常
2. 用户发消息，Claude 返回带 thinking block 的响应 → 正常（thinking block 的 signature 未被回传）
3. 更多消息积累，session 文件增长（本案例中达到 **16MB / 6212 行 / 1641 个 thinking block**）
4. Bridge 重启或 CLI 重连，`--continue` 加载完整历史 → **signature 校验失败，400 错误**

### 4. 为什么原生 Claude Code 不受影响？

原生 CLI 每次启动开新 session；即使使用 `--continue`，也是恢复的用户自己的 session（signature 来源一致）。cc-feishu-bridge 的特殊之处在于它长期维护同一个 session，导致大量带 signature 的 thinking block 积累。

## 复现

### 环境

- macOS Darwin 25.2.0
- Claude Code CLI: 2.1.114
- claude-agent-sdk: 0.1.63
- cc-feishu-bridge: 0.3.24
- API 配置：通过 OpenRouter 代理（`~/.claude/settings.json`）

### 复现脚本

文件路径：`/Users/james/.openclaw/workspace/test_bridge_session.py`

```python
#!/usr/bin/env python3
"""复现 cc-feishu-bridge 的 400 错误：Invalid signature in thinking block"""
import asyncio
import sys


async def test(session_id: str | None = None):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    opts: dict = dict(
        cwd="/Users/james/.openclaw/workspace",
        include_partial_messages=True,
        permission_mode="bypassPermissions",
    )
    if session_id:
        opts["resume"] = session_id

    options = ClaudeAgentOptions(**opts)
    options.system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": "你是一个在飞书中回答问题的助手。",
    }

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt="回复两个字：你好")
            async for message in client.receive_response():
                name = type(message).__name__
                if name == "AssistantMessage":
                    for block in getattr(message, "content", []):
                        if hasattr(block, "text"):
                            print(f"[TEXT] {block.text[:500]}")
                elif name == "ResultMessage":
                    cost = getattr(message, "total_cost_usd", 0)
                    sid = getattr(message, "session_id", None)
                    print(f"[RESULT] cost={cost}, session={sid}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Testing with session_id={sid!r}")
    asyncio.run(test(sid))
```

### 复现步骤

```bash
# 1. 不指定 session（新 session）→ 成功
python3 test_bridge_session.py
# 输出: [TEXT] 你好  [RESULT] cost=0.066 ...

# 2. 指定含有 thinking block 的旧 session → 失败
python3 test_bridge_session.py ca4e375b-6589-490b-8d64-ed9d4cfa2ab6
# 输出: [TEXT] API Error: 400 ... Invalid `signature` in `thinking` block
```

### 关键数据

损坏的 session 文件统计：

| 指标 | 值 |
|------|------|
| 文件路径 | `~/.claude/projects/-Users-james--openclaw-workspace/ca4e375b-....jsonl` |
| 文件大小 | 16MB |
| 总行数 | 6,219 |
| 含 thinking+signature 的行 | 1,641 |

## 解决方案

### 临时治标：清除损坏的 Session

删除或重命名 bridge 正在使用的 session 文件，让 bridge 创建新 session：

```bash
# 找到 bridge 使用的 session ID（从日志中获取）
SESSION_ID="ca4e375b-6589-490b-8d64-ed9d4cfa2ab6"
PROJECT_DIR="$HOME/.claude/projects/-Users-james--openclaw-workspace"

# 备份并删除
mv "$PROJECT_DIR/$SESSION_ID.jsonl" "$PROJECT_DIR/$SESSION_ID.jsonl.bak"
mv "$PROJECT_DIR/$SESSION_ID" "$PROJECT_DIR/$SESSION_ID.bak" 2>/dev/null
mv "$HOME/.claude/session-env/$SESSION_ID" "$HOME/.claude/session-env/$SESSION_ID.bak" 2>/dev/null

# 重启 bridge
# 在飞书中发送 /restart，或手动重启 cc-feishu-bridge 进程
```

> 注意：此方案只解决当前问题。随着使用，新 session 的 thinking block 会再次积累，问题会复发。

### 长期治本方案

#### 方案 A：禁用 Extended Thinking（简单但有代价）

在 `integration.py` 的 `_init_options()` 中禁用 thinking：

```python
options = ClaudeAgentOptions(
    cwd=self.approved_directory or ".",
    include_partial_messages=True,
    permission_mode="bypassPermissions",
    continue_conversation=continue_conversation,
    thinking={"type": "disabled"},   # ← 新增
    mcp_servers={...},
)
```

**对工作质量的影响：**

Extended thinking 是 Claude 在回答前进行深度推理的能力。禁用后：

| 场景 | 影响程度 | 说明 |
|------|----------|------|
| 简单问答、日常对话 | 无影响 | 不需要深度推理 |
| 简单代码编写 | 几乎无影响 | 直接模式足够处理 |
| 复杂多步骤编程 | 有一定影响 | 少了内部推理步骤，偶尔会遗漏边界情况 |
| 复杂架构设计/调试 | 有明显影响 | thinking 对复杂推理帮助很大 |
| 数学/逻辑推理 | 影响较大 | 这类任务最依赖 chain-of-thought |

总体评估：**对飞书聊天场景影响不大**。飞书中的交互通常是轻量级问答和小型编码任务，不太依赖 extended thinking。如果用户经常通过飞书让 Claude 做复杂编程任务，则需要考虑其他方案。

#### 方案 B：定期轮换 Session（推荐）

不禁用 thinking，而是在 session 达到一定大小时自动开始新 session：

```python
import os

SESSION_MAX_SIZE = 2 * 1024 * 1024  # 2MB 阈值

def _should_rotate_session(self) -> bool:
    """检查当前 session 是否需要轮换"""
    if not self._last_session_id:
        return False
    session_path = (
        Path.home() / ".claude" / "projects"
        / "-Users-james--openclaw-workspace"
        / f"{self._last_session_id}.jsonl"
    )
    try:
        return session_path.stat().st_size > SESSION_MAX_SIZE
    except FileNotFoundError:
        return False

def _init_options(self, system_prompt_append=None, continue_conversation=True):
    if continue_conversation and self._should_rotate_session():
        logger.info("Session too large, starting fresh")
        continue_conversation = False  # 强制新 session

    # ... 原有代码 ...
```

优点：保留 thinking 能力，只在 session 过大时切换。
缺点：切换后丢失对话上下文。

#### 方案 C：从 Session 历史中剔除 Thinking Block（最佳但需改 SDK）

在 `claude_agent_sdk` 或 Claude CLI 层面，当 `--continue` 加载历史消息时，自动剔除或清空 thinking block 的 signature 字段。这需要修改 SDK 或 CLI 的行为，不是 bridge 侧能单独解决的。

可以向 `claude-agent-sdk` 提交 feature request：在走非 Anthropic 直连 API 时（如 OpenRouter），自动处理 thinking block 的兼容性。

#### 方案 D：在 OpenRouter 端修复 Signature 透传

这是 OpenRouter 的 bug——代理层不应该修改 thinking block 的内容。可以向 OpenRouter 报告此问题。但修复时间不可控。

### 方案对比

| 方案 | 实施难度 | 工作质量 | 长期可靠性 | 推荐度 |
|------|----------|----------|------------|--------|
| A. 禁用 thinking | 低（改一行） | 轻微下降 | 高 | 飞书轻量场景推荐 |
| B. 定期轮换 session | 中 | 无影响 | 中（会丢上下文） | 通用推荐 |
| C. SDK 层面剔除 | 高（需改 SDK） | 无影响 | 高 | 最佳但依赖上游 |
| D. OpenRouter 修复 | 不可控 | 无影响 | 高 | 需等待第三方 |

### 推荐组合

**短期**：方案 A（禁用 thinking）或临时清 session 文件

**中期**：方案 B（session 轮换）+ 方案 A 作为 fallback

**长期**：向 OpenRouter 和 claude-agent-sdk 提 issue，推动方案 C/D

## 附录：相关文件路径

| 文件 | 说明 |
|------|------|
| `~/.claude/settings.json` | API 配置（OpenRouter 凭证和模型） |
| `~/.claude/projects/-Users-james--openclaw-workspace/*.jsonl` | Session 历史文件 |
| `/opt/miniconda3/.../cc_feishu_bridge/claude/integration.py` | Bridge 的 Claude SDK 集成代码 |
| `/opt/miniconda3/.../claude_agent_sdk/_internal/transport/subprocess_cli.py` | SDK 启动 CLI 子进程的逻辑 |
| `.cc-feishu-bridge/cc-feishu-bridge.log` | Bridge 运行日志 |
| `test_bridge_session.py` | Bug 复现脚本 |
