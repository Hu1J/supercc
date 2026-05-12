"""新建会话 — /new"""
from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.protocol import SessionKey


class NewSessionHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "new"

    @property
    def help(self) -> str:
        return "/new — 新建会话"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH

        session_key: SessionKey = context.get("session_key")
        user_open_id = context.get("user_open_id", "")
        worker_pool = context.get("worker_pool")

        sm = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
        session = sm.get_or_create_session(session_key, user_open_id)

        # 重置 Worker 的会话状态，下次 query 强制新建 Claude SDK session
        if worker_pool and session_key:
            await worker_pool.reset_session(session_key)

        return CommandResult(
            content=f"✅ 新会话已创建\n会话ID: {session.session_id}\n工作目录: {session.project_path}"
        )
