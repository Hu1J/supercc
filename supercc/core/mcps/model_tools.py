"""模型配置 MCP 工具 — ListModels, SetModel, AddCustomProvider"""
from __future__ import annotations

from claude_agent_sdk import tool

from supercc.core.models.model_config import (
    add_custom_provider,
    get_all_providers,
    get_active_model_for_project,
    update_provider_api_key,
    set_project_model,
    validate_model_env,
    get_model_env,
)
from supercc.core.models.model_providers import PROVIDERS


def _get_user_open_id() -> str | None:
    """从当前消息上下文获取 user_open_id（通过 contextvar）。"""
    from supercc.core.claude.message_context import get_current_user_open_id
    return get_current_user_open_id()


def _is_owner() -> bool:
    """检查当前用户是否为机器人所有者。"""
    from supercc.config import get_config

    user_id = _get_user_open_id()
    if not user_id:
        return False
    cfg = get_config()
    return user_id in cfg.channels.feishu.allowed_users


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
    "列出所有供应商及其配置状态，包括内置供应商和自定义供应商。",
    {},
)
async def list_models(args: dict) -> dict:
    """列出所有供应商的模型配置"""
    project_path = _get_project_path()
    providers = get_all_providers()
    current_pid, current_mid = get_active_model_for_project(project_path)

    # 获取 model.json 原始数据（含自定义供应商）
    from supercc.core.models.model_config import _load_json
    raw = _load_json()
    providers_raw = raw.get("providers", {})

    lines = ["## 🤖 模型配置\n"]
    lines.append("| 状态 | 供应商 | API Key | 可用模型 |")
    lines.append("|------|--------|---------|----------|")

    # 内置供应商
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

    # 自定义供应商（不在 PROVIDERS 中的）
    custom_pids = set(providers_raw.keys()) - set(PROVIDERS.keys())
    if custom_pids:
        lines.append("")
        lines.append("**自定义供应商：**")
        for pid in sorted(custom_pids):
            pdata = providers_raw.get(pid, {})
            api_key = pdata.get("api_key", "")
            masked = _mask_api_key(api_key)
            models = pdata.get("models", [])
            base_url = pdata.get("base_url", "")
            is_active = pid == current_pid
            mark = "✅" if is_active else "✴️"
            if models:
                avail = " / ".join(f"`{m}`" for m in models[:3])
                if len(models) > 3:
                    avail += f" ... (+{len(models) - 3})"
            else:
                avail = "—"
            url_note = f" ({base_url})" if base_url else ""
            lines.append(f"| {mark} | **{pid}**{url_note} | `{masked}` | {avail} |")

    if current_pid and current_pid not in PROVIDERS:
        lines.append("")
        lines.append(f"**当前激活：** `{current_pid}` / `{current_mid}`")
    elif current_pid:
        lines.append("")
        lines.append(f"**当前激活：** `{current_pid}` / `{current_mid}`")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "SetModel",
    """设置/切换模型配置（仅限机器人所有者操作）。
入参为 JSON 格式：
{
  "provider": "volcano",      // 必填，供应商 ID（内置或自定义均可）
  "model": "kimi-k2.6",       // 选填，模型 ID（内置供应商会校验，自定义供应商跳过校验）
  "api_key": "sk-xxx"         // 选填，如需更新 API Key 则传入
}
说明：新增自定义供应商请使用 AddCustomProvider 工具。
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

    # 检查是否为内置供应商
    provider = PROVIDERS.get(provider_id)
    is_builtin = provider is not None

    # 自定义供应商名称验证：只允许大小写英文+数字
    if not is_builtin:
        import re
        if not re.fullmatch(r'[a-zA-Z0-9]+', provider_id):
            return {"content": [{"type": "text", "text": "供应商名称只支持大小写英文字母和数字，不能包含特殊字符"}], "is_error": True}

    project_path = _get_project_path()
    changed = []

    # 如果提供了 api_key，更新供应商配置（供应商必须已存在）
    if api_key:
        from supercc.core.models.model_config import update_provider_api_key
        ok, err = update_provider_api_key(provider_id, api_key)
        if not ok:
            return {
                "content": [{"type": "text", "text": f"❌ API Key 更新失败：{err}"}],
                "is_error": True,
            }
        changed.append("API Key")

    # 切换模型（内置供应商带验证，自定义供应商跳过验证）
    if model_id:
        if is_builtin and provider.models and model_id not in provider.models:
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
            provider_name = provider.id if provider else provider_id
            return {
                "content": [{"type": "text", "text": f"✅ API Key 已更新\n\n供应商：`{provider_name}`\n\n提示：尚未为当前项目设置激活模型，请同时提供 model 参数切换模型。"}],
                "is_error": False,
            }

    changed_str = "、".join(changed) if changed else "无变更"
    provider_name = provider.id if provider else provider_id
    return {
        "content": [{
            "type": "text",
            "text": f"✅ 已完成：{changed_str}。\n\n供应商：`{provider_name}`\n模型：`{model_id or '未切换'}`\n已激活。"
        }]
    }


@tool(
    "AddCustomProvider",
    """新增自定义模型供应商（仅限机器人所有者操作）。
入参为 JSON 格式：
{
  "provider": "myProvider",      // 必填，供应商 ID（只允许大小写英文和数字）
  "api_key": "sk-xxx",           // 必填，API Key
  "base_url": "https://...",      // 必填，API 端点
  "models": ["model-1", "model-2"] // 必填，模型 ID 列表
}
说明：新增后可用 SetModel 切换到该供应商的模型。
```json
{"provider": "myProvider", "api_key": "sk-xxx", "base_url": "https://api.example.com/v1", "models": ["gpt-4", "gpt-3.5"]}
```
""",
    {"config": str},
)
async def add_custom_provider_tool(args: dict) -> dict:
    """新增自定义供应商"""
    if not _is_owner():
        return {
            "content": [{
                "type": "text",
                "text": "⚠️ 无权操作：新增供应商仅限机器人所有者。"
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
    api_key = cfg.get("api_key", "").strip()
    base_url = cfg.get("base_url", "").strip()
    models_raw = cfg.get("models", [])

    if not provider_id:
        return {"content": [{"type": "text", "text": "provider 是必填的"}], "is_error": True}
    if not api_key:
        return {"content": [{"type": "text", "text": "api_key 是必填的"}], "is_error": True}
    if not base_url:
        return {"content": [{"type": "text", "text": "base_url 是必填的"}], "is_error": True}
    if not models_raw or not isinstance(models_raw, list):
        return {"content": [{"type": "text", "text": "models 是必填的，必须是数组"}], "is_error": True}

    # 验证 provider_id 格式
    import re
    if not re.fullmatch(r'[a-zA-Z0-9]+', provider_id):
        return {"content": [{"type": "text", "text": "供应商名称只支持大小写英文字母和数字，不能包含特殊字符"}], "is_error": True}

    models = [m.strip() for m in models_raw if isinstance(m, str) and m.strip()]
    if not models:
        return {"content": [{"type": "text", "text": "models 不能为空"}], "is_error": True}

    ok, err = add_custom_provider(provider_id, api_key, base_url, models)
    if not ok:
        return {"content": [{"type": "text", "text": f"❌ 新增失败：{err}"}], "is_error": True}

    return {
        "content": [{
            "type": "text",
            "text": f"✅ 自定义供应商 `{provider_id}` 已添加。\n\nBase URL: `{base_url}`\n模型: {', '.join(f'`{m}`' for m in models)}\n\n可用 SetModel 切换到该供应商的模型。"
        }]
    }
