"""模型配置 MCP 工具 — ListModels, SetModel"""
from __future__ import annotations

from claude_agent_sdk import tool

from supercc.claude.model_config import (
    get_all_models,
    get_active_model,
    ModelEntry,
    ModelEnv,
    switch_model,
    update_model_env,
    update_model_token,
    add_model,
    validate_model_env,
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
    custom_models = []  # (model_id, provider_name, model, masked_key, base_url, is_active)

    active_entry = get_active_model()
    active_base_url = active_entry.env.ANTHROPIC_BASE_URL if active_entry else ""

    for p in PROVIDERS.values():
        if p.id == "custom":
            continue  # custom 模型单独处理
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
                mentry.env.ANTHROPIC_BASE_URL == active_base_url,
            ))
        else:
            unconfigured.append((p.id, p.name, p.models))

    # 收集自定义模型（base_url 不匹配任何预置供应商）
    for mid, mentry in models.items():
        is_custom = (
            mentry.provider_name != "custom"
            and not any(
                p.base_url and mentry.env.ANTHROPIC_BASE_URL == p.base_url
                for p in PROVIDERS.values()
            )
        )
        if mentry.provider_name == "custom" or is_custom:
            custom_models.append((
                mid,
                mentry.provider_name,
                mentry.env.ANTHROPIC_MODEL or "—",
                _mask_api_key(mentry.env.ANTHROPIC_AUTH_TOKEN),
                mentry.env.ANTHROPIC_BASE_URL,
                mentry.env.ANTHROPIC_BASE_URL == active_base_url,
            ))

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

    if custom_models:
        lines.append("")
        lines.append("### 自定义模型\n")
        lines.append("| 状态 | 供应商 | 模型 | API Key | Base URL |")
        lines.append("|------|--------|------|---------|----------|")
        for mid, pname, model, masked_key, base_url, is_active in custom_models:
            mark = "✅" if is_active else "✴️"
            lines.append(f"| {mark} | {pname} | `{model}` | `{masked_key}` | `{base_url}` |")

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
说明：provider 和 model 必填；api_key 选填（如需更新 API Key 则传入）。
支持两种场景：
1. 完整切换：provider + model + api_key
2. 切换模型（同供应商）：provider + model（api_key 不变）
3. 自定义模型：provider="custom"，必须同时提供 base_url；provider_name 可选（默认 "custom"）
```json
{"provider": "custom", "model": "my-model", "base_url": "https://my.api.com/v1", "api_key": "sk-xxx", "provider_name": "My Provider"}
```
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
    if not model:
        return {"content": [{"type": "text", "text": "model 是必填的"}], "is_error": True}

    # ── custom 模式 ──────────────────────────────────────────────────────────
    if provider_id == "custom":
        base_url = cfg.get("base_url", "").strip()
        if not base_url:
            return {"content": [{"type": "text", "text": "custom 模式必须提供 base_url"}], "is_error": True}
        if not api_key:
            return {"content": [{"type": "text", "text": "custom 模式必须提供 api_key"}], "is_error": True}

        provider_name = cfg.get("provider_name", "custom").strip() or "custom"
        custom_model_id = f"custom-{model}"
        env = ModelEnv(
            ANTHROPIC_AUTH_TOKEN=api_key,
            ANTHROPIC_BASE_URL=base_url.rstrip("/"),
            ANTHROPIC_MODEL=model,
        )

        all_models = get_all_models()
        matched_mid = None
        for mid, mentry in all_models.items():
            if mentry.env.ANTHROPIC_BASE_URL == base_url.rstrip("/"):
                matched_mid = mid
                break

        if matched_mid:
            changed_str = f"更新自定义模型 `{model}`"
        else:
            added = add_model(
                custom_model_id,
                model,
                f"自定义供应商: {provider_name}",
                env,
                provider_name=provider_name,
            )
            if not added:
                return {"content": [{"type": "text", "text": f"自定义模型 `{model}` 添加失败"}], "is_error": True}
            changed_str = f"新增自定义模型 `{model}`"

        valid, err_msg = validate_model_env(env)
        if not valid:
            return {
                "content": [{"type": "text", "text": f"❌ 配置无效：{err_msg}"}],
                "is_error": True,
            }

        if matched_mid:
            update_model_env(matched_mid, env, provider_name=provider_name)
            switch_model(matched_mid)
        else:
            switch_model(custom_model_id)
        return {
            "content": [{
                "type": "text",
                "text": f"✅ {changed_str}\n供应商：{provider_name}\n模型：`{model}`\nBase URL：{base_url}\n已激活。",
            }]
        }

    # ── 预置供应商模式 ────────────────────────────────────────────────────────
    provider = PROVIDERS.get(provider_id)
    if not provider:
        available = ", ".join(f"`{p.id}`" for p in PROVIDERS.values())
        return {"content": [{"type": "text", "text": f"未知供应商 `{provider_id}`\n可用: {available}"}], "is_error": True}

    if model and model not in provider.models:
        models_str = ", ".join(f"`{m}`" for m in provider.models)
        return {
            "content": [{"type": "text", "text": f"模型 `{model}` 不在供应商 `{provider.name}` 的可用模型中。\n可用: {models_str}"}],
            "is_error": True,
        }

    models = get_all_models()

    # 通过 base_url 查找该 provider 是否已有配置
    matched_mid = None
    for mid, mentry in models.items():
        if mentry.env.ANTHROPIC_BASE_URL == provider.base_url:
            matched_mid = mid
            break

    changed = []
    env_to_validate: ModelEnv | None = None

    if matched_mid:
        entry = models[matched_mid]
        if api_key:
            entry.env.ANTHROPIC_AUTH_TOKEN = api_key
            changed.append("API Key")
        if model:
            entry.env.ANTHROPIC_MODEL = model
            changed.append(f"模型 → `{model}`")
        env_to_validate = entry.env
    else:
        new_entry = ModelEntry(
            name=provider.name,
            description=provider.description,
            env=ModelEnv(
                ANTHROPIC_AUTH_TOKEN=api_key or "",
                ANTHROPIC_BASE_URL=provider.base_url,
                ANTHROPIC_MODEL=model,
            ),
        )
        added = add_model(provider_id, provider.name, provider.description, new_entry.env, provider_name=provider.name)
        if not added:
            return {"content": [{"type": "text", "text": f"供应商 `{provider.name}` 添加失败（ID 可能已存在）"}], "is_error": True}
        changed.append(f"新增供应商 `{provider.name}`")
        if model:
            changed.append(f"模型 → `{model}`")
        if api_key:
            changed.append("API Key")
        env_to_validate = new_entry.env

    # 先校验，失败则不切换
    valid, err_msg = validate_model_env(env_to_validate)
    if not valid:
        return {
            "content": [{
                "type": "text",
                "text": f"❌ 配置无效，切换被拒绝。\n\n错误：{err_msg}\n\n请检查 API Key、模型 ID 是否正确，或联系管理员。"
            }],
            "is_error": True,
        }

    # 校验通过后再写入文件并切换
    if matched_mid:
        update_model_env(matched_mid, env_to_validate)
        switch_model(matched_mid)
    else:
        switch_model(provider_id)

    changed_str = "、".join(changed)
    return {
        "content": [{
            "type": "text",
            "text": f"✅ 已完成：{changed_str}。\n\n供应商：`{provider.name}`\n模型：`{model or (env_to_validate.ANTHROPIC_MODEL if matched_mid else model)}`\n已激活。"
        }]
    }
