"""Tests for codex_exec.py — CodexStreamEvent mapping and CodexSessionTailingRunner."""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from supercc.claude.codex_exec import (
    _map_session_entry,
    build_codex_exec_cmd,
    CodexStreamEvent,
    CodexRunTailingRunner,
    CodexSessionTailingRunner,
    find_codex_session,
    map_codex_jsonl_event,
    prompt_hash,
)


class TestMapSessionEntry:
    """Test session JSONL entry → CodexStreamEvent mapping."""

    def _entry(self, msg_type: str, payload: dict) -> dict:
        return {"timestamp": "2026-04-26T10:00:00Z", "type": msg_type, "payload": payload}

    def test_task_started(self):
        entry = self._entry("event_msg", {"type": "task_started", "turn_id": "1"})
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "started"
        assert ev.title == "Codex started"

    def test_agent_message(self):
        entry = self._entry("event_msg", {"type": "agent_message", "message": "Hello world"})
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "text"
        assert ev.content == "Hello world"

    def test_agent_message_dict(self):
        entry = self._entry("event_msg", {"type": "agent_message", "message": {"text": "Hi there"}})
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "text"
        assert ev.content == "Hi there"

    def test_task_complete(self):
        entry = self._entry("event_msg", {"type": "task_complete"})
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "finished"
        assert ev.is_final is True

    def test_task_failed(self):
        entry = self._entry("event_msg", {"type": "task_failed", "error": "something went wrong"})
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "error"
        assert "something went wrong" in ev.content

    def test_exec_command(self):
        args = json.dumps({"cmd": "ls -la", "workdir": "/tmp"})
        entry = self._entry("response_item", {
            "type": "function_call",
            "name": "exec_command",
            "arguments": args,
            "call_id": "call_123",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "command_execution"
        assert ev.command == "ls -la"
        assert ev.tool_input == args

    def test_exec_command_malformed_args(self):
        entry = self._entry("response_item", {
            "type": "function_call",
            "name": "exec_command",
            "arguments": "not json",
            "call_id": "call_123",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "command_execution"

    def test_mcp_tool_call(self):
        entry = self._entry("response_item", {
            "type": "function_call",
            "name": "mcp_codex_codex",
            "arguments": '{"prompt": "hello"}',
            "call_id": "call_456",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "tool_use"
        assert ev.tool_name == "mcp_codex_codex"

    def test_generic_function_call(self):
        entry = self._entry("response_item", {
            "type": "function_call",
            "name": "Read",
            "arguments": '{"file_path": "/tmp/test.py"}',
            "call_id": "call_789",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "tool_use"
        assert ev.tool_name == "Read"

    def test_function_call_output(self):
        entry = self._entry("response_item", {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "Chunk ID: abc\nWall time: 0.0000 seconds\nProcess exited with code 0\nOutput:\nhello world",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "command_output"
        assert ev.exit_code == 0

    def test_function_call_output_error(self):
        entry = self._entry("response_item", {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "Process exited with code 1\nOutput:\nerror!",
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.exit_code == 1

    def test_reasoning_with_summary(self):
        entry = self._entry("response_item", {
            "type": "reasoning",
            "summary": ["thinking step 1", "thinking step 2"],
        })
        ev = _map_session_entry(entry)
        assert ev is not None
        assert ev.type == "reasoning"

    def test_reasoning_empty(self):
        entry = self._entry("response_item", {"type": "reasoning", "summary": []})
        ev = _map_session_entry(entry)
        assert ev is None  # empty reasoning filtered out

    def test_unknown_payload_type(self):
        entry = self._entry("event_msg", {"type": "something_new"})
        ev = _map_session_entry(entry)
        assert ev is None

    def test_unknown_msg_type(self):
        entry = {"timestamp": "2026-04-26T10:00:00Z", "type": "unknown_type", "payload": {}}
        ev = _map_session_entry(entry)
        assert ev is None


def test_build_codex_exec_cmd_uses_json_and_workspace_flags():
    cmd = build_codex_exec_cmd(
        "codex",
        "gpt-5.5",
        "workspace-write",
        "on-request",
        "/tmp/project",
        "Say OK",
    )

    assert cmd[:2] == ["codex", "exec"]
    assert ["-m", "gpt-5.5"] == cmd[2:4]
    assert "--skip-git-repo-check" in cmd
    assert "--json" in cmd
    assert cmd[-1] == "Say OK"


def test_map_codex_jsonl_agent_message():
    ev = map_codex_jsonl_event({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "OK"},
    })

    assert ev is not None
    assert ev.type == "text"
    assert ev.content == "OK"


def test_map_codex_jsonl_command_started():
    ev = map_codex_jsonl_event({
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/zsh -lc 'git status --short'",
            "status": "in_progress",
        },
    })

    assert ev is not None
    assert ev.type == "command_execution"
    assert "git status --short" in ev.command


def test_map_codex_jsonl_command_completed():
    ev = map_codex_jsonl_event({
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/zsh -lc 'git status --short'",
            "aggregated_output": " M supercc/adapter/feishu/message_handler.py",
            "exit_code": 0,
            "status": "completed",
        },
    })

    assert ev is not None
    assert ev.type == "command_output"
    assert "message_handler.py" in ev.content
    assert ev.exit_code == 0


def test_map_codex_jsonl_tool_call_started():
    ev = map_codex_jsonl_event({
        "type": "item.started",
        "item": {
            "id": "item_2",
            "type": "function_call",
            "name": "Read",
            "arguments": '{"file_path": "/tmp/test.py"}',
        },
    })

    assert ev is not None
    assert ev.type == "tool_use"
    assert ev.tool_name == "Read"


def test_map_codex_jsonl_turn_completed():
    ev = map_codex_jsonl_event({
        "type": "turn.completed",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })

    assert ev is not None
    assert ev.type == "finished"
    assert ev.is_final is True
    assert ev.exit_code == 0


class TestFindCodexSession:
    def test_find_most_recent(self, tmp_path, monkeypatch):
        """find_codex_session returns most recent file by mtime when no cwd match."""
        # Create a fake session dir structure
        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "2026").mkdir()
        (session_dir / "2026" / "04").mkdir()
        (session_dir / "2026" / "04" / "26").mkdir()

        old_file = session_dir / "2026" / "04" / "26" / "old.jsonl"
        old_file.write_text('{"type":"event_msg","payload":{}}')

        new_file = session_dir / "2026" / "04" / "26" / "new.jsonl"
        new_file.write_text('{"type":"event_msg","payload":{}}')

        # Make new_file appear newer
        import time
        time.sleep(0.01)
        new_file.touch()

        result = find_codex_session(session_dir, since=0)
        assert result == new_file

    def test_returns_none_when_no_sessions(self, tmp_path):
        """Returns None when session dir has no files."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True)
        result = find_codex_session(session_dir, since=0)
        assert result is None

    def test_filters_by_since_timestamp(self, tmp_path):
        """Only considers sessions newer than 'since' timestamp."""
        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "2026").mkdir()
        (session_dir / "2026" / "04").mkdir()
        (session_dir / "2026" / "04" / "26").mkdir()

        old_file = session_dir / "2026" / "04" / "26" / "old.jsonl"
        old_file.write_text('{"type":"event_msg","payload":{}}')
        # Make it appear old
        import time
        old_mtime = time.time() - 100
        import os
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = session_dir / "2026" / "04" / "26" / "new.jsonl"
        new_file.write_text('{"type":"event_msg","payload":{}}')

        # Ask for sessions newer than old_mtime + 1 second
        result = find_codex_session(session_dir, since=old_mtime + 1)
        assert result == new_file


class TestCodexSessionTailingRunner:
    """Test CodexSessionTailingRunner with a real temporary session file."""

    @pytest.mark.anyio
    async def test_tail_emits_events(self, tmp_path):
        """Tailing a session file emits mapped events."""
        session_file = tmp_path / "test_session.jsonl"
        session_file.write_text("")

        events = []

        async def on_event(ev: CodexStreamEvent):
            events.append(ev)

        runner = CodexSessionTailingRunner(
            session_path=str(session_file),
            on_codex_event=on_event,
            poll_interval=0.05,
        )

        # Write some entries
        with open(session_file, "w") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Hello"}}) + "\n")
            f.write(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"}}) + "\n")

        # Start tailing (it reads the file immediately then waits)
        task = asyncio.create_task(runner.run())
        # Give it time to process
        await asyncio.sleep(0.15)
        runner.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        types = [e.type for e in events]
        assert "started" in types
        assert "text" in types
        assert "command_execution" in types


class TestCodexRunTailingRunner:
    @pytest.mark.anyio
    async def test_finds_manifest_and_tails_events(self, tmp_path):
        runs_dir = tmp_path / ".supercc" / "codex_runs"
        runs_dir.mkdir(parents=True)
        events_path = runs_dir / "run-1.jsonl"
        manifest_path = runs_dir / "run-1.manifest.json"
        prompt = "Say OK"

        events_path.write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}) + "\n",
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps({
                "run_id": "run-1",
                "status": "completed",
                "cwd": str(tmp_path),
                "prompt_hash": prompt_hash(prompt),
                "events_path": str(events_path),
                "started_at": 100,
            }),
            encoding="utf-8",
        )

        events = []

        async def on_event(ev: CodexStreamEvent):
            events.append(ev)

        runner = CodexRunTailingRunner(
            cwd=str(tmp_path),
            prompt=prompt,
            on_codex_event=on_event,
            started_after=0,
            poll_interval=0.01,
            find_timeout=1,
        )
        await runner.run()

        assert [event.content for event in events if event.type == "text"] == ["OK"]
