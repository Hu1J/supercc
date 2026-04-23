"""模型配置 MCP 工具 — list_models, switch_model, add_model"""
from __future__ import annotations

from claude_agent_sdk import tool

from supercc.claude.model_config import (
    get_all_models,
    get_active_model,
    switch_model,
    add_model,
    delete_model,
    ModelEnv,
    get_current_claude_settings,
    is_configured,
)
from supercc.claude.model_providers import PROVIDERS


def _fmt_model(model_id: str, entry, is_active: bool) -> str:
    """格式化单个模型的显示"""
    active_mark = "✅ " if is_active else "   "
    env = entry.env
    token_display = f"***{env.ANTHROPIC_AUTH_TOKEN[-4:]:>4}" if env.ANTHROPIC_AUTH_TOKEN else "(未设置)"
    return "\n".join([
        f"{active_mark}**{entry.name}** (`{model_id}`)",
        f"    描述: {entry.description or '(无)'}",
        f"    模型: `{env.ANTHROPIC_MODEL}`",
        f"    端点: `{env.ANTHROPIC_BASE_URL}`",
        f"    Token: ...{token_display}",
    ])


# ── tools ──────────────────────────────────────────────────────────────────────

@tool(
    "ListModels",
    "列出所有已配置的模型，包括名称、描述、当前使用的模型会用 ✅ 标记。",
    {},
)
async def list_models(args: dict) -> dict:
    """列出所有模型"""
    if not is_configured():
        current_settings = get_current_claude_settings()
        env_cfg = current_settings.get("env", {})

        if env_cfg.get("ANTHROPIC_AUTH_TOKEN"):
            guide = "\n\n".join([
                "📋 **检测到您已配置过 Claude Code**",
                "",
                "您的现有配置：",
                f"- 模型: `{env_cfg.get('ANTHROPIC_MODEL', '未设置')}`",
                f"- 端点: `{env_cfg.get('ANTHROPIC_BASE_URL', '未设置')}`",
                "",
                "💡 **建议**: 使用 `/model add` 将现有配置导入为第一个模型。",
                "",
                "用法: `/model add <name>|<description>|<token>|<base_url>|<model>`",
                "",
                "示例: `/model add 导入配置|从Claude Code导入|"
                + f"{env_cfg.get('ANTHROPIC_AUTH_TOKEN', '')}|"
                + f"{env_cfg.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')}|"
                + f"{env_cfg.get('ANTHROPIC_MODEL', 'claude-opus-4-5')}`",
            ])
        else:
            guide = "\n\n".join([
                "📋 **首次使用模型配置**",
                "",
                "您还没有配置任何模型。请使用 `/model add` 添加第一个模型。",
                "",
                "用法: `/model add <name>|<description>|<token>|<base_url>|<model>`",
                "",
                "示例:",
                "- Anthropic: `/model add anthropic|Anthropic API|sk-ant-xxx|https://api.anthropic.com|claude-opus-4-5`",
                "- OpenRouter: `/model add openrouter|OpenRouter|sk-or-xxx|https://openrouter.ai/api/v1|anthropic/claude-3.5-sonnet`",
            ])

        return {"content": [{"type": "text", "text": guide}]}

    models = get_all_models()
    active_id = None
    for mid, mentry in models.items():
        if mentry is get_active_model():
            active_id = mid
            break

    lines = ["🤖 **已配置的模型**\n"]
    for model_id, entry in models.items():
        lines.append(_fmt_model(model_id, entry, is_active=(model_id == active_id)))
        lines.append("")

    lines.append(f"\n当前激活: `{(active_id or '未知')}`")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "ListProviders",
    "列出所有预置模型供应商（Anthropic、OpenAI、DeepSeek、OpenRouter 等）及其可用模型。用户只需提供 API Key 和选择模型即可快速添加。",
    {},
)
async def list_providers(args: dict) -> dict:
    """列出所有预置供应商"""
    lines = ["🌐 **支持的模型供应商**\n"]
    for p in PROVIDERS.values():
        auth_display = {"bearer": "Bearer Token", "api_key": "API Key", "azure": "Azure AD"}.get(p.auth_type, p.auth_type)
        lines.append(f"**`{p.id}`** — {p.name}")
        lines.append(f"  {p.description}")
        lines.append(f"  端点: `{p.base_url or '(用户填入)'}`")
        lines.append(f"  认证: {auth_display}")
        lines.append(f"  模型:")
        for m in p.models[:8]:  # 最多显示8个
            lines.append(f"    `/{m}`")
        if len(p.models) > 8:
            lines.append(f"    ... 等 {len(p.models)} 个模型")
        lines.append("")

    lines.append("---")
    lines.append("**快速添加供应商模型：**")
    lines.append("`/model add --provider <provider_id>`")
    lines.append("然后提供 API Key 和选择模型即可。")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "SwitchModel",
    "切换当前使用的模型。切换后 Claude Code 将使用新模型的 API 配置。",
    {"model_id": str},
)
async def switch_model_tool(args: dict) -> dict:
    """切换模型"""
    model_id = args.get("model_id", "").strip()
    if not model_id:
        return {"content": [{"type": "text", "text": "model_id 是必填的"}], "is_error": True}

    models = get_all_models()
    if model_id not in models:
        available = ", ".join(f"`{mid}`" for mid in models.keys())
        return {
            "content": [{
                "type": "text",
                "text": f"未找到模型 ID: `{model_id}`\n可用模型: {available}"
            }],
            "is_error": True
        }

    ok = switch_model(model_id)
    if not ok:
        return {"content": [{"type": "text", "text": f"切换模型 `{model_id}` 失败"}], "is_error": True}

    entry = models[model_id]
    return {
        "content": [{
            "type": "text",
            "text": f"✅ 已切换到 **{entry.name}**\n\n"
                    f"模型: `{entry.env.ANTHROPIC_MODEL}`\n"
                    f"端点: `{entry.env.ANTHROPIC_BASE_URL}`\n\n"
                    f"注意: Claude Code 需要重启才能生效，使用 `/restart` 命令重启。"
        }]
    }


@tool(
    "AddModel",
    "添加新的模型配置。添加后需要使用 SwitchModel 切换到新模型。",
    {
        "model_id": str,
        "name": str,
        "description": str,
        "auth_token": str,
        "base_url": str,
        "model": str,
        "provider": str,
    },
)
async def add_model_tool(args: dict) -> dict:
    """添加新模型（支持 --provider 快捷方式）"""
    from supercc.claude.model_providers import get_provider, PROVIDERS

    model_id = args.get("model_id", "").strip()
    name = args.get("name", "").strip()
    description = args.get("description", "").strip()
    auth_token = args.get("auth_token", "").strip()
    base_url = args.get("base_url", "").strip()
    model = args.get("model", "").strip()
    provider_id = args.get("provider", "").strip()

    # --provider 快捷方式：自动填充 base_url
    if provider_id:
        provider = get_provider(provider_id)
        if not provider:
            available = ", ".join(PROVIDERS.keys())
            return {
                "content": [{"type": "text", "text": f"未知供应商 `{provider_id}`\n可用供应商: {available}"}],
                "is_error": True
            }
        if not base_url:
            base_url = provider.base_url
        if not name:
            name = provider.name

    # 验证必填字段
    missing = []
    if not model_id:
        missing.append("model_id")
    if not auth_token:
        missing.append("auth_token")
    if not base_url:
        missing.append("base_url（可通过 --provider 自动填入）")
    if not model:
        missing.append("model")

    if missing:
        # 提供供应商模型列表作为提示
        hint = ""
        if provider_id:
            p = get_provider(provider_id)
            if p:
                models_str = "\n".join(f"`{m}`" for m in p.models)
                hint = f"\n\n{p.name} 可用模型：\n{models_str}"
        return {
            "content": [{"type": "text", "text": f"缺少必填字段: {', '.join(missing)}{hint}"}],
            "is_error": True
        }

    env = ModelEnv(
        ANTHROPIC_AUTH_TOKEN=auth_token,
        ANTHROPIC_BASE_URL=base_url,
        ANTHROPIC_MODEL=model,
    )

    ok = add_model(model_id, name, description, env)
    if not ok:
        return {"content": [{"type": "text", "text": f"模型 ID `{model_id}` 已存在，请使用其他 ID"}], "is_error": True}

    return {
        "content": [{
            "type": "text",
            "text": f"✅ 模型 **{name}** (`{model_id}`) 已添加\n\n"
                    f"供应商: `{provider_id or 'custom'}`\n"
                    f"模型: `{model}`\n"
                    f"端点: `{base_url}`\n\n"
                    f"使用 `/model switch {model_id}` 切换到新模型。"
        }]
    }


@tool(
    "DeleteModel",
    "删除一个模型配置。无法删除当前激活的模型。",
    {"model_id": str},
)
async def delete_model_tool(args: dict) -> dict:
    """删除模型"""
    model_id = args.get("model_id", "").strip()
    if not model_id:
        return {"content": [{"type": "text", "text": "model_id 是必填的"}], "is_error": True}

    ok = delete_model(model_id)
    if not ok:
        return {
            "content": [{"type": "text", "text": f"删除模型 `{model_id}` 失败。可能原因：模型不存在或为当前激活模型。"}],
            "is_error": True
        }

    return {
        "content": [{
            "type": "text",
            "text": f"✅ 模型 `{model_id}` 已删除。"
        }]
    }
