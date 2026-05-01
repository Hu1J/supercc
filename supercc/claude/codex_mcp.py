"""Codex MCP configuration helpers for Claude Code."""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from supercc.config import CodexMcpConfig

logger = logging.getLogger(__name__)

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CODEX_SUPPORTED_MODELS = [
    ("gpt-5.5", "推荐，能力最强"),
    ("gpt-5.4", "稳定通用"),
    ("gpt-5.4-mini", "更快更省"),
    ("gpt-5.2", "兼容保守"),
]
CODEX_MCP_GUIDE_TEMPLATE = """
【Codex】
用户要求代码审查/实现/调试/架构分析时，用 mcp__SuperCC__Codex（当前可用：{availability}）。
优先使用 {model}，用户指定模型时在调用时传入 model 参数。
普通聊天/简单问答/记忆/文件/定时任务不要调用 Codex。
"""


@dataclass
class CodexMcpStatus:
    state: str
    enabled: bool
    cli_path: str | None
    model: str
    sandbox: str
    approval: str
    message: str = ""


def resolve_codex_cli(
    cli_path: str = "",
    *,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Resolve the configured Codex CLI path without assuming a platform."""
    configured = (cli_path or "").strip()
    if configured:
        return configured
    finder = which or shutil.which
    return finder("codex")


def build_codex_mcp_server_config(cli_path: str, cfg: CodexMcpConfig) -> dict:
    """Build a Claude Code mcpServers entry for SuperCC's Codex MCP server."""
    return build_supercc_codex_mcp_config(replace(cfg, cli_path=cli_path), cwd=".")


def build_supercc_codex_mcp_config(cfg: CodexMcpConfig, cwd: str | None = None) -> dict:
    """Build a Claude Code mcpServers entry for SuperCC's Codex MCP server.

    This server runs codex exec directly and returns the result.
    """
    import shutil
    # Resolve supercc-codex-mcp-server path
    server_path = shutil.sys.executable.replace("\\", "/")
    server_dir = Path(server_path).parent
    possible_paths = [
        server_dir / "supercc-codex-mcp-server",
        server_dir / "supercc-codex-mcp-server.exe",
        Path(__file__).parent.parent.parent / "supercc-codex-mcp-server",
    ]
    cmd = "supercc-codex-mcp-server"
    for p in possible_paths:
        if p.exists():
            cmd = str(p)
            break

    return {
        "type": "stdio",
        "command": cmd,
        "args": [
            "--cli-path", cfg.cli_path or "codex",
            "--model", cfg.model or "gpt-5.5",
            "--sandbox", cfg.sandbox or "workspace-write",
            "--approval", cfg.approval or "on-request",
            "--cwd", cwd or ".",
        ],
    }


def _read_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to read Claude settings from %s", settings_path, exc_info=True)
        return {}


def _is_codex_server(server: dict) -> bool:
    command = str(server.get("command", ""))
    args = server.get("args", [])
    command_name = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if command_name in {"codex", "codex.exe", "codex.cmd", "codex.bat"} and "mcp-server" in args:
        return True
    return command_name in {
        "supercc-codex-mcp-server",
        "supercc-codex-mcp-server.exe",
        "supercc-codex-mcp-server.cmd",
        "supercc-codex-mcp-server.bat",
    }


def ensure_codex_mcp_configured(
    cfg: CodexMcpConfig,
    *,
    settings_path: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CodexMcpStatus:
    """Ensure Claude Code has a Codex MCP server entry.

    Existing non-Codex entries named "codex" are left untouched and reported as
    conflicts to avoid clobbering user-managed MCP configuration.
    """
    target_path = Path(settings_path) if settings_path is not None else CLAUDE_SETTINGS_PATH
    if not cfg.enabled:
        return CodexMcpStatus(
            state="disabled",
            enabled=False,
            cli_path=None,
            model=cfg.model,
            sandbox=cfg.sandbox,
            approval=cfg.approval,
            message="Codex MCP is disabled.",
        )
    if not cfg.auto_configure_mcp:
        cli_path = resolve_codex_cli(cfg.cli_path, which=which)
        return CodexMcpStatus(
            state="manual",
            enabled=True,
            cli_path=cli_path,
            model=cfg.model,
            sandbox=cfg.sandbox,
            approval=cfg.approval,
            message="Codex MCP auto-configuration is disabled.",
        )

    cli_path = resolve_codex_cli(cfg.cli_path, which=which)
    if not cli_path:
        return CodexMcpStatus(
            state="missing_cli",
            enabled=True,
            cli_path=None,
            model=cfg.model,
            sandbox=cfg.sandbox,
            approval=cfg.approval,
            message="未找到 codex CLI",
        )

    settings = _read_settings(target_path)
    servers = settings.setdefault("mcpServers", {})
    existing = servers.get("codex")
    desired = build_codex_mcp_server_config(cli_path, cfg)

    if existing and existing != desired and not _is_codex_server(existing):
        return CodexMcpStatus(
            state="conflict",
            enabled=True,
            cli_path=cli_path,
            model=cfg.model,
            sandbox=cfg.sandbox,
            approval=cfg.approval,
            message="Claude settings already contain a non-Codex server named codex.",
        )

    servers["codex"] = desired
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to write Claude settings to %s", target_path, exc_info=True)
        return CodexMcpStatus(
            state="error",
            enabled=True,
            cli_path=cli_path,
            model=cfg.model,
            sandbox=cfg.sandbox,
            approval=cfg.approval,
            message=str(exc),
        )

    return CodexMcpStatus(
        state="configured",
        enabled=True,
        cli_path=cli_path,
        model=cfg.model,
        sandbox=cfg.sandbox,
        approval=cfg.approval,
        message="Codex MCP configured.",
    )


def get_codex_mcp_status(
    cfg: CodexMcpConfig,
    *,
    settings_path: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> CodexMcpStatus:
    """Inspect Codex MCP status without mutating Claude settings."""
    target_path = Path(settings_path) if settings_path is not None else CLAUDE_SETTINGS_PATH
    if not cfg.enabled:
        return CodexMcpStatus("disabled", False, None, cfg.model, cfg.sandbox, cfg.approval)
    cli_path = resolve_codex_cli(cfg.cli_path, which=which)
    if not cli_path:
        return CodexMcpStatus("missing_cli", True, None, cfg.model, cfg.sandbox, cfg.approval, "未找到 codex CLI")
    settings = _read_settings(target_path)
    server = settings.get("mcpServers", {}).get("codex")
    if not server:
        return CodexMcpStatus("not_configured", True, cli_path, cfg.model, cfg.sandbox, cfg.approval)
    if not _is_codex_server(server):
        return CodexMcpStatus("conflict", True, cli_path, cfg.model, cfg.sandbox, cfg.approval)
    return CodexMcpStatus("configured", True, cli_path, cfg.model, cfg.sandbox, cfg.approval)


def format_codex_status(status: CodexMcpStatus) -> str:
    cli = status.cli_path or "未找到"
    lines = [
        "## Codex MCP",
        "",
        f"| 项目 | 值 |",
        f"|------|----|",
        f"| 状态 | `{status.state}` |",
        f"| 启用 | `{status.enabled}` |",
        f"| CLI | `{cli}` |",
        f"| 模型 | `{status.model}` |",
        f"| Sandbox | `{status.sandbox}` |",
        f"| Approval | `{status.approval}` |",
    ]
    if status.message:
        lines.extend(["", status.message])
    if status.state == "missing_cli":
        lines.extend([
            "",
            "请先安装并登录 Codex，或在 config.yaml 中设置 `codex.cli_path`。",
        ])
    elif status.state == "conflict":
        lines.extend([
            "",
            "Claude settings 中已经存在名为 `codex` 的非 Codex MCP server，请手动检查后再运行 `/codex setup`。",
        ])
    lines.extend(["", format_codex_models(include_header=False)])
    return "\n".join(lines)


def is_codex_available(status: CodexMcpStatus) -> bool:
    return status.enabled and status.cli_path is not None and status.state in {"configured", "manual"}


def format_codex_availability(status: CodexMcpStatus) -> str:
    if is_codex_available(status):
        return f"Codex 可用：`{status.model}` ({status.cli_path})"
    reason = status.message or status.state
    return f"Codex 不可用：`{reason}`"


def format_codex_models(include_header: bool = True) -> str:
    lines = []
    if include_header:
        lines.extend(["## Codex 可选模型", ""])
    lines.extend([
        "可选模型：",
        "",
        "| 模型 | 说明 |",
        "|------|------|",
    ])
    for model, note in CODEX_SUPPORTED_MODELS:
        lines.append(f"| `{model}` | {note} |")
    lines.extend([
        "",
        "在 `config.yaml` 中设置 `codex.model` 后运行 `/codex setup` 或重启 SuperCC 生效。",
    ])
    return "\n".join(lines)


def get_codex_mcp_guide(cfg: CodexMcpConfig, status: CodexMcpStatus | None = None) -> str:
    if not cfg.enabled:
        return ""
    availability = format_codex_availability(status) if status else "未知，请以 /codex available 或当前 MCP 配置检查结果为准"
    return CODEX_MCP_GUIDE_TEMPLATE.format(model=cfg.model, availability=availability)
