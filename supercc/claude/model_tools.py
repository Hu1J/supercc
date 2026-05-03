"""模型配置 MCP 工具 — ListModels, SetModel"""
from __future__ import annotations

from claude_agent_sdk import tool

from supercc.claude.model_config import (
    get_all_providers,
    get_active_model_for_project,
    update_provider_api_key,
    set_project_model,
    validate_model_env,
    get_model_env,
)
from supercc.claude.model_providers import PROVIDERS


def _get_user_open_id() -> str | None:
    """从当前消息上下文获取 user_open_id（通过 contextvar）。"""
    from supercc.claude.message_context import get_current_user_open_id
    return get_current_user_open_id()


def _is_owner() -> bool:
    """检查当前用户是否为机器人所有者。"""
    from supercc.config import get_config

    user_id = _get_user_open_id()
    if not user_id:
        return False
    cfg = get_config()
    return user_id in cfg.auth.allowed_users


def _get_project_path() -> str:
    """获取当前项目路径。"""
    from supercc.config import get_config
    return get_config().claude.approved_directory  # 项目根路径


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
    "列出所有预置供应商及其配置状态，包括供应商名称、API Key、所有可用模型。",
    {},
)
async def list_models(args: dict) -> dict:
    """列出所有供应商的模型配置"""
    project_path = _get_project_path()
    providers = get_all_providers()
    current_pid, current_mid = get_active_model_for_project(project_path)

    lines = ["## 🤖 模型配置\n"]
    lines.append("| 状态 | 供应商 | API Key | 可用模型 |")
    lines.append("|------|--------|---------|----------|")

    for pid, provider in PROVIDERS.items():
        if pid == "custom":
            continue
        pcfg = providers.get(pid)
        api_key = pcfg.api_key if pcfg else ""
        masked = _mask_api_key(api_key)
        is_active = pid == current_pid
        mark = "✅" if is_active else "✴️"
        avail = " / ".join(f"`{m}`" for m in provider.models[:5])
        if len(provider.models) > 5:
            avail += f" ... (+{len(provider.models) - 5})"
        lines.append(f"| {mark} | **{provider.id}** | `{masked}` | {avail} |")

    if current_pid:
        active_provider = PROVIDERS.get(current_pid)
        if active_provider:
            lines.append("")
            lines.append(f"**当前激活：** `{current_pid}` / `{current_mid}`")

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
说明：provider 必填；model 选填（填了则校验是否在可用列表中）；api_key 选填。
支持两种场景：
1. 完整切换：provider + model + api_key（新增供应商配置）
2. 切换模型：provider + model（只更新当前项目的激活模型）
3. 更新 API Key：provider + api_key（只更新供应商的 API Key）
```json
{"provider": "volcano", "model": "kimi-k2.6", "api_key": "sk-xxx"}
```
""",
    {"config": str},
)
async def set_model_tool(args: dict) -> dict:
    """设置/切换模型"""
    if not _is_owner():
        return {
            "content": [{
                "type": "text",
                "text": "⚠️ 无权操作：切换模型仅限机器人所有者。"
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
    model_id = cfg.get("model", "").strip()
    api_key = cfg.get("api_key", "").strip() if cfg.get("api_key") else None

    if not provider_id:
        return {"content": [{"type": "text", "text": "provider 是必填的"}], "is_error": True}

    # 校验 provider 存在
    provider = PROVIDERS.get(provider_id)
    if not provider:
        available = ", ".join(f"`{p.id}`" for p in PROVIDERS.values())
        return {"content": [{"type": "text", "text": f"未知供应商 `{provider_id}`\n可用: {available}"}], "is_error": True}

    project_path = _get_project_path()
    changed = []

    # 如果提供了 api_key，更新供应商配置（带验证）
    if api_key:
        from supercc.claude.model_config import update_provider_api_key
        ok, err = update_provider_api_key(provider_id, api_key)
        if not ok:
            return {
                "content": [{"type": "text", "text": f"❌ API Key 更新失败：{err}"}],
                "is_error": True,
            }
        changed.append("API Key")

    # 切换模型（带验证）
    if model_id:
        if provider.models and model_id not in provider.models:
            models_str = ", ".join(f"`{m}`" for m in provider.models)
            return {
                "content": [{"type": "text", "text": f"模型 `{model_id}` 不在供应商 `{provider.id}` 的可用模型中。\n可用: {models_str}"}],
                "is_error": True,
            }

        ok, err = set_project_model(project_path, provider_id, model_id)
        if not ok:
            return {
                "content": [{"type": "text", "text": f"❌ 切换失败：{err}"}],
                "is_error": True,
            }
        changed.append(f"模型 → `{model_id}`")
    elif api_key:
        # 只更新了 api_key，没切模型，检查当前项目是否已有激活映射
        current_pid, current_mid = get_active_model_for_project(project_path)
        if not current_pid:
            return {
                "content": [{"type": "text", "text": f"✅ API Key 已更新\n\n供应商：`{provider.id}`\n\n提示：尚未为当前项目设置激活模型，请同时提供 model 参数切换模型。"}],
                "is_error": False,
            }

    changed_str = "、".join(changed) if changed else "无变更"
    return {
        "content": [{
            "type": "text",
            "text": f"✅ 已完成：{changed_str}。\n\n供应商：`{provider.id}`\n模型：`{model_id or '未切换'}`\n已激活。"
        }]
    }
