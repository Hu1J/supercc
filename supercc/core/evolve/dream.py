"""Dream — nightly memory refinement at 3am.

Runs as a cron job that asks Claude to review all memories
(user preferences + project memories) and refine them:
merge duplicates, simplify verbose content, delete obsolete entries.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


DREAM_PROMPT = """【做梦 — 每日凌晨精炼】

你是记忆管家。请审查所有用户记忆和项目记忆，精炼冗余内容、合并相似记忆、删除过时信息，并纠正记忆放错位置的情况。

## 操作步骤

1. **获取所有用户偏好**：调用 `mcp__SuperCC__MemoryListUser`（不需要参数），获取当前用户的所有偏好

2. **获取所有项目记忆**：对 project_path 下的每一种 platform × chat_id 组合，调用 `mcp__SuperCC__MemoryListProj`（project_path 参数必传），获取该组合下的所有项目记忆。platform 包括 feishu 等，chat_id 为各飞书会话 ID，遍历所有组合确保不遗漏。

3. **检查放错位置的记忆**：
   - 用户偏好中，如果某条内容涉及：文件路径、代码规范、bug、架构决策、git、项目工具使用方式等**项目特有**的信息，说明它放错位置了
   - 同样，项目记忆中如果某条是关于用户的**个人偏好**（语言风格、沟通习惯等），也应纠正
   - 发现放错位置的记忆：先用 `MemoryAddXxx` 在正确位置新建一条（保留相同 title/keywords/content），再用 `MemoryDeleteXxx` 删除旧的那条

4. **精炼记忆**：
   - 合并内容高度相似的记忆（保留最完整的一条，更新其他为合并后的内容）
   - 精简冗长啰嗦的记忆内容（保留关键信息，去除重复表述）
   - 删除已过时或无价值的记忆
   - 用 `mcp__SuperCC__MemoryUpdateProj`、`mcp__SuperCC__MemoryUpdateUser` 更新内容有变化的记忆
   - 用 `mcp__SuperCC__MemoryDeleteProj`、`mcp__SuperCC__MemoryDeleteUser` 删除需要清理的记忆

5. **输出总结**：完成后，输出一段简短的精炼报告，说明你做了哪些纠正/合并/精简/删除操作
"""


def get_dream_prompt() -> str:
    """Return the prompt used for the dream cron job."""
    return DREAM_PROMPT


def register_dream_job(data_dir: str) -> bool:
    """
    Register the dream cron job (idempotent, P2P only).

    Returns True if registered, False if skipped (already exists, no chat_id, or group chat).
    Only recreates job if the prompt has changed from the existing one.
    """
    from supercc.core.cron_scheduler import list_jobs, create_job, delete_job, _get_active_chat_id, _is_group_chat

    chat_id = _get_active_chat_id(data_dir)
    if not chat_id:
        logger.info("[dream] no active chat_id, skipping")
        return False

    if _is_group_chat(data_dir, chat_id):
        logger.info("[dream] active chat is a group, skipping registration")
        return False

    # Only recreate if prompt changed
    existing = list_jobs(data_dir)
    for j in existing:
        if j.get("name") == "做梦":
            if j.get("prompt") == DREAM_PROMPT:
                logger.info("[dream] prompt unchanged, skipping recreation")
                return True
            delete_job(j["id"], data_dir)
            logger.info("[dream] prompt changed, removed old job, will recreate")
            break

    try:
        create_job(
            prompt=DREAM_PROMPT,
            schedule="0 9 * * 1-5",  # 工作日早上9点
            chat_id=chat_id,
            name="做梦",
            repeat=None,
            data_dir=data_dir,
            verbose=True,  # 流式推送 tool calls 到飞书
            notify_at="0 10 * * 1-5",  # 工作日早上10点通知结果
        )
        # 额外创建下午2点和晚上8点的任务
        create_job(
            prompt=DREAM_PROMPT,
            schedule="0 14 * * 1-5",  # 工作日下午2点
            chat_id=chat_id,
            name="做梦",
            repeat=None,
            data_dir=data_dir,
            verbose=False,
            notify_at=None,
        )
        create_job(
            prompt=DREAM_PROMPT,
            schedule="0 20 * * 1-5",  # 工作日晚上8点
            chat_id=chat_id,
            name="做梦",
            repeat=None,
            data_dir=data_dir,
            verbose=False,
            notify_at=None,
        )
        logger.info("[dream] registered dream jobs at 9am/2pm/8pm on weekdays")
        return True
    except Exception as e:
        logger.warning(f"[dream] failed to register: {e}")
        return False