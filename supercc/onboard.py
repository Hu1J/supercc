"""Interactive onboarding flow for first-time SuperCC setup."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import questionary

from supercc.claude.model_config import (
    ModelEnv,
    ModelEntry,
    save_models_config,
    get_current_claude_settings,
)


def _print_step(step: int, total: int, title: str) -> None:
    """Print step header."""
    print(f"\n{'━' * 60}")
    print(f" Step {step}/{total}: {title}")
    print(f"{'━' * 60}\n")


def run_onboard_flow() -> bool:
    """Run the interactive onboard flow. Returns True if setup completed."""
    TOTAL_STEPS = 2

    print("\n🐲 SuperCC 项目初始化引导\n")

    # ── Risk warning ─────────────────────────────────────────────────────────
    print("⚠️  安全风险警告\n")
    print("supercc 以 bypassPermissions 模式运行。")
    print("Claude Code 可以执行任意终端命令、读写本地文件，无需每次授权确认。")
    print("这意味着如果有人通过飞书向机器人发送恶意指令，攻击者可以：")
    print("  • 在你的电脑上执行任意命令")
    print("  • 读取、修改或删除你的本地文件")
    print("  • 访问你的敏感信息\n")
    print("请仅在可信任的网络环境下使用本工具。\n")

    accept = questionary.confirm(
        "我了解风险并确认继续",
        default=False,
        style=questionary.Style([
            ("selected", "fg:#FF5555 bold"),
        ]),
    ).ask()

    if not accept:
        print("\n❌ 已取消安装引导")
        return False

    # ── Step 1: Model config ─────────────────────────────────────────────────
    _print_step(1, TOTAL_STEPS, "配置模型")
    print("请选择您的模型供应商，并提供 API Key\n")

    existing_settings = get_current_claude_settings()
    env_cfg = existing_settings.get("env", {})
    detected_token = env_cfg.get("ANTHROPIC_AUTH_TOKEN", "")

    if detected_token:
        print(f"📋 检测到现有 Claude Code 配置")
        print(f"   模型: `{env_cfg.get('ANTHROPIC_MODEL', '未设置')}`")
        print(f"   端点: `{env_cfg.get('ANTHROPIC_BASE_URL', '未设置')}`\n")

        import_to_current = questionary.confirm(
            "是否导入现有配置？",
            default=True,
            style=questionary.Style([
                ("selected", "fg:#00AA00 bold"),
            ]),
        ).ask()

        if import_to_current:
            model_id = "default"
            name = f"导入配置 ({env_cfg.get('ANTHROPIC_MODEL', '未知')})"
            description = "从现有 Claude Code 配置导入"
            env = ModelEnv(
                ANTHROPIC_AUTH_TOKEN=detected_token,
                ANTHROPIC_BASE_URL=env_cfg.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                ANTHROPIC_MODEL=env_cfg.get("ANTHROPIC_MODEL", "claude-opus-4-5"),
            )
            from supercc.claude.model_config import get_all_models
            models = get_all_models()
            models[model_id] = ModelEntry(
                name=name,
                description=description,
                env=env,
                is_default=True,
            )
            save_models_config(model_id, models)
            # 同步更新内存中的 active_model_id，避免被 load_models_config 覆盖
            from supercc.claude import model_config
            model_config._active_model_id = model_id
            print("✅ 现有配置已导入为默认模型\n")
        else:
            _do_model_config_step()
    else:
        _do_model_config_step()

    # ── Step 2: Feishu config ─────────────────────────────────────────────────
    _print_step(2, TOTAL_STEPS, "配置飞书")
    print("扫码登录飞书应用...\n")

    import asyncio
    from supercc.install.flow import run_install_flow
    from supercc.config import resolve_config_path

    try:
        cfg_path, data_dir = resolve_config_path()
    except Exception:
        cfg_path = os.path.join(os.getcwd(), "config.yaml")
        data_dir = os.path.join(os.getcwd(), ".supercc")

    Path(cfg_path).parent.mkdir(parents=True, exist_ok=True)
    feishu_ok = asyncio.run(run_install_flow(cfg_path, bypass_accepted=True))

    if feishu_ok:
        print("✅ 飞书配置完成\n")
        feishu_configured = True
    else:
        print("⚠️  飞书配置未完成（稍后可手动配置）\n")
        feishu_configured = False

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'━' * 60}")
    print(" 确认配置")
    print(f"{'━' * 60}\n")

    from supercc.claude.model_config import get_all_models, get_active_model
    models = get_all_models()
    active = get_active_model()
    if active:
        print(f"模型: {active.name} @ {active.env.ANTHROPIC_BASE_URL}")
        print(f"     模型 ID: `{active.env.ANTHROPIC_MODEL}`")
    print(f"飞书: {'已配置' if feishu_configured else '未配置'}")

    print()

    confirm = questionary.confirm(
        "确认写入配置？",
        default=True,
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
        ]),
    ).ask()

    if not confirm:
        print("\n❌ 已取消安装引导")
        return False

    # ── Save bypass accepted ──────────────────────────────────────────────────
    from supercc.config import init_config, get_config, write_config, accept_bypass_warning
    try:
        cfg_path, _ = resolve_config_path()
        init_config(cfg_path)
        accept_bypass_warning(cfg_path)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("✅ SuperCC 安装引导完成！")
    print("=" * 60)
    print()
    print("下一步：")
    print("  • 使用 `supercc start` 启动 SuperCC")
    print("  • 使用 `supercc config` 管理模型配置")
    print()

    return True


def _do_model_config_step() -> None:
    """Handle the model configuration step with provider selection (TUI)."""
    from supercc.claude.model_providers import PROVIDERS

    # Step 1: 选择供应商
    provider_choices = [
        questionary.Choice(
            f"{p.name}  ({p.base_url or '用户填入'})",
            value=pid,
        )
        for pid, p in PROVIDERS.items()
    ]
    provider_choices.append(questionary.Choice("⏭  跳过（稍后手动配置）", value="__skip__"))

    provider_id = questionary.select(
        "请选择模型供应商",
        choices=provider_choices,
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask()

    if not provider_id or provider_id == "__skip__":
        print("\n⚠️  跳过模型配置（后续可使用 `supercc config add` 添加）\n")
        return

    provider = PROVIDERS[provider_id]
    auth_display = {"bearer": "Bearer API Key", "api_key": "API Key", "azure": "Azure AD Token"}.get(provider.auth_type, provider.auth_type)

    # Step 2: 输入 API Key
    token = questionary.password(
        f"API Key（{auth_display}）",
        style=questionary.Style([("password", "fg:#CCCCCC")]),
    ).ask()

    if not token:
        print("\n⚠️  未提供 API Key，跳过模型配置\n")
        return

    # Step 3: 选择模型
    model_choices = [
        questionary.Choice(f"`{m}`", value=m)
        for m in provider.models
    ]
    selected_model = questionary.select(
        f"请选择模型（{provider.name}）",
        choices=model_choices,
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask()

    if not selected_model:
        print("\n⚠️  未选择模型，跳过\n")
        return

    # Step 4: 验证 API Key + 模型是否可用
    env = ModelEnv(
        ANTHROPIC_AUTH_TOKEN=token,
        ANTHROPIC_BASE_URL=provider.base_url,
        ANTHROPIC_MODEL=selected_model,
    )

    from supercc.claude.model_config import validate_model_env
    while True:
        valid, err_msg = validate_model_env(env)
        if valid:
            break
        print(f"\n❌ API 验证失败: {err_msg}")
        retry = questionary.confirm(
            "是否重新输入 API Key？",
            default=True,
        ).ask()
        if not retry:
            print("\n⚠️  跳过模型配置（后续可使用 `supercc config add` 添加）\n")
            return
        token = questionary.password(
            f"API Key（{auth_display}）",
            style=questionary.Style([("password", "fg:#CCCCCC")]),
        ).ask()
        if not token:
            print("\n⚠️  未提供 API Key，跳过模型配置\n")
            return
        env.ANTHROPIC_AUTH_TOKEN = token

    # 保存配置
    model_id = provider_id
    name = f"{provider.name} ({selected_model})"
    description = f"供应商: {provider.name}"

    from supercc.claude.model_config import get_all_models, save_models_config, switch_model, _models_cache
    models = get_all_models()
    models[model_id] = ModelEntry(
        name=name,
        description=description,
        env=env,
        is_default=True,
    )
    _models_cache.update(models)  # 先同步到内存 cache
    save_models_config(model_id, models)
    switch_model(model_id)  # 设置为激活模型，同步写入 Claude 内部配置
    print(f"\n✅ 模型配置已保存")
    print(f"   供应商: {provider.name}")
    print(f"   模型: `{selected_model}`")
    print(f"   端点: {provider.base_url}\n")
