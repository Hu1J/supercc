"""斜杠指令帮助信息 — /help"""
from core.commands.base import CommandHandler, CommandResult


class HelpHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "help"

    @property
    def help(self) -> str:
        return "/help — 显示帮助信息"

    async def execute(self, args: str, context: dict) -> CommandResult:
        text = (
            "🐲 **SuperCC 命令**\n\n"
            "• /new — 新建会话\n"
            "• /status — 会话状态\n"
            "• /stop — 打断当前查询\n"
            "• /git — 显示 Git 状态\n"
            "• /model — 查看/切换模型\n"
            "• /codex — 查看或配置 Codex MCP\n"
            "• /verbose — 控制消息推送\n"
            "• /memory — 查看/管理记忆\n"
            "• /skill — 查看技能列表\n"
            "• /switch <路径> — 切换到另一个项目\n"
            "• /restart — 重启当前 SuperCC\n"
            "• /update — 检查并更新到最新版本\n"
            "• /help — 显示本帮助"
        )
        return CommandResult(content=text)
