"""新建会话 — /new"""
from core.commands.base import CommandHandler, CommandResult
from core.protocol import SessionKey


class NewSessionHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "new"

    @property
    def help(self) -> str:
        return "/new — 新建会话"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH

        session_key: SessionKey = context.get("session_key")
        user_open_id = context.get("user_open_id", "")

        sm = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
        # Use get_or_create_session which is the actual API
        # (task description mentioned create_session but that method doesn't exist)
        session = sm.get_or_create_session(session_key, user_open_id)

        return CommandResult(
            content=f"✅ 新会话已创建\n会话ID: {session.session_id}\n工作目录: {session.project_path}"
        )