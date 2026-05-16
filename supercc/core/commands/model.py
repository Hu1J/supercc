"""模型管理 — /model"""
from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.protocol import SessionKey


class ModelHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "model"

    @property
    def help(self) -> str:
        return "/model — 查看/切换模型"

    def _mask_key(self, key: str) -> str:
        if not key or len(key) <= 8:
            return "****"
        return key[:6] + "***" + key[-4:]

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc.core.models.model_config import get_model_env, get_all_providers, set_project_model, _load_json
        from supercc.core.models.model_providers import PROVIDERS

        session_key: SessionKey = context.get("session_key")
        project_path = session_key.project_path if session_key else ""
        parts = args.strip().split(maxsplit=2)
        action = parts[0].lower() if parts else ""

        # /model switch <provider> <model>
        if action == "switch" and len(parts) >= 3:
            target_pid = parts[1]
            target_model = parts[2]
            ok, err = set_project_model(project_path, target_pid, target_model)
            if not ok:
                return CommandResult(content=f"❌ 切换失败：{err}")
            return CommandResult(content=f"✅ 已切换为 `{target_pid}`（模型：`{target_model}`）")

        # Default: show model table
        env = get_model_env()
        current_mid = env.ANTHROPIC_MODEL
        current_pid = env.provider_id
        providers_cfg = get_all_providers()

        configured = []
        unconfigured = []
        for pid, provider in PROVIDERS.items():
            if pid == "custom":
                continue
            pcfg = providers_cfg.get(pid)
            api_key = pcfg.api_key if pcfg else ""
            if api_key:
                configured.append((pid, provider.id, api_key, current_mid or "—", provider.models, pid == current_pid))
            else:
                unconfigured.append((pid, provider.id, "", "—", provider.models, False))

        # Custom providers
        raw = _load_json()
        providers_raw = raw.get("providers", {})
        custom_pids = set(providers_raw.keys()) - set(PROVIDERS.keys())
        for pid in sorted(custom_pids):
            pdata = providers_raw.get(pid, {})
            api_key = pdata.get("api_key", "")
            models = pdata.get("models", [])
            is_active = pid == current_pid
            if api_key:
                configured.append((pid, pid, api_key, current_mid or "—", models, is_active))
            else:
                unconfigured.append((pid, pid, "", "—", models, False))

        configured.sort(key=lambda x: 0 if x[5] else 1)

        def fmt_models(models, current):
            parts = []
            for m in models:
                if m == current:
                    parts.append(f"**`{m}`**")
                else:
                    parts.append(f"`{m}`")
            return " / ".join(parts)

        table_lines = [
            "| 状态 | Provider | API Key | 模型 |",
            "|------|----------|---------|------|",
        ]
        for pid, pname, api_key, current_model, all_models, is_active in configured:
            mark = "✅" if is_active else "✴️"
            table_lines.append(f"| {mark} | `{pid}` | `{self._mask_key(api_key)}` | {fmt_models(all_models, current_model)} |")
        for pid, pname, api_key, current_model, all_models, _is_active in unconfigured:
            avail = " / ".join(f"`{m}`" for m in all_models[:4])
            table_lines.append(f"| 📛 | `{pid}` | — | {avail} |")

        active_name = current_pid or "未设置"
        table_content = "\n".join(table_lines)

        content = (
            f"## 🔀 模型配置\n"
            f"当前使用：**{active_name}**（`{current_mid or '未设置'}`）\n\n"
            f"{table_content}\n\n"
            f"💡 切换模型：`/model switch <供应商> <模型ID>` 或者对我说：帮我切换到<供应商>的<模型ID>"
        )
        return CommandResult(content=content)