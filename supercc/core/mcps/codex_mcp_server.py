"""SuperCC Codex MCP server.

The MCP process runs Codex, writes its JSONL stream into project-local run
files, and returns a compact summary to Claude Code. SuperCC tails those files
from the parent process and owns all Feishu rendering.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from supercc.core.mcps.codex_exec import (
    build_codex_exec_cmd,
    codex_runs_dir,
    map_codex_jsonl_event,
    prompt_hash,
)

_default_model = "gpt-5.5"
_default_sandbox = "workspace-write"
_default_approval = "on-request"
_default_cwd = "."
_default_timeout = 300
_default_cli_path = "codex"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _prepare_exec_cwd(cwd_path: Path) -> Path:
    if _is_ascii_path(cwd_path):
        return cwd_path
    link_dir = cwd_path / ".supercc" / "codex_workspaces"
    link_dir.mkdir(parents=True, exist_ok=True)
    link_path = link_dir / hashlib.sha256(str(cwd_path).encode("utf-8")).hexdigest()[:16]
    if link_path.exists():
        return link_path
    try:
        os.symlink(cwd_path, link_path, target_is_directory=True)
        return link_path
    except OSError:
        return cwd_path


async def _drain_stderr(proc: asyncio.subprocess.Process) -> str:
    if proc.stderr is None:
        return ""
    data = await proc.stderr.read()
    return data.decode("utf-8", errors="replace") if data else ""


def _subprocess_group_kwargs() -> dict:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


async def _terminate_proc_tree(proc: asyncio.subprocess.Process, *, force: bool = False) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            if force:
                proc.kill()
            else:
                proc.terminate()
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return
    except ProcessLookupError:
        return
    except Exception:
        if force:
            proc.kill()
        else:
            proc.terminate()


async def _stop_proc_tree(proc: asyncio.subprocess.Process, *, grace_timeout: float = 2.0) -> int | None:
    if proc.returncode is not None:
        return proc.returncode
    await _terminate_proc_tree(proc, force=False)
    try:
        return await asyncio.wait_for(proc.wait(), timeout=grace_timeout)
    except asyncio.TimeoutError:
        await _terminate_proc_tree(proc, force=True)
        return await proc.wait()


async def _run_codex_exec(
    cli_path: str,
    model: str,
    sandbox: str,
    approval: str,
    cwd: str,
    prompt: str,
    *,
    timeout: int = 300,
) -> dict:
    """Run Codex and persist its JSONL event stream for SuperCC to tail."""
    cwd_path = Path(cwd).resolve()
    exec_cwd = _prepare_exec_cwd(cwd_path)
    runs_dir = codex_runs_dir(cwd_path)
    runs_dir.mkdir(parents=True, exist_ok=True)

    run_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    events_path = runs_dir / f"{run_id}.jsonl"
    manifest_path = runs_dir / f"{run_id}.manifest.json"
    started_at = time.time()
    manifest = {
        "run_id": run_id,
        "status": "running",
        "cwd": str(cwd_path),
        "exec_cwd": str(exec_cwd),
        "prompt_hash": prompt_hash(prompt),
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
        "cli_path": cli_path,
        "events_path": str(events_path),
        "started_at": started_at,
        "updated_at": started_at,
        "last_response": "",
        "events_count": 0,
    }
    _write_json(manifest_path, manifest)

    cmd = build_codex_exec_cmd(cli_path, model, sandbox, approval, str(exec_cwd), prompt)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(exec_cwd),
        **_subprocess_group_kwargs(),
    )
    stderr_task = asyncio.create_task(_drain_stderr(proc))

    events_count = 0
    last_response = ""

    async def _consume_stdout() -> None:
        nonlocal events_count, last_response
        assert proc.stdout is not None
        with events_path.open("a", encoding="utf-8") as handle:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                handle.write(line + "\n")
                handle.flush()
                events_count += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = map_codex_jsonl_event(entry)
                if event and event.type == "text" and event.content:
                    last_response = event.content

    status = "completed"
    timed_out = False
    cancelled = False
    returncode = None
    consume_task = asyncio.create_task(_consume_stdout())
    try:
        await asyncio.wait_for(consume_task, timeout=timeout)
        returncode = await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        status = "timeout"
        returncode = await _stop_proc_tree(proc)
    except asyncio.CancelledError:
        cancelled = True
        status = "cancelled"
        consume_task.cancel()
        await asyncio.gather(consume_task, return_exceptions=True)
        returncode = await _stop_proc_tree(proc)
    finally:
        if proc.returncode is None:
            returncode = await _stop_proc_tree(proc, grace_timeout=0.5)
        stderr = await asyncio.shield(stderr_task)
        if returncode not in (0, None) and not timed_out and not cancelled:
            status = "failed"

        manifest.update({
            "status": status,
            "updated_at": time.time(),
            "finished_at": time.time(),
            "returncode": returncode,
            "events_count": events_count,
            "last_response": last_response,
            "stderr_tail": stderr[-2000:],
        })
        _write_json(manifest_path, manifest)

    if cancelled:
        raise asyncio.CancelledError

    return {
        "run_id": run_id,
        "status": status,
        "events_path": str(events_path),
        "manifest_path": str(manifest_path),
        "events_count": events_count,
        "last_response": last_response,
        "returncode": returncode,
    }


async def _handle_tools_call(tool_name: str, arguments: dict) -> dict:
    if tool_name != "codex":
        raise ValueError(f"Unknown tool: {tool_name}")

    prompt = arguments.get("prompt", "")
    if not prompt:
        raise ValueError("prompt is required")

    result = await _run_codex_exec(
        arguments.get("cli_path", _default_cli_path),
        arguments.get("model", _default_model),
        arguments.get("sandbox", _default_sandbox),
        arguments.get("approval", _default_approval),
        arguments.get("cwd", _default_cwd),
        prompt,
        timeout=int(arguments.get("timeout") or _default_timeout),
    )
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


async def _handle_request(method: str, params: dict, request_id: int | str | None) -> dict | None:
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "supercc-codex", "version": "1.0.0"},
        }
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "codex",
                    "description": "Execute a prompt using OpenAI Codex CLI and persist JSONL events for SuperCC.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "The prompt/任务 to execute with Codex"},
                            "model": {"type": "string", "description": "Codex model"},
                            "sandbox": {"type": "string", "description": "Sandbox mode"},
                            "approval": {"type": "string", "description": "Approval policy"},
                            "cli_path": {"type": "string", "description": "Codex CLI path"},
                            "cwd": {"type": "string", "description": "Working directory"},
                            "timeout": {"type": "integer", "description": "Execution timeout in seconds"},
                        },
                        "required": ["prompt"],
                    },
                }
            ]
        }
    if method == "tools/call":
        try:
            return await _handle_tools_call(params.get("name", ""), params.get("arguments", {}))
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
    return None


async def _main() -> None:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    write_lock = asyncio.Lock()

    async def send_response(msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        async with write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            request = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        req_id = request.get("id")
        if req_id is None:
            continue
        try:
            result = await _handle_request(request.get("method", ""), request.get("params", {}), req_id)
            if result is not None:
                await send_response({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            await send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            })


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperCC Codex MCP Server")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument("--approval", default="on-request")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--cli-path", default="codex")
    args = parser.parse_args()

    global _default_model, _default_sandbox, _default_approval, _default_cwd, _default_timeout, _default_cli_path
    _default_model = args.model
    _default_sandbox = args.sandbox
    _default_approval = args.approval
    _default_timeout = args.timeout
    _default_cli_path = args.cli_path
    _default_cwd = os.path.abspath(args.cwd) if args.cwd != "." else os.getcwd()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
