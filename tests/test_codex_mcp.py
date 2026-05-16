import json
from pathlib import Path

import pytest


def test_codex_config_loads_defaults(tmp_path):
    from supercc.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
channels:
  feishu:
    app_id: cli_test
    app_secret: secret
auth:
  allowed_users: []
claude:
  approved_directory: /tmp/project
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))

    assert cfg.codex.enabled is True
    assert cfg.codex.cli_path == ""
    assert cfg.codex.model == "gpt-5.5"
    assert cfg.codex.sandbox == "workspace-write"
    assert cfg.codex.approval == "on-request"
    assert cfg.codex.auto_configure_mcp is True
    assert cfg.codex.capture.capture_mode is True


def test_disabled_codex_does_not_write_settings(tmp_path):
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import ensure_codex_mcp_configured

    settings_path = tmp_path / "settings.json"
    cfg = CodexMcpConfig(enabled=False)

    status = ensure_codex_mcp_configured(
        cfg,
        settings_path=settings_path,
        which=lambda _: "/usr/local/bin/codex",
    )

    assert status.state == "disabled"
    assert not settings_path.exists()


def test_legacy_codex_model_is_migrated(tmp_path):
    from supercc.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
channels:
  feishu:
    app_id: cli_test
    app_secret: secret
auth:
  allowed_users: []
claude:
  approved_directory: /tmp/project
codex:
  model: gpt-5.5-codex
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))

    assert cfg.codex.model == "gpt-5.5"
    assert "gpt-5.5-codex" not in cfg_path.read_text(encoding="utf-8")


def test_ensure_codex_mcp_writes_config_and_preserves_existing_servers(tmp_path):
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import ensure_codex_mcp_configured

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "env": {"ANTHROPIC_MODEL": "claude-opus-4-5"},
                "mcpServers": {
                    "SuperCC": {"command": "python", "args": ["-m", "supercc.mcp"]}
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = CodexMcpConfig(
        enabled=True,
        cli_path="",
        model="gpt-5.5",
        sandbox="workspace-write",
        approval="on-request",
    )

    status = ensure_codex_mcp_configured(
        cfg,
        settings_path=settings_path,
        which=lambda _: "/opt/homebrew/bin/codex",
    )

    assert status.state == "configured"
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert raw["env"]["ANTHROPIC_MODEL"] == "claude-opus-4-5"
    assert raw["mcpServers"]["SuperCC"]["command"] == "python"
    codex_server = raw["mcpServers"]["codex"]
    assert "supercc-codex-mcp-server" in codex_server["command"]
    assert "--cli-path" in codex_server["args"]
    assert "/opt/homebrew/bin/codex" in codex_server["args"]
    assert "--model" in codex_server["args"]
    assert "gpt-5.5" in codex_server["args"]
    assert "--sandbox" in codex_server["args"]
    assert "workspace-write" in codex_server["args"]


def test_explicit_windows_codex_cmd_path_is_accepted(tmp_path):
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import ensure_codex_mcp_configured

    settings_path = tmp_path / "settings.json"
    cfg = CodexMcpConfig(
        enabled=True,
        cli_path=r"C:\Users\x\AppData\Roaming\npm\codex.cmd",
    )

    status = ensure_codex_mcp_configured(
        cfg,
        settings_path=settings_path,
        which=lambda _: None,
    )

    assert status.state == "configured"
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "supercc-codex-mcp-server" in raw["mcpServers"]["codex"]["command"]
    assert r"C:\Users\x\AppData\Roaming\npm\codex.cmd" in raw["mcpServers"]["codex"]["args"]


def test_existing_non_codex_server_named_codex_is_reported_as_conflict(tmp_path):
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import ensure_codex_mcp_configured

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"mcpServers": {"codex": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )
    cfg = CodexMcpConfig(enabled=True)

    status = ensure_codex_mcp_configured(
        cfg,
        settings_path=settings_path,
        which=lambda _: "/usr/local/bin/codex",
    )

    assert status.state == "conflict"
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["codex"]["command"] == "node"


def test_existing_windows_codex_server_can_be_refreshed(tmp_path):
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import ensure_codex_mcp_configured

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "codex": {
                        "command": r"C:\Users\x\AppData\Roaming\npm\codex.cmd",
                        "args": ["mcp-server"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = CodexMcpConfig(
        enabled=True,
        cli_path=r"C:\Users\x\AppData\Roaming\npm\codex.cmd",
        model="gpt-5.5",
    )

    status = ensure_codex_mcp_configured(
        cfg,
        settings_path=settings_path,
        which=lambda _: None,
    )

    assert status.state == "configured"
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "supercc-codex-mcp-server" in raw["mcpServers"]["codex"]["command"]
    assert "gpt-5.5" in raw["mcpServers"]["codex"]["args"]


def test_format_codex_status_for_missing_cli():
    from supercc.claude.codex_mcp import CodexMcpStatus, format_codex_status

    status = CodexMcpStatus(
        state="missing_cli",
        enabled=True,
        cli_path=None,
        model="gpt-5.5",
        sandbox="workspace-write",
        approval="on-request",
        message="未找到 codex CLI",
    )

    text = format_codex_status(status)

    assert "missing_cli" in text
    assert "gpt-5.5" in text
    assert "config.json" in text
    assert "gpt-5.4" in text


def test_format_codex_models_lists_recommended_models():
    from supercc.claude.codex_mcp import format_codex_models

    text = format_codex_models()

    assert "gpt-5.5" in text
    assert "gpt-5.4" in text


def test_format_codex_availability_is_short():
    from supercc.claude.codex_mcp import CodexMcpStatus, format_codex_availability, is_codex_available

    ready = CodexMcpStatus("configured", True, "/usr/local/bin/codex", "gpt-5.5", "workspace-write", "on-request")
    missing = CodexMcpStatus("missing_cli", True, None, "gpt-5.5", "workspace-write", "on-request", "未找到 codex CLI")

    assert is_codex_available(ready) is True
    assert format_codex_availability(ready).startswith("Codex 可用")
    assert is_codex_available(missing) is False
    assert format_codex_availability(missing).startswith("Codex 不可用")


def test_codex_guide_is_only_injected_when_enabled():
    from supercc.config import CodexMcpConfig
    from supercc.claude.codex_mcp import CodexMcpStatus, get_codex_mcp_guide

    assert get_codex_mcp_guide(CodexMcpConfig(enabled=False)) == ""

    status = CodexMcpStatus("configured", True, "/usr/local/bin/codex", "gpt-5.5", "workspace-write", "on-request")
    guide = get_codex_mcp_guide(CodexMcpConfig(enabled=True, model="gpt-5.5"), status)

    assert "【Codex 子代理】" in guide
    assert "当前状态" in guide
    assert "Codex 可用" in guide
    assert "GPT-5.5" in guide
    assert "model 参数" in guide
    assert "gpt-5.4" in guide
    assert "在开始普通分析或调用非 Codex 工具前" in guide
    assert "codex" in guide


def test_claude_options_include_codex_mcp_when_enabled(tmp_path):
    from supercc.config import init_config
    from supercc.claude.integration import ClaudeIntegration

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
channels:
  feishu:
    app_id: cli_test
    app_secret: secret
auth:
  allowed_users: []
claude:
  approved_directory: /tmp/project
codex:
  enabled: true
  cli_path: /usr/local/bin/codex
  model: gpt-5.5
""",
        encoding="utf-8",
    )
    init_config(str(cfg_path))

    integration = ClaudeIntegration(approved_directory="/tmp/project")
    integration._init_options()

    codex_server = integration._options.mcp_servers["codex"]
    # Uses SuperCC's custom MCP server that wraps CodexExecRunner
    assert "supercc-codex-mcp-server" in codex_server["command"]
    assert "--model" in codex_server["args"]
    assert "gpt-5.5" in codex_server["args"]
    assert "--cwd" in codex_server["args"]
    assert "/tmp/project" in codex_server["args"]


def test_codex_mcp_tool_name_matches_mcp_qualified_name():
    from supercc.channels.feishu.message_handler import _is_codex_tool_name

    assert _is_codex_tool_name("codex") is True
    assert _is_codex_tool_name("mcp__codex__codex") is True
    assert _is_codex_tool_name("mcp__SuperCC__MemorySearchProj") is False


def test_codex_card_uses_codex_title():
    from supercc.channels.feishu.format.agent_card import format_codex_card

    card = format_codex_card("text", "OK")
    content = card["body"]["elements"][0]["content"]

    assert content.startswith("## 🤖 Codex - 🧩 content")
    assert "## 🤖 Agent" not in content
    assert "content:" not in content


def test_codex_tool_card_uses_tool_title():
    from supercc.channels.feishu.format.agent_card import format_codex_card

    card = format_codex_card("tool_use", '{"file_path":"/tmp/test.py"}', {"tool_name": "Read"})
    content = card["body"]["elements"][0]["content"]

    assert content.startswith("## 🤖 Codex - 📖 Read")


@pytest.mark.anyio
async def test_codex_mcp_exec_detaches_child_stdin(monkeypatch, tmp_path):
    import asyncio

    from supercc.claude import codex_mcp_server

    captured = {}

    class FakeStdout:
        def __aiter__(self):
            self.lines = iter([
                b'{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
                b'{"type":"turn.completed"}\n',
            ])
            return self

        async def __anext__(self):
            try:
                return next(self.lines)
            except StopIteration:
                raise StopAsyncIteration

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProcess:
        stdout = FakeStdout()
        stderr = FakeStderr()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await codex_mcp_server._run_codex_exec(
        "codex",
        "gpt-5.5",
        "workspace-write",
        "on-request",
        str(tmp_path),
        "Say OK only.",
    )

    assert result["last_response"] == "OK"
    assert result["events_count"] == 2
    assert Path(result["events_path"]).exists()
    assert Path(result["manifest_path"]).exists()
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert "--json" in captured["cmd"]
    assert "--skip-git-repo-check" in captured["cmd"]


@pytest.mark.anyio
async def test_codex_mcp_exec_cleans_up_subprocess_on_cancel(monkeypatch, tmp_path):
    import asyncio

    from supercc.claude import codex_mcp_server

    class FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            raise StopAsyncIteration

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = None
            self.terminate_called = False
            self.kill_called = False
            self._wait_event = asyncio.Event()
            self.pid = 4242

        async def wait(self):
            await self._wait_event.wait()
            return self.returncode

        def terminate(self):
            self.terminate_called = True
            self.returncode = -15
            self._wait_event.set()

        def kill(self):
            self.kill_called = True
            self.returncode = -9
            self._wait_event.set()

    fake_proc = FakeProcess()

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_mcp_server, "_subprocess_group_kwargs", lambda: {})
    async def fake_terminate_proc_tree(proc, *, force=False):
        if force:
            proc.kill()
        else:
            proc.terminate()
    monkeypatch.setattr(codex_mcp_server, "_terminate_proc_tree", fake_terminate_proc_tree)

    task = asyncio.create_task(
        codex_mcp_server._run_codex_exec(
            "codex",
            "gpt-5.5",
            "workspace-write",
            "on-request",
            str(tmp_path),
            "Say OK only.",
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    manifests = sorted((tmp_path / ".supercc" / "codex_runs").glob("*.manifest.json"))
    assert manifests
    data = json.loads(manifests[-1].read_text(encoding="utf-8"))
    assert data["status"] == "cancelled"
    assert fake_proc.terminate_called or fake_proc.kill_called


@pytest.mark.anyio
async def test_process_message_without_codex_bridge_attribute_does_not_crash():
    from unittest.mock import AsyncMock, MagicMock

    from supercc.channels.feishu.client import IncomingMessage
    from supercc.channels.feishu.message_handler import MessageHandler
    from supercc.config import AuthConfig, ChannelsConfig, ClaudeConfig, CodexMcpConfig, Config

    session = MagicMock()
    session.project_path = "/tmp/project"
    session.chat_id = "oc_chat"

    handler = MessageHandler(
        feishu_client=MagicMock(),
        authenticator=MagicMock(authenticate=MagicMock(return_value=MagicMock(authorized=True))),
        validator=MagicMock(),
        claude=MagicMock(),
        session_manager=MagicMock(get_active_session=MagicMock(return_value=session)),
        formatter=MagicMock(),
        approved_directory="/tmp/project",
        config=Config(
            channels=ChannelsConfig(),
            auth=AuthConfig(),
            claude=ClaudeConfig(approved_directory="/tmp/project"),
            codex=CodexMcpConfig(enabled=True),
        ),
    )
    handler.memory_manager.inject_context = MagicMock(return_value="")
    handler._run_query = AsyncMock()

    message = IncomingMessage(
        message_id="om_1",
        chat_id="oc_chat",
        user_open_id="ou_user",
        content="hello",
        message_type="text",
        create_time="1",
    )

    await handler._process_message(message)

    handler._run_query.assert_awaited_once()
