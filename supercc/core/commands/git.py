"""Git 状态 — /git"""
import subprocess
from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.protocol import SessionKey


class GitHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "git"

    @property
    def help(self) -> str:
        return "/git — 显示 Git 状态"

    def _run_git(self, args: list[str], cwd: str) -> str:
        try:
            r = subprocess.run(
                ["git"] + args, capture_output=True, text=True, timeout=10, cwd=cwd
            )
            return r.stdout.strip()
        except Exception:
            return ""

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.banner import _check_git_available

        session_key: SessionKey = context.get("session_key")
        project_path = session_key.project_path if session_key else ""

        if not _check_git_available():
            return CommandResult(
                content=(
                    "⚠️ 检测到系统中尚未安装 git，无法使用 /git 命令。\n\n"
                    "💡 跟我说: \"安装 git\", 即可启用 Git 相关功能。"
                )
            )

        branch = self._run_git(["branch", "--show-current"], project_path) or "(无分支)"
        status_output = self._run_git(["status", "--porcelain"], project_path)
        log_lines = self._run_git(
            ["log", "--format=%cI %h %s", "-5"], project_path
        ).splitlines()

        lines = [f"🌟 **Git Status - {branch}**", "", "📝 **变更文件**", ""]

        if status_output:
            for line in status_output.splitlines():
                char = line[0] if line[0] != " " else (line[1] if line[1] != " " else "?")
                filename = line[3:]
                emoji = {"A": "✨", "M": "📄", "D": "🗑", "R": "🔄", "?": "❓"}.get(char, "•")
                lines.append(f"{emoji} `{filename}`")
        else:
            lines.append("✅ 工作区干净，无待提交变更")

        lines.extend(["", "📋 **最近提交**", "", "| 时间 | Hash | 描述 |", "|------|------|------|"])
        for log_line in log_lines:
            parts = log_line.split(" ", 2)
            if len(parts) >= 3:
                dt = parts[0].replace("T", " ")[:16]
                h = parts[1]
                msg = parts[2]
                lines.append(f"| {dt} | `{h}` | {msg} |")

        return CommandResult(content="\n".join(lines))