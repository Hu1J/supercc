"""技能列表 — /skill"""
from pathlib import Path
from supercc.core.commands.base import CommandHandler, CommandResult


class SkillHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "skill"

    @property
    def help(self) -> str:
        return "/skill — 查看技能列表"

    async def execute(self, args: str, context: dict) -> CommandResult:
        import re

        parts = args.strip().split(maxsplit=1)
        scope_all = len(parts) > 1 and parts[1].strip().lower() == "all"

        if scope_all:
            skills_dir = Path.home() / ".claude" / "skills"
            title = "全局 Skills"
        else:
            project_path = ""
            if context.get("session_key"):
                project_path = context["session_key"].project_path or ""
            skills_dir = Path(project_path) / ".supercc" / "skills"
            title = f"项目 Skills（{Path(project_path).name}）"

        if not skills_dir.exists():
            return CommandResult(content=f"📭 暂无 {'全局' if scope_all else '项目'} Skills\n目录不存在：{skills_dir}")

        skill_entries = []
        for item in skills_dir.iterdir():
            if not item.is_dir() or not (item / "SKILL.md").exists():
                continue
            name = item.name
            description = ""
            try:
                content = (item / "SKILL.md").read_text(encoding="utf-8")
                m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                if m:
                    for line in m.group(1).splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass
            skill_entries.append((item.name, name, description))

        if not skill_entries:
            return CommandResult(content=f"📭 暂无 {'全局' if scope_all else '项目'} Skills")

        lines = [
            f"## 🛠 {title}（共 {len(skill_entries)} 个）\n",
            "| 目录名 | Skill 名称 | 描述 |",
            "|--------|-----------|------|",
        ]
        for dirname, name, description in skill_entries:
            desc_short = description[:40] + "…" if len(description) > 40 else description
            lines.append(f"| `{dirname}` | {name} | {desc_short} |")

        return CommandResult(content="\n".join(lines))