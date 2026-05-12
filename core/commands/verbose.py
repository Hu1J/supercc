"""Verbose 配置 — /verbose"""
from core.commands.base import CommandHandler, CommandResult


class VerboseHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "verbose"

    @property
    def help(self) -> str:
        return "/verbose — 控制消息推送"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.config import VerboseChannelEntry

        platform = context.get("platform", "feishu")
        chat_id = context.get("chat_id", "")
        config = context.get("config")

        if not config:
            return CommandResult(content="⚠️ Config 不可用")

        if platform not in config.verbose:
            config.verbose[platform] = {}
        if chat_id not in config.verbose[platform]:
            config.verbose[platform][chat_id] = VerboseChannelEntry()
        entry = config.verbose[platform][chat_id]

        parts = args.strip().lower().split()
        if not parts:
            status = [
                f"**消息推送配置**（chat: `{chat_id}`）",
                f"- 🧰 Skill 自进化：`{'开' if entry.skill else '关'}`",
                f"- 🧠 记忆自优化：`{'开' if entry.mem else '关'}`",
                f"- ⚙️ 过程消息：`{'开' if entry.step else '关'}`",
                "",
                "调整：/verbose on|off|skill on|off|mem on|off|step on|off",
            ]
            return CommandResult(content="\n".join(status))

        cmd = parts[0]
        sub = parts[1] if len(parts) > 1 else ""

        if cmd == "on":
            entry.skill = entry.mem = entry.step = True
            msg = "✅ 已开启所有消息推送"
        elif cmd == "off":
            entry.skill = entry.mem = entry.step = False
            msg = "🔇 已关闭所有消息推送"
        elif cmd == "skill" and sub in ("on", "off"):
            entry.skill = sub == "on"
            msg = f"🧰 Skill 自进化已{'开启' if entry.skill else '关闭'}"
        elif cmd == "mem" and sub in ("on", "off"):
            entry.mem = sub == "on"
            msg = f"🧠 记忆自优化已{'开启' if entry.mem else '关闭'}"
        elif cmd == "step" and sub in ("on", "off"):
            entry.step = sub == "on"
            msg = f"⚙️ 过程消息已{'开启' if entry.step else '关闭'}"
        else:
            msg = "❓ 用法：/verbose [on|off|skill on|off|mem on|off|step on|off]"

        # 持久化到磁盘
        config_path = context.get("config_path", "")
        if config_path:
            try:
                from supercc.config import _write_config_to_path
                _write_config_to_path(config_path, config)
            except Exception as e:
                import logging
                logging.warning(f"[/verbose] failed to write config: {e}")

        return CommandResult(content=msg)