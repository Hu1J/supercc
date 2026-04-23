"""模型配置 MCP 工具 — ListModels, SetModel"""
from __future__ import annotations

from claude_agent_sdk import tool

from supercc.claude.model_config import (
    get_all_models,
    ModelEntry,
    ModelEnv,
)
from supercc.claude.model_providers import PROVIDERS


def _get_user_open_id() -> str | None:
    """从当前活跃会话获取 user_open_id。"""
    from supercc.claude.session_manager import SessionManager
    from supercc.config import resolve_config_path, SESSIONS_DB_PATH

    _, _ = resolve_config_path()
    db_path = SESSIONS_DB_PATH
    sm = SessionManager(db_path=db_path)
    session = sm.get_active_session_by_chat_id()
    return session.user_id if session else None


def _is_owner() -> bool:
    """检查当前用户是否为机器人所有者。"""
    from supercc.config import get_config

    user_id = _get_user_open_id()
    if not user_id:
        return False
    cfg = get_config()
    return user_id in cfg.auth.allowed_users


def _mask_api_key(key: str) -> str:
    """掩码展示 API Key，只露头尾。"""
    if not key:
        return "—"
    if len(key) <= 10:
        return "****"
    return key[:6] + "***" + key[-4:]


# ── tools ──────────────────────────────────────────────────────────────────────

@tool(
    "ListModels",
    "列出所有预置供应商及其配置状态，包括供应商名称、当前模型、API Key、所有可用模型。",
    {},
)
async def list_models(args: dict) -> dict:
    """列出所有供应商的模型配置"""
    models = get_all_models()

    # 建立 base_url -> (model_id, ModelEntry) 反查表
    url_to_model: dict[str, tuple[str, ModelEntry]] = {}
    for mid, mentry in models.items():
        if mentry.env.ANTHROPIC_AUTH_TOKEN and mentry.env.ANTHROPIC_BASE_URL:
            url_to_model[mentry.env.ANTHROPIC_BASE_URL] = (mid, mentry)

    configured = []  # (provider_id, provider_name, current_model, masked_api_key, all_models, is_active)
    unconfigured = []  # (provider_id, provider_name, all_models)

    active_id = None
    for mid, mentry in models.items():
        if mentry.env.ANTHROPIC_AUTH_TOKEN:
            if active_id is None:
                active_id = mid

    for p in PROVIDERS.values():
        matched = None
        if p.base_url and p.base_url in url_to_model:
            matched = url_to_model[p.base_url]
        if not matched:
            for url, (mid, mentry) in url_to_model.items():
                if p.base_url and url.startswith(p.base_url.rstrip("/") + "/"):
                    matched = (mid, mentry)
                    break

        if matched:
            mid, mentry = matched
            configured.append((
                p.id,
                p.name,
                mentry.env.ANTHROPIC_MODEL or "—",
                _mask_api_key(mentry.env.ANTHROPIC_AUTH_TOKEN),
                p.models,
                mid == active_id,
            ))
        else:
            unconfigured.append((p.id, p.name, p.models))

    lines = ["## 🤖 模型配置\n"]
    lines.append("| 状态 | 供应商 | 当前模型 | API Key | 所有可用模型 |")
    lines.append("|------|--------|---------|---------|------------|")
    for pid, pname, model, masked_key, all_models, is_active in configured:
        mark = "✅" if is_active else "✴️"
        avail = " / ".join(f"`{m}`" for m in all_models)
        lines.append(f"| {mark} | **{pname}** | `{model}` | `{masked_key}` | {avail} |")
    for pid, pname, all_models in unconfigured:
        avail = " / ".join(f"`{m}`" for m in all_models)
        lines.append(f"| 📛 | {pname} | — | — | {avail} |")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "SetModel",
    """设置/切换模型配置（仅限机器人所有者操作）。
入参为 JSON 格式：
{
  "provider": "volcano",      // 必填，供应商 ID
  "model": "kimi-k2.6",     // 选填，模型 ID（必须在该 provider 的可用模型列表中）
  "api_key": "sk-xxx"        // 选填，如需更新 API Key 则传入
}
说明：provider 必填；model 和 api_key 二选一（至少传一个）。
支持三种场景：
1. 切换供应商：provider + model + api_key（完整切换）
2. 切换模型（同供应商）：provider + model（api_key 不变）
3. 仅更新 API Key：provider（原 model 不变）+ api_key
""",
    {"config": str},
)
async def set_model_tool(args: dict) -> dict:
    """设置/切换模型（权限 + 智能路由）"""
    if not _is_owner():
        return {
            "content": [{
                "type": "text",
                "text": "⚠️ 无权操作：切换模型仅限机器人所有者。如需变更，请联系管理员。"
            }],
            "is_error": True,
        }

    import json
    config_str = args.get("config", "").strip()
    if not config_str:
        return {"content": [{"type": "text", "text": "config 是必填的（JSON 格式）"}], "is_error": True}

    try:
        cfg = json.loads(config_str)
    except json.JSONDecodeError:
        return {"content": [{"type": "text", "text": "config 必须是合法 JSON"}], "is_error": True}

    provider_id = cfg.get("provider", "").strip()
    model = cfg.get("model", "").strip()
    api_key = cfg.get("api_key")
    if api_key is not None:
        api_key = api_key.strip()

    if not provider_id:
        return {"content": [{"type": "text", "text": "provider 是必填的"}], "is_error": True}
    if not model and not api_key:
        return {"content": [{"type": "text", "text": "model 和 api_key 至少要传一个"}], "is_error": True}

    # 查找 provider
    provider = PROVIDERS.get(provider_id)
    if not provider:
        available = ", ".join(f"`{p.id}`" for p in PROVIDERS.values())
        return {"content": [{"type": "text", "text": f"未知供应商 `{provider_id}`\n可用: {available}"}], "is_error": True}

    # 验证 model 在 provider.models 中
    if model and model not in provider.models:
        models_str = ", ".join(f"`{m}`" for m in provider.models)
        return {
            "content": [{"type": "text", "text": f"模型 `{model}` 不在供应商 `{provider.name}` 的可用模型中。\n可用: {models_str}"}],
            "is_error": True,
        }

    models = get_all_models()

    # 查找该 provider 是否已有配置（通过 base_url 匹配）
    matched_mid = None
    matched_entry = None
    for mid, mentry in models.items():
        if mentry.env.ANTHROPIC_BASE_URL == provider.base_url:
            matched_mid = mid
            matched_entry = mentry
            break

    changed = []
    new_model_id = matched_mid or provider_id

    if matched_entry:
        # 已有配置，更新
        if api_key:
            matched_entry.env.ANTHROPIC_AUTH_TOKEN = api_key
            changed.append("API Key")
        if model:
            matched_entry.env.ANTHROPIC_MODEL = model
            changed.append(f"模型 → `{model}`")
        final_env = matched_entry.env
        mc._active_model_id = matched_mid
    else:
        # 新增配置
        new_entry = ModelEntry(
            name=provider.name,
            description=provider.description,
            env=ModelEnv(
                ANTHROPIC_AUTH_TOKEN=api_key or "",
                ANTHROPIC_BASE_URL=provider.base_url,
                ANTHROPIC_MODEL=model,
            ),
        )
        models[new_model_id] = new_entry
        changed.append(f"新增供应商 `{provider.name}`")
        if model:
            changed.append(f"模型 → `{model}`")
        if api_key:
            changed.append("API Key")
        final_env = new_entry.env

    # 保存前先校验 API credentials 是否有效
    import supercc.claude.model_config as mc
    from supercc.claude.model_config import save_models_config, _active_model_id, validate_model_env
    valid, err_msg = validate_model_env(final_env)
    if not valid:
        return {
            "content": [{
                "type": "text",
                "text": f"❌ 配置无效，切换被拒绝。\n\n错误：{err_msg}\n\n请检查 API Key、模型 ID 是否正确，或联系管理员。"
            }],
            "is_error": True,
        }

    # 新增供应商时，将其设为激活模型
    if not matched_entry:
        import supercc.claude.model_config as mc
        mc._active_model_id = new_model_id

    save_models_config(mc._active_model_id, models)

    # 同步写入 Claude Code 内部配置文件
    import supercc.claude.model_config as mc
    active_env = models[mc._active_model_id].env
    mc._update_claude_settings(active_env)
    mc._ensure_claude_onboarding()

    changed_str = "、".join(changed)
    return {
        "content": [{
            "type": "text",
            "text": f"✅ 已完成：{changed_str}。\n\n供应商：`{provider.name}`\n模型：`{model or matched_entry.env.ANTHROPIC_MODEL if matched_entry else model}`\n下次对话起生效。"
        }]
    }
