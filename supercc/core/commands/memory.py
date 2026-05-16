"""记忆管理 — /memory"""
from supercc.core.commands.base import CommandHandler, CommandResult


HELP_TEXT = (
    "【记忆系统指令】\n"
    "/memory user list — 列出用户偏好\n"
    "/memory user add <title>|<content>|<keywords> — 新增用户偏好\n"
    "/memory user del <id> — 删除用户偏好\n"
    "/memory user update <id> <title>|<content>|<keywords> — 编辑用户偏好\n"
    "/memory user search <关键词> — 搜索用户偏好\n"
    "/memory proj list — 列出项目记忆\n"
    "/memory proj add <title>|<content>|<keywords> — 新增项目记忆\n"
    "/memory proj del <id> — 删除项目记忆\n"
    "/memory proj update <id> <title>|<content>|<keywords> — 编辑项目记忆\n"
    "/memory proj search <关键词> — 搜索项目记忆\n"
    "关键词用逗号分隔（若有多个）"
)


def _fmt_pref_table(prefs, total):
    header = f"👤 **用户偏好**（共 {total} 条）\n\n"
    header += "| # | 标题 | 内容摘要 | 关键词 |\n|---|------|----------|--------|\n"
    for i, p in enumerate(prefs, 1):
        title = p.title[:40]
        content = p.content[:50] + ("…" if len(p.content) > 50 else "")
        header += f"| {i} | {title} | {content} | {p.keywords} |\n"
    return header


def _fmt_proj_table(mems, total):
    header = f"📁 **项目记忆**（共 {total} 条）\n\n"
    header += "| # | 标题 | 内容摘要 | 关键词 |\n|---|------|----------|--------|\n"
    for i, m in enumerate(mems, 1):
        title = m.title[:40]
        content = m.content[:50] + ("…" if len(m.content) > 50 else "")
        header += f"| {i} | {title} | {content} | {m.keywords} |\n"
    return header


class MemoryHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "memory"

    @property
    def help(self) -> str:
        return "/memory — 查看/管理记忆"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.core.claude.memory_manager import get_memory_manager

        parts = args.strip().split(maxsplit=2)
        scope = parts[0].lower() if len(parts) > 0 else ""
        action = parts[1].lower() if len(parts) > 1 else ""
        raw_args = parts[2].strip() if len(parts) > 2 else ""

        user_open_id = context.get("user_open_id", "")
        platform = context.get("platform", "feishu")
        chat_id = context.get("chat_id", "")
        bot_id = context.get("bot_id", "")
        if not bot_id and context.get("session_key"):
            bot_id = context["session_key"].bot_id or ""
        project_path = ""
        if context.get("session_key"):
            project_path = context["session_key"].project_path or ""

        mm = get_memory_manager()

        if not scope:
            return CommandResult(content=HELP_TEXT)

        if scope == "user":
            if action == "list":
                prefs = mm.get_preferences_by_user(user_open_id, platform=platform, bot_id=bot_id)
                if not prefs:
                    return CommandResult(content="📭 暂无用户偏好记录")
                return CommandResult(content=_fmt_pref_table(prefs, len(prefs)))
            if action == "add":
                parts = raw_args.split("|")
                if len(parts) < 3:
                    return CommandResult(content="用法: /memory user add <title>|<content>|<keywords>")
                title, content, keywords = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if not title or not content or not keywords:
                    return CommandResult(content="title、content、keywords 三样必填")
                p = mm.add_preference(user_open_id, title, content, keywords, platform=platform, bot_id=bot_id)
                return CommandResult(content=f"✅ 用户偏好已保存（ID: {p.id}）")
            if action == "del":
                if not raw_args:
                    return CommandResult(content="用法: /memory user del <id>")
                ok = mm.delete_preference(raw_args, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
                return CommandResult(content=f"🗑️ 用户偏好 {raw_args} 已删除" if ok else f"未找到 id={raw_args}")
            if action == "update":
                parts = raw_args.split("|", 2)
                if len(parts) < 3:
                    return CommandResult(content="用法: /memory user update <id> <title>|<content>|<keywords>")
                pref_id, title, content = parts[0].strip(), parts[1].strip(), parts[2].strip()
                keywords = parts[3].strip() if len(parts) > 3 else ""
                ok = mm.update_preference(pref_id, title, content, keywords, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
                return CommandResult(content=f"✅ 用户偏好 {pref_id} 已更新" if ok else f"未找到 id={pref_id}")
            if action == "search":
                if not raw_args:
                    return CommandResult(content="用法: /memory user search <关键词>")
                results = mm.search_preferences(raw_args, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
                if not results:
                    return CommandResult(content=f"未找到与「{raw_args}」相关的用户偏好")
                return CommandResult(content=_fmt_pref_table(results, len(results)))
            return CommandResult(content=f"未知 user action: {action}")

        if scope == "proj":
            if action == "list":
                mems = mm.get_project_memories(project_path, platform=platform, chat_id=chat_id)
                if not mems:
                    return CommandResult(content="📭 暂无项目记忆记录")
                return CommandResult(content=_fmt_proj_table(mems, len(mems)))
            if action == "add":
                parts = raw_args.split("|")
                if len(parts) < 3:
                    return CommandResult(content="用法: /memory proj add <title>|<content>|<keywords>")
                title, content, keywords = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if not title or not content or not keywords:
                    return CommandResult(content="title、content、keywords 三样必填")
                m = mm.add_project_memory(project_path, title, content, keywords, platform=platform, chat_id=chat_id)
                return CommandResult(content=f"✅ 项目记忆已保存（ID: {m.id}）")
            if action == "del":
                if not raw_args:
                    return CommandResult(content="用法: /memory proj del <id>")
                ok = mm.delete_project_memory(raw_args, project_path, platform=platform, chat_id=chat_id)
                return CommandResult(content=f"🗑️ 项目记忆 {raw_args} 已删除" if ok else f"未找到 id={raw_args}")
            if action == "update":
                parts = raw_args.split("|", 2)
                if len(parts) < 3:
                    return CommandResult(content="用法: /memory proj update <id> <title>|<content>|<keywords>")
                mem_id, title, content = parts[0].strip(), parts[1].strip(), parts[2].strip()
                keywords = parts[3].strip() if len(parts) > 3 else ""
                ok = mm.update_project_memory(mem_id, title, content, keywords, project_path, platform=platform, chat_id=chat_id)
                return CommandResult(content=f"✅ 项目记忆 {mem_id} 已更新" if ok else f"未找到 id={mem_id}")
            if action == "search":
                if not raw_args:
                    return CommandResult(content="用法: /memory proj search <关键词>")
                results = mm.search_project_memories(raw_args, project_path, platform=platform, chat_id=chat_id)
                if not results:
                    return CommandResult(content=f"未找到与「{raw_args}」相关的项目记忆")
                mems = [r.memory for r in results]
                return CommandResult(content=_fmt_proj_table(mems, len(mems)))
            return CommandResult(content=f"未知 proj action: {action}")

        return CommandResult(content=f"未知 scope: {scope}\n{HELP_TEXT}")