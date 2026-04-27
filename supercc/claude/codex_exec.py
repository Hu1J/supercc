"""Codex exec helpers, JSONL event mapping, and run-file tailing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

CODEX_RUNS_DIR = "codex_runs"
CodexEventCallback = Callable[["CodexStreamEvent"], Any]


@dataclass
class CodexStreamEvent:
    """Unified event type for Codex exec output."""
    type: str
    title: str = ""
    content: str = ""
    is_final: bool = False
    exit_code: int | None = None
    command: str = ""
    tool_name: str = ""
    tool_input: str = ""
    file_path: str = ""


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


def codex_runs_dir(cwd: str | Path) -> Path:
    return Path(cwd).resolve() / ".supercc" / CODEX_RUNS_DIR


def build_codex_exec_cmd(
    cli_path: str,
    model: str,
    sandbox: str,
    approval: str,
    cwd: str,
    prompt: str,
) -> list[str]:
    cmd = [
        cli_path,
        "exec",
        "-m", model,
        "-s", sandbox,
        "-C", cwd,
        "-c", f'approval_policy="{approval}"',
        "--skip-git-repo-check",
        "--json",
        prompt,
    ]
    return cmd


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "message", "content", "output"):
            if key in value:
                return _extract_text(value.get(key))
        return _json_dumps(value)
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    return str(value)


def _map_function_call(payload: dict) -> CodexStreamEvent | None:
    name = str(payload.get("name") or payload.get("tool_name") or "")
    arguments = payload.get("arguments") or payload.get("input") or ""
    if isinstance(arguments, dict):
        arguments_text = _json_dumps(arguments)
        arguments_dict = arguments
    else:
        arguments_text = str(arguments or "")
        try:
            arguments_dict = json.loads(arguments_text) if arguments_text else {}
        except json.JSONDecodeError:
            arguments_dict = {}

    if name in {"exec_command", "shell", "bash", "Bash"}:
        command = (
            arguments_dict.get("cmd")
            or arguments_dict.get("command")
            or arguments_text
        )
        return CodexStreamEvent(
            type="command_execution",
            title="Codex command",
            command=str(command or ""),
            tool_name=name,
            tool_input=arguments_text,
        )

    return CodexStreamEvent(
        type="tool_use",
        title=f"Codex tool: {name or 'unknown'}",
        tool_name=name,
        tool_input=arguments_text,
    )


def _map_function_output(payload: dict) -> CodexStreamEvent | None:
    output = _extract_text(payload.get("output") or payload.get("content"))
    exit_code = None
    match = re.search(r"Process exited with code\s+(-?\d+)", output)
    if match:
        try:
            exit_code = int(match.group(1))
        except ValueError:
            exit_code = None
    return CodexStreamEvent(
        type="command_output",
        title="Codex command output",
        content=output,
        exit_code=exit_code,
    )


def _map_command_execution_payload(payload: dict, *, completed: bool) -> CodexStreamEvent | None:
    command = str(payload.get("command") or "")
    output = _extract_text(payload.get("aggregated_output") or payload.get("output") or payload.get("content"))
    exit_code = payload.get("exit_code")
    try:
        exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        exit_code = None

    if completed:
        return CodexStreamEvent(
            type="command_output",
            title="Codex command output",
            content=output,
            command=command,
            exit_code=exit_code,
        )

    return CodexStreamEvent(
        type="command_execution",
        title="Codex command",
        command=command,
        content=output,
        exit_code=exit_code,
        tool_name="command_execution",
    )


def _map_response_item_payload(payload: dict, *, completed: bool = True) -> CodexStreamEvent | None:
    item_type = payload.get("type")

    if item_type == "agent_message":
        text = _extract_text(payload.get("text") or payload.get("message") or payload.get("content"))
        if text:
            return CodexStreamEvent(type="text", title="Codex", content=text)
        return None

    if item_type in {"function_call", "tool_call"}:
        return _map_function_call(payload)

    if item_type in {"function_call_output", "tool_call_output"}:
        return _map_function_output(payload)

    if item_type == "command_execution":
        return _map_command_execution_payload(payload, completed=completed)

    if item_type == "reasoning":
        text = _extract_text(payload.get("summary") or payload.get("content"))
        if text:
            return CodexStreamEvent(type="reasoning", title="Codex reasoning", content=text)
        return None

    if item_type in {"file_change", "file_changed"}:
        path = str(payload.get("path") or payload.get("file_path") or "")
        return CodexStreamEvent(
            type="file_change",
            title="Codex file change",
            content=_extract_text(payload.get("content") or payload.get("diff")),
            file_path=path,
        )

    return None


def _map_session_entry(entry: dict) -> CodexStreamEvent | None:
    """Map older Codex session JSONL entries to CodexStreamEvent."""
    msg_type = entry.get("type")
    payload = entry.get("payload") or {}

    if msg_type == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "task_started":
            return CodexStreamEvent(type="started", title="Codex started")
        if payload_type == "agent_message":
            text = _extract_text(payload.get("message"))
            return CodexStreamEvent(type="text", title="Codex", content=text) if text else None
        if payload_type == "task_complete":
            return CodexStreamEvent(type="finished", title="Codex finished", is_final=True)
        if payload_type == "task_failed":
            return CodexStreamEvent(
                type="error",
                title="Codex error",
                content=_extract_text(payload.get("error")),
                is_final=True,
            )
        return None

    if msg_type == "response_item":
        return _map_response_item_payload(payload)

    return None


def map_codex_jsonl_event(entry: dict) -> CodexStreamEvent | None:
    """Map `codex exec --json` entries to CodexStreamEvent."""
    event_type = entry.get("type")

    if event_type == "thread.started":
        return CodexStreamEvent(
            type="started",
            title="Codex started",
            content=f"thread_id={entry.get('thread_id', '')}".strip(),
        )
    if event_type == "turn.started":
        return CodexStreamEvent(type="started", title="Codex turn started")
    if event_type == "item.started":
        item = entry.get("item") or {}
        return _map_response_item_payload(item, completed=False)
    if event_type == "item.completed":
        item = entry.get("item") or {}
        return _map_response_item_payload(item, completed=True)
    if event_type == "turn.completed":
        usage = entry.get("usage") or {}
        content = _json_dumps({"usage": usage}) if usage else ""
        return CodexStreamEvent(
            type="finished",
            title="Codex finished",
            content=content,
            is_final=True,
            exit_code=0,
        )
    if event_type == "error":
        return CodexStreamEvent(
            type="error",
            title="Codex error",
            content=_extract_text(entry.get("message") or entry.get("error")),
        )

    return _map_session_entry(entry)


def find_codex_session(session_dir: Path, since: float = 0) -> Path | None:
    if not session_dir.exists():
        return None
    files = [
        path for path in session_dir.rglob("*.jsonl")
        if path.is_file() and path.stat().st_mtime >= since
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


class CodexJsonlTailingRunner:
    """Tail a SuperCC Codex run JSONL file and emit mapped events."""

    def __init__(
        self,
        events_path: str | Path,
        on_codex_event: CodexEventCallback,
        *,
        manifest_path: str | Path | None = None,
        poll_interval: float = 0.25,
        idle_timeout: float = 600,
    ):
        self.events_path = Path(events_path)
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.on_codex_event = on_codex_event
        self.poll_interval = poll_interval
        self.idle_timeout = idle_timeout
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self) -> None:
        offset = 0
        last_activity = time.time()
        buffer = ""

        while not self._cancelled:
            if self.events_path.exists():
                with self.events_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
                if chunk:
                    last_activity = time.time()
                    buffer += chunk
                    lines = buffer.splitlines(keepends=True)
                    buffer = ""
                    if lines and not lines[-1].endswith(("\n", "\r")):
                        buffer = lines.pop()
                    for raw_line in lines:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event = map_codex_jsonl_event(entry)
                        if event:
                            await self.on_codex_event(event)

            status = self._manifest_status()
            if status in {"completed", "failed", "timeout"} and not buffer:
                break
            if time.time() - last_activity > self.idle_timeout:
                logger.warning("[codex_tail] idle timeout for %s", self.events_path)
                break
            await asyncio.sleep(self.poll_interval)

    def _manifest_status(self) -> str:
        if not self.manifest_path or not self.manifest_path.exists():
            return ""
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return str(data.get("status") or "")
        except Exception:
            return ""


class CodexRunTailingRunner:
    """Find a live Codex run manifest and tail its JSONL events."""

    def __init__(
        self,
        cwd: str,
        prompt: str,
        on_codex_event: CodexEventCallback,
        *,
        started_after: float = 0,
        poll_interval: float = 0.25,
        find_timeout: float = 20,
        idle_timeout: float = 600,
    ):
        self.cwd = cwd
        self.prompt = prompt
        self.on_codex_event = on_codex_event
        self.started_after = started_after
        self.poll_interval = poll_interval
        self.find_timeout = find_timeout
        self.idle_timeout = idle_timeout
        self._cancelled = False
        self._tailer: CodexJsonlTailingRunner | None = None

    def cancel(self) -> None:
        self._cancelled = True
        if self._tailer:
            self._tailer.cancel()

    async def run(self) -> None:
        manifest = await self._wait_for_manifest()
        if not manifest:
            logger.warning("[codex_tail] no matching run manifest found")
            return

        data = json.loads(manifest.read_text(encoding="utf-8"))
        events_path = Path(data.get("events_path") or "")
        if not events_path.is_absolute():
            events_path = manifest.parent / events_path
        self._tailer = CodexJsonlTailingRunner(
            events_path,
            self.on_codex_event,
            manifest_path=manifest,
            poll_interval=self.poll_interval,
            idle_timeout=self.idle_timeout,
        )
        await self._tailer.run()

    async def _wait_for_manifest(self) -> Path | None:
        deadline = time.time() + self.find_timeout
        target_hash = prompt_hash(self.prompt)
        runs_dir = codex_runs_dir(self.cwd)

        while not self._cancelled and time.time() < deadline:
            candidates = []
            if runs_dir.exists():
                for path in runs_dir.glob("*.manifest.json"):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if data.get("prompt_hash") != target_hash:
                        continue
                    if float(data.get("started_at") or 0) + 2 < self.started_after:
                        continue
                    candidates.append((float(data.get("started_at") or 0), path))
            if candidates:
                return max(candidates, key=lambda item: item[0])[1]
            await asyncio.sleep(self.poll_interval)
        return None


class CodexSessionTailingRunner(CodexJsonlTailingRunner):
    """Backward-compatible tailer for Codex session files used by older tests."""

    def __init__(
        self,
        session_path: str,
        on_codex_event: CodexEventCallback,
        poll_interval: float = 0.25,
    ):
        super().__init__(
            session_path,
            on_codex_event,
            poll_interval=poll_interval,
            idle_timeout=600,
        )


class CodexExecRunner:
    """Run `codex exec --json` and emit mapped events."""

    def __init__(
        self,
        cli_path: str,
        model: str,
        sandbox: str,
        approval: str,
        cwd: str,
        on_codex_event: CodexEventCallback,
    ):
        self.cli_path = cli_path
        self.model = model
        self.sandbox = sandbox
        self.approval = approval
        self.cwd = cwd
        self.on_codex_event = on_codex_event

    async def run(self, prompt: str) -> int:
        cmd = build_codex_exec_cmd(
            self.cli_path,
            self.model,
            self.sandbox,
            self.approval,
            self.cwd,
            prompt,
        )
        logger.info("[codex_exec] starting: %s ...", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

        async def _drain_stderr() -> None:
            try:
                stderr_data = await proc.stderr.read()
                if stderr_data:
                    logger.debug("[codex_exec] stderr: %s", stderr_data.decode("utf-8", errors="replace")[:500])
            except Exception:
                pass

        stderr_task = asyncio.create_task(_drain_stderr())

        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = map_codex_jsonl_event(entry)
            if event:
                await self.on_codex_event(event)

        returncode = await proc.wait()
        await stderr_task
        return returncode if returncode is not None else -1
