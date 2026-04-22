"""
Cron job scheduler — executes time-based jobs and delivers output to Feishu.

Stores jobs in {data_dir}/cron_jobs.json
Output logs in {data_dir}/cron_logs/{job_id}/{timestamp}.md

Architecture:
- CronScheduler runs tick() every 60s from a background thread
- Each due job runs in its own ClaudeIntegration subprocess (avoids concurrent conflicts)
- Job output is saved to file AND sent to the job's chat_id via FeishuClient
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_CST = ZoneInfo("Asia/Shanghai")
from pathlib import Path
from typing import Optional

from cc_feishu_bridge.config import Config
from cc_feishu_bridge.claude.integration import ClaudeIntegration
from cc_feishu_bridge.feishu.client import FeishuClient


def _get_active_chat_id(data_dir: str) -> str | None:
    """Get the most recent active session's chat_id."""
    db_path = os.path.join(data_dir, "sessions.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT chat_id FROM sessions WHERE chat_id IS NOT NULL ORDER BY last_used DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["chat_id"] if row else None
    except Exception:
        return None

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(_CST)


# ─── Schedule Parsing ────────────────────────────────────────────────────────

def parse_schedule(schedule: str) -> dict:
    """
    Parse schedule string into structured format.

    Returns dict with:
      - kind: "once" | "interval" | "cron"
      - minutes: int (interval only)
      - expr: str (cron only)
      - run_at: ISO str (once only)
      - display: human-readable str

    Examples:
      "30m"              → once in 30 minutes
      "2h"               → once in 2 hours
      "every 30m"        → recurring every 30 minutes
      "every 2h"         → recurring every 2 hours
      "0 9 * * *"        → cron expression
      "2026-02-03T14:00" → once at timestamp
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    # "every X" pattern → recurring interval
    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        minutes = _parse_duration(duration_str)
        return {
            "kind": "interval",
            "minutes": minutes,
            "display": f"every {minutes}m"
        }

    # Cron expression (5 or 6 fields)
    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]):
        try:
            import croniter as croniter_module
            croniter_module.croniter(schedule)
        except ImportError:
            raise ValueError("Cron expressions require the 'croniter' package. Install with: pip install croniter")
        except Exception:
            raise ValueError(f"Invalid cron expression: {schedule}")
        return {
            "kind": "cron",
            "expr": schedule,
            "display": schedule
        }

    # ISO timestamp (contains T)
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")

    # Duration like "30m", "2h" → one-shot from now
    try:
        minutes = _parse_duration(schedule)
        run_at = _utcnow() + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": f"once in {original}"
        }
    except ValueError:
        pass

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - '30m', '2h' (once after duration)\n"
        f"  - 'every 30m', 'every 2h' (recurring)\n"
        f"  - '0 9 * * *' (cron expression)\n"
        f"  - '2026-02-03T14:00' (once at time)"
    )


def _parse_duration(s: str) -> int:
    """Parse duration string into minutes."""
    s = s.strip().lower()
    match = re.match(r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', s)
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', '1d'")
    value = int(match.group(1))
    unit = match.group(2)[0]
    multipliers = {'m': 1, 'h': 60, 'd': 1440}
    return value * multipliers[unit]


# ─── Job Storage ─────────────────────────────────────────────────────────────

class _CronStore:
    """Thin wrapper around cron_jobs.json with atomic writes."""

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "cron_jobs.json"
        self._output_dir = Path(data_dir) / "cron_logs"

    def _ensure_dirs(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _load_raw(self) -> list:
        self._ensure_dirs()
        if not self._path.exists():
            return []
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f).get("jobs", [])
        except (json.JSONDecodeError, IOError):
            return []

    def _save_raw(self, jobs: list):
        self._ensure_dirs()
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix='.tmp', prefix='.cron_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({
                    "jobs": jobs,
                    "updated_at": _utcnow().isoformat()
                }, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_jobs(self) -> list:
        return copy.deepcopy(self._load_raw())

    def save_jobs(self, jobs: list):
        self._save_raw(jobs)

    def output_path(self, job_id: str, timestamp: str) -> Path:
        job_dir = self._output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir / f"{timestamp}.md"

    def list_outputs(self, job_id: str) -> list:
        job_dir = self._output_dir / job_id
        if not job_dir.exists():
            return []
        files = sorted(job_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
        return [str(f) for f in files]


class _PendingStore:
    """Stores pending job notifications that haven't reached their notify_at time."""

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / ".pending_notifications.json"

    def _ensure_dir(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        self._ensure_dir()
        if not self._path.exists():
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self, data: dict):
        self._ensure_dir()
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix='.tmp', prefix='.pending_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, job_id: str, response: str, chat_id: str, job_name: str, notify_at: str, intermediates: list | None = None) -> str:
        """Save a pending notification. Returns the unique key for this pending entry."""
        data = self._load()
        created_at = _utcnow().isoformat()
        # Use job_id + created_at as key to avoid overwriting when same job triggers again
        pending_key = f"{job_id}:{created_at}"
        data[pending_key] = {
            "response": response,
            "intermediates": intermediates or [],
            "chat_id": chat_id,
            "job_name": job_name,
            "notify_at": notify_at,
            "created_at": created_at,
            "job_id": job_id,  # store original job_id for reference
        }
        self._save(data)
        return pending_key

    def get_due(self) -> list[dict]:
        """Return all pending notifications that are due to be sent (does NOT remove them)."""
        data = self._load()
        now = _utcnow()
        due = []
        for pending_key, entry in data.items():
            notify_at = _ensure_aware(datetime.fromisoformat(entry["notify_at"]))
            if notify_at <= now:
                entry_copy = dict(entry)
                entry_copy["pending_key"] = pending_key
                due.append(entry_copy)
        return due

    def remove(self, pending_key: str) -> None:
        """Remove a pending notification after it's been sent."""
        data = self._load()
        data.pop(pending_key, None)
        self._save(data)


# ─── Schedule Computation ─────────────────────────────────────────────────────

def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def compute_next_run(schedule: dict, last_run_at: Optional[str] = None) -> Optional[str]:
    """Compute next run time. Returns ISO str or None if no more runs."""
    now = _utcnow()

    if schedule["kind"] == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
        # Grace window: 120 seconds for one-shot
        if run_at_dt >= now - timedelta(seconds=120):
            return run_at
        return None  # already fired and missed

    elif schedule["kind"] == "interval":
        minutes = schedule["minutes"]
        if last_run_at:
            last = _ensure_aware(datetime.fromisoformat(last_run_at))
            next_run = last + timedelta(minutes=minutes)
        else:
            next_run = now + timedelta(minutes=minutes)
        return next_run.isoformat()

    elif schedule["kind"] == "cron":
        try:
            import croniter as croniter_module
            cron = croniter_module.croniter(schedule["expr"], now)
            next_run = cron.get_next(datetime)
            return next_run.isoformat()
        except Exception:
            return None

    return None


# ─── Job CRUD ────────────────────────────────────────────────────────────────

def create_job(
    prompt: str,
    schedule: str,
    chat_id: str,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    data_dir: str = "",
    verbose: bool = False,
    notify_at: Optional[str] = None,
) -> dict:
    """
    Create a new cron job.

    Args:
        prompt: The prompt to run when the job fires
        schedule: Schedule string (see parse_schedule)
        chat_id: Feishu chat_id to deliver output to
        name: Optional friendly name
        repeat: None = infinite, int = max runs
        data_dir: Bridge data directory
        verbose: If True, stream tool calls to Feishu in real-time (default False)
        notify_at: Optional cron expression for when to send notification.
                   If set, execution happens at `schedule` but notification is sent
                   at `notify_at` instead of immediately after execution.

    Returns:
        The created job dict
    """
    parsed = parse_schedule(schedule)
    # Auto-set repeat=1 for one-shot if not specified
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1

    # Parse notify_at if provided
    notify_schedule = None
    if notify_at:
        notify_schedule = parse_schedule(notify_at)

    job_id = uuid.uuid4().hex[:12]
    now = _utcnow()

    label_source = prompt[:50].strip() if prompt else "cron job"
    job = {
        "id": job_id,
        "name": name or label_source,
        "prompt": prompt,
        "schedule": parsed,
        "schedule_display": parsed.get("display", schedule),
        "repeat": {
            "times": repeat,
            "completed": 0
        },
        "enabled": True,
        "state": "scheduled",
        "chat_id": chat_id,
        "verbose": verbose,
        "notify_at": notify_schedule,
        "notify_at_display": notify_schedule.get("display") if notify_schedule else None,
        "created_at": now.isoformat(),
        "next_run_at": compute_next_run(parsed),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
    }

    store = _CronStore(data_dir)
    jobs = store.load_jobs()
    jobs.append(job)
    store.save_jobs(jobs)

    return job


def get_job(job_id: str, data_dir: str) -> Optional[dict]:
    jobs = _CronStore(data_dir).load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            return copy.deepcopy(j)
    return None


def list_jobs(data_dir: str) -> list:
    return _CronStore(data_dir).load_jobs()


def update_job(job_id: str, updates: dict, data_dir: str) -> Optional[dict]:
    """Update job fields, refresh derived fields."""
    store = _CronStore(data_dir)
    jobs = store.load_jobs()
    for i, job in enumerate(jobs):
        if job["id"] != job_id:
            continue

        job.update(updates)

        if "schedule" in updates:
            schedule = job["schedule"]
            job["schedule_display"] = updates.get(
                "schedule_display",
                schedule.get("display", schedule.get("expr", ""))
            )
            if job.get("state") != "paused":
                job["next_run_at"] = compute_next_run(schedule, job.get("last_run_at"))

        if job.get("enabled") and job.get("state") != "paused" and not job.get("next_run_at"):
            job["next_run_at"] = compute_next_run(job["schedule"])

        jobs[i] = job
        store.save_jobs(jobs)
        return copy.deepcopy(job)
    return None


def delete_job(job_id: str, data_dir: str) -> bool:
    store = _CronStore(data_dir)
    jobs = store.load_jobs()
    original_len = len(jobs)
    jobs = [j for j in jobs if j["id"] != job_id]
    if len(jobs) < original_len:
        store.save_jobs(jobs)
        return True
    return False


def mark_run(
    job_id: str,
    success: bool,
    error: Optional[str] = None,
    data_dir: str = "",
):
    """Mark a job as having run. Updates counters and next_run_at."""
    store = _CronStore(data_dir)
    jobs = store.load_jobs()
    for i, job in enumerate(jobs):
        if job["id"] != job_id:
            continue

        now = _utcnow().isoformat()
        job["last_run_at"] = now
        job["last_status"] = "ok" if success else "error"
        job["last_error"] = error if not success else None

        if job.get("repeat"):
            job["repeat"]["completed"] = job["repeat"].get("completed", 0) + 1
            times = job["repeat"].get("times")
            completed = job["repeat"]["completed"]
            if times is not None and times > 0 and completed >= times:
                jobs.pop(i)
                store.save_jobs(jobs)
                return

        job["next_run_at"] = compute_next_run(job["schedule"], now)

        if job["next_run_at"] is None:
            job["enabled"] = False
            job["state"] = "completed"
        elif job.get("state") != "paused":
            job["state"] = "scheduled"

        store.save_jobs(jobs)
        return


def get_due_jobs(data_dir: str) -> list:
    """Return all jobs that are due to run now."""
    now = _utcnow()
    store = _CronStore(data_dir)
    jobs = store.load_jobs()
    due = []

    for job in jobs:
        if not job.get("enabled", True):
            continue

        next_run = job.get("next_run_at")
        if not next_run:
            continue

        next_run_dt = _ensure_aware(datetime.fromisoformat(next_run))
        if next_run_dt <= now:
            due.append(copy.deepcopy(job))

    return due


def get_job_logs(job_id: str, data_dir: str) -> dict:
    """
    Return job execution history with raw log content.

    Returns:
        {
            "job": job dict (without output history),
            "runs": [
                {
                    "timestamp": "...",
                    "status": "ok"|"error",
                    "error": "..." or null,
                    "output_file": "...",
                    "content": "...(full log content)..."
                },
                ...
            ]
        }
    """
    job = get_job(job_id, data_dir)
    if not job:
        return {}

    store = _CronStore(data_dir)
    output_files = store.list_outputs(job_id)

    runs = []
    for f in output_files:
        ts_name = Path(f).stem  # "2026-04-20_10-00-00"
        try:
            dt = datetime.strptime(ts_name, "%Y-%m-%d_%H-%M-%S")
            timestamp = dt.isoformat()
        except ValueError:
            timestamp = ts_name

        with open(f, encoding="utf-8") as fp:
            content = fp.read()

        status = "ok"
        error = None
        if job.get("last_error") and job.get("last_run_at"):
            last_ts = job["last_run_at"][:19].replace("T", "_").replace(":", "-")
            if last_ts in ts_name:
                status = "error"
                error = job.get("last_error")

        runs.append({
            "timestamp": timestamp,
            "status": status,
            "error": error,
            "output_file": f,
            "content": content,
        })

    return {
        "job": job,
        "runs": runs
    }


# ─── Job Execution ───────────────────────────────────────────────────────────

async def _run_job(job: dict, config: Config, data_dir: str, running_jobs: set[str] | None = None):
    """Execute a single cron job: run Claude, save output, deliver to Feishu."""
    job_id = job["id"]
    if running_jobs is None:
        running_jobs = set()
    chat_id = job["chat_id"]
    prompt = job["prompt"]
    job_name = job.get("name", job_id)
    ts_start = _utcnow()
    logger.info(f"[cron] Running job {job_id} ({job_name})")

    # ── Pre-flight ────────────────────────────────────────────────────────────
    steps = []
    _log = lambda step, note="": steps.append(f"[{_utcnow().strftime('%H:%M:%S')}] {step}" + (f" — {note}" if note else ""))

    _log("JOB_TRIGGERED", f"name={job_name}, schedule={job.get('schedule_display')}")

    # Create Feishu client for delivery
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
        data_dir=data_dir,
    )
    _log("FEISHU_CLIENT_CREATED")

    # Create independent Claude instance (avoids concurrent conflicts)
    claude = ClaudeIntegration(
        cli_path=config.claude.cli_path,
        max_turns=5,
        approved_directory=config.claude.approved_directory,
    )
    claude._init_options()
    _log("CLAUDE_INTEGRATION_CREATED")

    # ── Execute ───────────────────────────────────────────────────────────────
    skills_dir = Path(data_dir) / "skills"
    is_skill_scan = job_name == "Skill 优化扫描"

    # Snapshot before state for skill scan jobs
    before_state = None
    if is_skill_scan:
        from cc_feishu_bridge.skill_nudge import _get_skill_git_state
        before_state = _get_skill_git_state(skills_dir)
        prompt = prompt.replace("{SKILLS_DIR}", str(skills_dir))

    async def _stream_log(claude_msg):
        """Log Claude tool calls and text to execution trace (always)."""
        if claude_msg.tool_name:
            _log("TOOL", f"[{claude_msg.tool_name}] {str(claude_msg.tool_input)[:200]}")
        elif claude_msg.content:
            _log("TEXT", claude_msg.content[:100])

    is_verbose = job.get("verbose", False)
    has_notify_at = bool(job.get("notify_at"))

    # When notify_at is set, all output (including intermediate) should be sent at notify_at time.
    # Disable real-time streaming in this case regardless of verbose setting.
    stream_to_feishu = is_verbose and not has_notify_at

    # Collect intermediate messages for pending delivery
    intermediates: list[dict] = []

    async def _on_stream(claude_msg):
        try:
            if claude_msg.tool_name:
                from cc_feishu_bridge.format.reply_formatter import ReplyFormatter
                from cc_feishu_bridge.format.edit_diff import _DiffMarker, _MemoryCardMarker
                from cc_feishu_bridge.format.questionnaire_card import _AskUserQuestionMarker, format_questionnaire_card
                formatter = ReplyFormatter()
                result = formatter.format_tool_call(claude_msg.tool_name, claude_msg.tool_input)

                if stream_to_feishu:
                    # Send immediately
                    if isinstance(result, _DiffMarker):
                        await feishu.send_card(chat_id, result.card)
                    elif isinstance(result, list):
                        for marker in result:
                            if isinstance(marker, _DiffMarker):
                                await feishu.send_card(chat_id, marker.card)
                    elif isinstance(result, _MemoryCardMarker):
                        md = result.render()
                        if md:
                            await feishu.send_interactive_card(chat_id, md)
                    elif isinstance(result, _AskUserQuestionMarker):
                        card = format_questionnaire_card(result)
                        if card:
                            await feishu.send_card(chat_id, card)
                    else:
                        text = str(result)
                        if formatter.should_use_card(text):
                            await feishu.send_interactive_card(chat_id, text)
                        else:
                            await feishu.send_post(chat_id, text)
                else:
                    # Collect for later delivery
                    if isinstance(result, _DiffMarker):
                        intermediates.append({"type": "card", "content": result.card})
                    elif isinstance(result, list):
                        for marker in result:
                            if isinstance(marker, _DiffMarker):
                                intermediates.append({"type": "card", "content": marker.card})
                    elif isinstance(result, _MemoryCardMarker):
                        md = result.render()
                        if md:
                            intermediates.append({"type": "interactive_card", "content": md})
                    elif isinstance(result, _AskUserQuestionMarker):
                        card = format_questionnaire_card(result)
                        if card:
                            intermediates.append({"type": "card", "content": card})
                    else:
                        text = str(result)
                        intermediates.append({"type": "text", "content": text})

                await _stream_log(claude_msg)
            elif claude_msg.content:
                await _stream_log(claude_msg)
        except Exception as e:
            logger.warning(f"[_on_stream] error: {e}")

    try:
        _log("CLAUDE_QUERY_START")
        response, session_id, cost = await claude.query(prompt=prompt, on_stream=_on_stream)
        elapsed = (datetime.now(_CST) - ts_start).total_seconds()
        _log("CLAUDE_QUERY_DONE", f"elapsed={elapsed:.1f}s, session_id={session_id!r}, cost=${cost:.4f}")

        # For skill scan jobs, detect changes via git state comparison
        if is_skill_scan and before_state is not None:
            from cc_feishu_bridge.skill_nudge import _detect_skill_changes
            from cc_feishu_bridge.format.reply_formatter import should_use_card

            async def _skill_send(cid, text):
                if should_use_card(text):
                    await feishu.send_interactive_card(cid, text)
                else:
                    await feishu.send_post(cid, text)

            await _detect_skill_changes(
                before_state=before_state,
                skills_dir=skills_dir,
                chat_id=chat_id,
                send_to_feishu=_skill_send,
                notify=False,
            )
    except Exception as e:
        logger.warning(f"[cron] Job {job_id} Claude error: {e}")
        _log("CLAUDE_QUERY_ERROR", str(e))
        # Write execution trace before saving error result
        total_elapsed = (datetime.now(_CST) - ts_start).total_seconds()
        output_file = _save_job_output(job_id, data_dir, steps, response=None, error=str(e), total_elapsed=total_elapsed)
        mark_run(job_id, success=False, error=str(e), data_dir=data_dir)
        running_jobs.discard(job_id)
        return

    if not response or not response.strip():
        logger.info(f"[cron] Job {job_id} empty response, skipping send")
        _log("CLAUDE_RESPONSE_EMPTY")
        total_elapsed = (datetime.now(_CST) - ts_start).total_seconds()
        output_file = _save_job_output(job_id, data_dir, steps, response=None, error=None, total_elapsed=total_elapsed)
        mark_run(job_id, success=True, data_dir=data_dir)
        running_jobs.discard(job_id)
        return

    # ── Save output with execution trace ─────────────────────────────────────
    total_elapsed = (datetime.now(_CST) - ts_start).total_seconds()
    output_file = _save_job_output(job_id, data_dir, steps, response=response.strip(), error=None, total_elapsed=total_elapsed)
    logger.info(f"[cron] Job {job_id} output saved to {output_file}")

    # ── Deliver ────────────────────────────────────────────────────────────────
    notify_schedule = job.get("notify_at")
    if notify_schedule:
        # Save to pending store, notify later at notify_at
        next_notify = compute_next_run(notify_schedule)
        pending_store = _PendingStore(data_dir)
        pending_store.add(job_id, response.strip(), chat_id, job_name, next_notify, intermediates)
        _log("FEISHU_NOTIFY_PENDING", f"notify_at={next_notify}")
        logger.info(f"[cron] Job {job_id} notification pending until {next_notify}")
        mark_run(job_id, success=True, data_dir=data_dir)
        running_jobs.discard(job_id)
        return

    from cc_feishu_bridge.format.reply_formatter import should_use_card, optimize_markdown_style
    header = f"⏰ **{job_name}**"
    body = optimize_markdown_style(response.strip(), card_version=2)
    text = f"{header}\n\n{body}"
    try:
        _log("FEISHU_DELIVERY_START")
        if should_use_card(body):
            await feishu.send_interactive_card(chat_id, text)
        else:
            await feishu.send_post(chat_id, text)
        _log("FEISHU_DELIVERY_DONE")
        logger.info(f"[cron] Job {job_id} delivered to {chat_id}")
    except Exception as e:
        logger.warning(f"[cron] Job {job_id} delivery failed: {e}")
        _log("FEISHU_DELIVERY_ERROR", str(e))
        mark_run(job_id, success=True, error=f"Delivery failed: {e}", data_dir=data_dir)
        running_jobs.discard(job_id)
        return

    mark_run(job_id, success=True, data_dir=data_dir)
    running_jobs.discard(job_id)


def _save_job_output(job_id: str, data_dir: str, steps: list[str], response: str | None, error: str | None, total_elapsed: float | None = None) -> str:
    """Build a complete execution log file with all steps + optional response + optional error."""
    store = _CronStore(data_dir)
    timestamp = _utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    path = store.output_path(job_id, timestamp)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp', prefix='.out_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            # ── Execution trace ──────────────────────────────────────────────
            f.write("# 执行日志\n\n")
            for line in steps:
                f.write(f"{line}\n")
            elapsed_str = f"{total_elapsed:.1f}s" if total_elapsed is not None else "—"
            f.write(f"\n[完成] 耗时 {elapsed_str}\n")

            # ── Claude response ──────────────────────────────────────────────
            if error:
                f.write(f"\n---\n\n**错误**: {error}\n")
            elif response:
                f.write("\n---\n\n")
                f.write(response)
            f.write("\n")

        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return str(path)


# ─── CronScheduler ──────────────────────────────────────────────────────────

class CronScheduler:
    """
    Background scheduler that checks for due cron jobs every 60 seconds.

    Usage:
        scheduler = CronScheduler(config, data_dir)
        scheduler.start()
        # ... bridge runs ...
        scheduler.stop()
    """

    def __init__(self, config: Config, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = asyncio.Event()
        self._running_jobs: set[str] = set()  # prevent overlap: skip jobs already running

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("CronScheduler started")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run())

    def stop(self):
        """Synchronous stop — safe to call from signal handlers."""
        if self._thread is None:
            return
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._loop.call_soon_threadsafe(self._task.cancel if self._task else None)
        self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        logger.info("CronScheduler stopped")

    async def _run(self):
        """Main loop: tick every 60 seconds."""
        self._task = asyncio.current_task()
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("[cron] Tick error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        """Check for due jobs and run them. Awaits all jobs to ensure completion."""
        # Poll skill changes on every tick (independent of job scheduling)
        skills_dir = Path(self.data_dir) / "skills"
        if skills_dir.exists():
            from cc_feishu_bridge.skill_nudge import poll_skill_changes_and_notify
            from cc_feishu_bridge.feishu.client import FeishuClient
            feishu = FeishuClient(
                app_id=self.config.feishu.app_id,
                app_secret=self.config.feishu.app_secret,
                bot_name=self.config.feishu.bot_name,
                data_dir=self.data_dir,
            )

            async def _skill_send(cid, text):
                await feishu.send_post(cid, text)

            try:
                await poll_skill_changes_and_notify(
                    data_dir=self.data_dir,
                    skills_dir=skills_dir,
                    send_to_feishu=_skill_send,
                    get_chat_id=lambda dd: _get_active_chat_id(dd),
                )
            except Exception:
                logger.exception("[cron] poll_skill_changes_and_notify error")

        # Deliver any pending notifications that have reached their notify_at time
        pending_store = _PendingStore(self.data_dir)
        due_pending = pending_store.get_due()
        sent_this_tick: set[str] = set()  # dedup: skip entries sent successfully this tick
        if due_pending:
            from cc_feishu_bridge.feishu.client import FeishuClient
            from cc_feishu_bridge.format.reply_formatter import should_use_card, optimize_markdown_style
            feishu = FeishuClient(
                app_id=self.config.feishu.app_id,
                app_secret=self.config.feishu.app_secret,
                bot_name=self.config.feishu.bot_name,
                data_dir=self.data_dir,
            )
            for entry in due_pending:
                pending_key = entry.get("pending_key", entry.get("job_id", ""))
                if pending_key in sent_this_tick:
                    continue
                try:
                    # Send intermediate messages first (in order)
                    intermediates = entry.get("intermediates", [])
                    for msg in intermediates:
                        msg_type = msg.get("type", "text")
                        content = msg.get("content", "")
                        if msg_type == "card":
                            await feishu.send_card(entry["chat_id"], content)
                        elif msg_type == "interactive_card":
                            await feishu.send_interactive_card(entry["chat_id"], content)
                        else:
                            # text or unknown
                            if should_use_card(content):
                                await feishu.send_interactive_card(entry["chat_id"], content)
                            else:
                                await feishu.send_post(entry["chat_id"], content)

                    # Send final response
                    header = f"⏰ **{entry['job_name']}**"
                    body = optimize_markdown_style(entry["response"], card_version=2)
                    text = f"{header}\n\n{body}"
                    if should_use_card(body):
                        await feishu.send_interactive_card(entry["chat_id"], text)
                    else:
                        await feishu.send_post(entry["chat_id"], text)

                    pending_store.remove(pending_key)
                    sent_this_tick.add(pending_key)
                    logger.info(f"[cron] Pending notification delivered for job {pending_key}")
                except Exception as e:
                    logger.warning(f"[cron] Pending notification delivery failed: {e}")

        due = get_due_jobs(self.data_dir)
        # Filter out jobs that are already running (prevents overlap if job takes >60s)
        due = [j for j in due if j["id"] not in self._running_jobs]
        if not due:
            return

        logger.info(f"[cron] {len(due)} job(s) due")
        for job in due:
            self._running_jobs.add(job["id"])
        tasks = [
            _run_job(job, self.config, self.data_dir, self._running_jobs)
            for job in due
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ─── MCP Tools ───────────────────────────────────────────────────────────────

CRON_TOOLS = [
    {
        "name": "cc_cron_create",
        "description": "Create a new scheduled cron job. The job will run automatically at the specified schedule and send results to the Feishu chat where it was created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": "When to execute the job: '30m' (once in 30min), 'every 1h' (hourly), 'every day 9am' (daily at 9am), '0 9 * * *' (cron expression), '2026-04-20T14:00' (once at specific time)"
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt/question to ask Claude when the job runs. Make it concrete and actionable."
                },
                "name": {
                    "type": "string",
                    "description": "Optional friendly name for this job (e.g. 'Daily standup reminder'). Defaults to first 50 chars of prompt."
                },
                "repeat": {
                    "type": "integer",
                    "description": "Maximum number of times to run. Omit or set to null for infinite. One-shot schedules default to 1."
                },
                "verbose": {
                    "type": "boolean",
                    "description": "If true, stream tool calls to Feishu in real-time as the job runs. Default false."
                },
                "notify_at": {
                    "type": "string",
                    "description": "Optional: separate schedule for when to send the notification to Feishu. Example: '0 8 * * *' means execute at 'schedule' but notify at 8am. If not set, notify immediately after execution."
                }
            },
            "required": ["schedule", "prompt"]
        }
    },
    {
        "name": "cc_cron_list",
        "description": "List all cron jobs with their current status, next run time, and last result.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "cc_cron_delete",
        "description": "Delete a cron job by its ID. The job stops running and its output history is removed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the job to delete."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "cc_cron_pause",
        "description": "Pause a cron job. A paused job will not run automatically but can be resumed later.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the job to pause."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "cc_cron_resume",
        "description": "Resume a paused cron job. It will resume its normal schedule from now.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the job to resume."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "cc_cron_trigger",
        "description": "Immediately trigger a cron job to run once, outside of its normal schedule. The job's next scheduled run is not affected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the job to trigger."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "cc_cron_logs",
        "description": "View the execution history and output summary of a cron job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The ID of the job to view logs for."
                }
            },
            "required": ["job_id"]
        }
    },
]
