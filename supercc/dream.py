"""Dream — nightly memory refinement at 3am.

Runs as a cron job that asks Claude to review all memories
(user preferences + project memories) and refine them:
merge duplicates, simplify verbose content, delete obsolete entries.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


DREAM_PROMPT = """【做梦 — 每日凌晨精炼】

你是记忆管家。请审查所有用户记忆和项目记忆，精炼冗余内容、合并相似记忆、删除过时信息。

## 操作步骤

1. **获取所有用户偏好**：调用 `mcp__memory__MemoryListUser`（不需要参数），获取当前用户的所有偏好

2. **获取所有项目记忆**：调用 `mcp__memory__MemoryListProj`，project_path 不需要传，获取当前项目的所有记忆

3. **精炼记忆**：
   - 合并内容高度相似的记忆（保留最完整的一条，更新其他为合并后的内容）
   - 精简冗长啰嗦的记忆内容（保留关键信息，去除重复表述）
   - 删除已过时或无价值的记忆
   - 用 `mcp__memory__MemoryUpdateProj`、`mcp__memory__MemoryUpdateUser` 更新内容有变化的记忆
   - 用 `mcp__memory__MemoryDeleteProj`、`mcp__memory__MemoryDeleteUser` 删除需要清理的记忆

4. **输出总结**：完成后，输出一段简短的精炼报告，说明你做了哪些合并/精简/删除操作
"""


def get_dream_prompt() -> str:
    """Return the prompt used for the dream cron job."""
    return DREAM_PROMPT


def register_dream_job(data_dir: str) -> bool:
    """
    Register the dream cron job (idempotent).

    Returns True if registered, False if skipped (already exists or no chat_id).
    """
    from supercc.cron_scheduler import list_jobs, create_job
    from supercc.main import _get_active_chat_id

    chat_id = _get_active_chat_id(data_dir)
    if not chat_id:
        logger.info("[dream] no active chat_id, skipping")
        return False

    existing = list_jobs(data_dir)
    if any(j.get("name") == "做梦" for j in existing):
        logger.info("[dream] job already registered, skipping")
        return False

    try:
        create_job(
            prompt=DREAM_PROMPT,
            schedule="0 3 * * *",  # 每天凌晨3点执行
            chat_id=chat_id,
            name="做梦",
            repeat=None,
            data_dir=data_dir,
            verbose=True,  # 流式推送 tool calls 到飞书
            notify_at="0 8 * * *",  # 早上8点通知结果
        )
        logger.info("[dream] registered daily dream job at 3am")
        return True
    except Exception as e:
        logger.warning(f"[dream] failed to register: {e}")
        return False