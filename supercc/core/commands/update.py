"""更新 — /update"""
import packaging.version

from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.commands.restart_impl import check_version, do_update


class UpdateHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "update"

    @property
    def help(self) -> str:
        return "/update — 检查并更新到最新版本"

    async def execute(self, args: str, context: dict) -> CommandResult:
        import asyncio

        current_ver, latest_ver = check_version()
        if packaging.version.parse(latest_ver) <= packaging.version.parse(current_ver):
            return CommandResult(content=f"✅ 当前版本 {current_ver} 已是最新")

        # 先执行安装，再返回结果
        try:
            await asyncio.to_thread(do_update)
        except Exception as e:
            return CommandResult(content=f"❌ 更新失败: {e}")

        return CommandResult(
            content=f"✅ 已更新 {current_ver} → {latest_ver}，请发 /restart 指令重启生效",
            event="update",
        )
