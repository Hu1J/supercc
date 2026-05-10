"""Codex MCP 状态 — /codex"""
from core.commands.base import CommandHandler, CommandResult


class CodexHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "codex"

    @property
    def help(self) -> str:
        return "/codex — 查看或配置 Codex MCP"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.claude.codex_mcp import (
            get_codex_mcp_status,
            format_codex_status,
            format_codex_availability,
            format_codex_models,
        )

        parts = args.strip().split()
        first = parts[0].lower() if parts else "status"

        if first == "status":
            status = get_codex_mcp_status(context.get("config"))
            return CommandResult(content=format_codex_status(status))
        if first in ("available", "availability", "ready"):
            status = get_codex_mcp_status(context.get("config"))
            return CommandResult(content=format_codex_availability(status))
        if first in ("models", "model"):
            return CommandResult(content=format_codex_models())
        return CommandResult(
            content=(
                "Codex 命令：\n"
                "• /codex status — 查看 Codex MCP 状态\n"
                "• /codex available — 快速判断 Codex 当前是否可用\n"
                "• /codex models — 查看 Codex 可选模型"
            )
        )