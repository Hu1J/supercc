"""Cron job MCP tools — 7 tools exposed to Claude Code via MCP."""
from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Optional

CRON_SYSTEM_GUIDE = """
【定时任务】工具前缀: mcp__cron__

当用户要求创建、查看、管理定时任务时，使用以下 MCP 工具（注意：CC 内置也有同名 CronList/CronCreate，务必用带 Bridge 前缀的这个）：

## 定时任务工具
mcp__cron__BridgeCronCreate — 创建定时任务（自动发送到当前飞书会话）
mcp__cron__BridgeCronList — 列出所有定时任务
mcp__cron__BridgeCronDelete — 删除定时任务
mcp__cron__BridgeCronPause — 暂停定时任务
mcp__cron__BridgeCronResume — 恢复被暂停的定时任务
mcp__cron__BridgeCronTrigger — 立即触发一次定时任务
mcp__cron__BridgeCronLogs — 查看定时任务的执行日志（返回文件路径，需 CC 自行读取文件生成摘要）
"""

from supercc.cron_scheduler import (
    create_job,
    list_jobs,
    delete_job,
    update_job,
    get_job,
    get_job_logs,
)
from supercc.config import Config


_cron_scheduler: Optional["CronScheduler"] = None
_cron_config: Optional[Config] = None


def set_cron_scheduler(scheduler: "CronScheduler", config: Config):
    """Called by main.py to wire up the scheduler instance."""
    global _cron_scheduler, _cron_config
    _cron_scheduler = scheduler
    _cron_config = config


def _get_data_dir() -> str:
    from supercc.config import resolve_config_path
    _, data_dir = resolve_config_path()
    return data_dir


def _get_chat_id() -> Optional[str]:
    from supercc.claude.session_manager import SessionManager
    from supercc.config import resolve_config_path, SESSIONS_DB_PATH
    _, _ = resolve_config_path()
    db_path = SESSIONS_DB_PATH
    sm = SessionManager(db_path=db_path)
    session = sm.get_active_session_by_chat_id()
    return session.chat_id if session else None


def _fmt_job(j: dict) -> str:
    lines = [
        f"⏰ **{j.get('name', j['id'])}** (`{j['id']}`)",
        f"  Schedule: {j.get('schedule_display', '?')}",
        f"  State: {j.get('state', 'scheduled')} ({'enabled' if j.get('enabled') else 'disabled'})",
        f"  Next run: {j.get('next_run_at', '—')}",
        f"  Last run: {j.get('last_run_at', '—')} → {j.get('last_status', '—')}",
    ]
    if j.get('last_error'):
        lines.append(f"  Last error: {j['last_error']}")
    return "\n".join(lines)


def _fmt_job_summary(j: dict) -> str:
    state = j.get('state', 'scheduled')
    schedule = j.get('schedule_display', '?')
    next_run = j.get('next_run_at', '—')
    return f"⏰ **{j.get('name', j['id'])}** (`{j['id']}`) [{state}] {schedule} | next: {next_run}"


def _build_cron_mcp_server():
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        "BridgeCronCreate",
        "创建一个新的定时任务。任务会按照 schedule 自动运行，并把结果发送到创建时所在的飞书会话。",
        {
            "schedule": str,
            "prompt": str,
            "name": str,
            "repeat": int,
        },
    )
    async def cron_create(args: dict) -> dict:
        schedule = args.get("schedule", "").strip()
        prompt = args.get("prompt", "").strip()
        name = args.get("name", "").strip() or None
        repeat = args.get("repeat")
        if repeat is not None:
            repeat = int(repeat)

        if not schedule or not prompt:
            return {"content": [{"type": "text", "text": "schedule 和 prompt 都是必填的"}], "is_error": True}

        chat_id = _get_chat_id()
        if not chat_id:
            return {"content": [{"type": "text", "text": "未找到活跃飞书会话，请先在飞书里发一条消息"}], "is_error": True}

        data_dir = _get_data_dir()
        try:
            job = create_job(
                prompt=prompt,
                schedule=schedule,
                chat_id=chat_id,
                name=name,
                repeat=repeat,
                data_dir=data_dir,
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"参数错误: {e}"}], "is_error": True}

        return {"content": [{"type": "text", "text": f"✅ 定时任务已创建\n\n{_fmt_job(job)}\n\n将在 {job['next_run_at']} 首次执行"}]}

    @tool(
        "BridgeCronList",
        "列出所有定时任务，包括状态、下次执行时间、上次执行结果。",
        {},
    )
    async def cron_list(args: dict) -> dict:
        data_dir = _get_data_dir()
        jobs = list_jobs(data_dir)
        if not jobs:
            return {"content": [{"type": "text", "text": "📭 暂无定时任务。"}]}
        lines = [f"⏰ 定时任务（共 {len(jobs)} 个）\n"]
        for j in jobs:
            lines.append(_fmt_job_summary(j))
            lines.append("")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "BridgeCronDelete",
        "删除一个定时任务，任务停止运行且执行历史会被清除。",
        {"job_id": str},
    )
    async def cron_delete(args: dict) -> dict:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return {"content": [{"type": "text", "text": "job_id 是必填的"}], "is_error": True}

        data_dir = _get_data_dir()
        job = get_job(job_id, data_dir)
        if not job:
            return {"content": [{"type": "text", "text": f"未找到 job_id={job_id} 的任务"}], "is_error": True}

        ok = delete_job(job_id, data_dir)
        if ok:
            return {"content": [{"type": "text", "text": f"🗑️ 定时任务 `{job_id}` 已删除。"}]}
        return {"content": [{"type": "text", "text": f"删除失败，未找到 job_id={job_id}"}], "is_error": True}

    @tool(
        "BridgeCronPause",
        "暂停一个定时任务。暂停后任务不会自动执行，但可以随时恢复。",
        {"job_id": str},
    )
    async def cron_pause(args: dict) -> dict:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return {"content": [{"type": "text", "text": "job_id 是必填的"}], "is_error": True}

        data_dir = _get_data_dir()
        job = get_job(job_id, data_dir)
        if not job:
            return {"content": [{"type": "text", "text": f"未找到 job_id={job_id} 的任务"}], "is_error": True}

        updated = update_job(job_id, {"enabled": False, "state": "paused"}, data_dir)
        if updated:
            return {"content": [{"type": "text", "text": f"⏸ 任务 `{job_id}` 已暂停。"}]}
        return {"content": [{"type": "text", "text": f"暂停失败"}], "is_error": True}

    @tool(
        "BridgeCronResume",
        "恢复一个被暂停的定时任务，任务将按原 schedule 继续运行。",
        {"job_id": str},
    )
    async def cron_resume(args: dict) -> dict:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return {"content": [{"type": "text", "text": "job_id 是必填的"}], "is_error": True}

        data_dir = _get_data_dir()
        job = get_job(job_id, data_dir)
        if not job:
            return {"content": [{"type": "text", "text": f"未找到 job_id={job_id} 的任务"}], "is_error": True}

        updated = update_job(job_id, {"enabled": True, "state": "scheduled"}, data_dir)
        if updated:
            return {"content": [{"type": "text", "text": f"▶️ 任务 `{job_id}` 已恢复，下次执行: {updated.get('next_run_at', '—')}"}]}
        return {"content": [{"type": "text", "text": f"恢复失败"}], "is_error": True}

    @tool(
        "BridgeCronTrigger",
        "立刻触发一次定时任务，不影响其正常的执行计划。",
        {"job_id": str},
    )
    async def cron_trigger(args: dict) -> dict:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return {"content": [{"type": "text", "text": "job_id 是必填的"}], "is_error": True}

        data_dir = _get_data_dir()
        job = get_job(job_id, data_dir)
        if not job:
            return {"content": [{"type": "text", "text": f"未找到 job_id={job_id} 的任务"}], "is_error": True}

        if not job.get("enabled"):
            return {"content": [{"type": "text", "text": f"任务 `{job_id}` 已暂停，请先 resume"}], "is_error": True}

        # Set next_run_at to the past so it becomes due on the next tick
        from datetime import datetime, timezone
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        update_job(job_id, {"next_run_at": past}, data_dir)

        return {"content": [{"type": "text", "text": f"🚀 任务 `{job_id}` 已触发，将在下一次调度周期执行。"}]}

    @tool(
        "BridgeCronLogs",
        "查看某个定时任务的执行历史，返回日志文件路径列表（不含内容），由 AI 自行读取并生成摘要。",
        {"job_id": str},
    )
    async def cron_logs(args: dict) -> dict:
        job_id = args.get("job_id", "").strip()
        if not job_id:
            return {"content": [{"type": "text", "text": "job_id 是必填的"}], "is_error": True}

        data_dir = _get_data_dir()
        result = get_job_logs(job_id, data_dir)
        if not result or not result.get("job"):
            return {"content": [{"type": "text", "text": f"未找到 job_id={job_id} 的任务"}], "is_error": True}

        job = result["job"]
        runs = result.get("runs", [])

        if not runs:
            return {"content": [{"type": "text", "text": f"⏰ 任务 `{job_id}` 暂无执行记录。"}]}

        lines = [f"⏰ 任务 `{job_id}` 的日志文件：\n"]
        for r in runs:
            lines.append(r["output_file"])

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="cron",
        version="1.0.0",
        tools=[
            cron_create,
            cron_list,
            cron_delete,
            cron_pause,
            cron_resume,
            cron_trigger,
            cron_logs,
        ],
    )


_mcp_server = None
_mcp_server_lock = threading.Lock()


def get_cron_mcp_server():
    global _mcp_server
    if _mcp_server is None:
        with _mcp_server_lock:
            if _mcp_server is None:
                _mcp_server = _build_cron_mcp_server()
    return _mcp_server
