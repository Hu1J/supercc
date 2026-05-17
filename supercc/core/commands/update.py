"""更新 — /update"""
import packaging.version

from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.commands.restart_impl import _pip_install


class UpdateHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "update"

    @property
    def help(self) -> str:
        return "/update — 检查并更新到最新版本"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.core.commands.restart_impl import check_version

        current_ver, latest_ver = check_version()
        if packaging.version.parse(latest_ver) <= packaging.version.parse(current_ver):
            return CommandResult(content=f"SuperCC当前版本{current_ver}已是最新版")

        content = f"SuperCC可更新！当前版本{current_ver} -> 新版本{latest_ver}"

        # 先返回消息，再在后台线程做 pip install + restart
        async def _background_update():
            import threading
            from supercc.core.commands.restart_impl import _do_update
            # 在独立线程中执行（不阻塞 event loop）
            t = threading.Thread(target=lambda: list(_do_update()), daemon=True)
            t.start()
            t.join()  # 等待线程完成（线程内会 execvp 替换进程）

        import asyncio
        asyncio.ensure_future(_background_update())

        return CommandResult(content=content, event="update")
