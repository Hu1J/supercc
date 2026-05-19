"""更新 — /update"""
import packaging.version

from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.commands.restart_impl import check_version


class UpdateHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "update"

    @property
    def help(self) -> str:
        return "/update — 检查并更新到最新版本"

    async def execute(self, args: str, context: dict) -> CommandResult:
        current_ver, latest_ver = check_version()
        if packaging.version.parse(latest_ver) <= packaging.version.parse(current_ver):
            return CommandResult(content=f"✅ 当前版本 {current_ver} 已是最新")

        content = f"✅ 已更新 {current_ver} → {latest_ver}，请发 /restart 指令重启生效"

        # 先返回消息，再在后台 pip install
        async def _background_update():
            import threading
            from supercc.core.commands.restart_impl import do_update
            t = threading.Thread(target=do_update, daemon=True)
            t.start()
            t.join()

        import asyncio
        asyncio.ensure_future(_background_update())

        return CommandResult(content=content, event="update")
