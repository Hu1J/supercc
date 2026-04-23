"""Interactive onboarding flow for first-time SuperCC setup."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from supercc.claude.model_config import (
    ModelEnv,
    ModelEntry,
    save_models_config,
    get_current_claude_settings,
)


def _input(prompt: str, default: str = "", password: bool = False) -> str:
    """Prompt for input with optional default."""
    if default:
        prompt = f"{prompt} [{default}]"
    prompt = f"{prompt}: "
    if password:
        import getpass
        return getpass.getpass(prompt) or default
    return input(prompt) or default


def _confirm(prompt: str, default: bool = False) -> bool:
    """Ask for yes/no confirmation."""
    suffix = " (Y/n)" if default else " (y/N)"
    answer = input(prompt + suffix + ": ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _print_step(step: int, total: int, title: str) -> None:
    """Print step header."""
    print(f"\n{'━' * 60}")
    print(f" Step {step}/{total}: {title}")
    print(f"{'━' * 60}\n")


def run_onboard_flow() -> bool:
    """Run the interactive onboard flow. Returns True if setup completed."""
    TOTAL_STEPS = 3

    print("\n🚀 SuperCC 首次安装引导\n")

    # ── Step 1: Model config ─────────────────────────────────────────────────
    _print_step(1, TOTAL_STEPS, "配置模型")
    print("请选择您的模型供应商，并提供 API Key\n")

    # Try to detect existing config
    existing_settings = get_current_claude_settings()
    env_cfg = existing_settings.get("env", {})
    detected_token = env_cfg.get("ANTHROPIC_AUTH_TOKEN", "")

    if detected_token:
        print(f"📋 检测到现有 Claude Code 配置")
        print(f"   模型: `{env_cfg.get('ANTHROPIC_MODEL', '未设置')}`")
        print(f"   端点: `{env_cfg.get('ANTHROPIC_BASE_URL', '未设置')}`")
        if _confirm("是否导入现有配置？", default=True):
            model_id = "default"
            name = f"导入配置 ({env_cfg.get('ANTHROPIC_MODEL', '未知')})"
            description = "从现有 Claude Code 配置导入"
            env = ModelEnv(
                ANTHROPIC_AUTH_TOKEN=detected_token,
                ANTHROPIC_BASE_URL=env_cfg.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                ANTHROPIC_MODEL=env_cfg.get("ANTHROPIC_MODEL", "claude-opus-4-5"),
            )
            # Save to models.yaml
            from supercc.claude.model_config import get_all_models, get_active_model
            models = get_all_models()
            models[model_id] = ModelEntry(
                name=name,
                description=description,
                env=env,
                is_default=True,
            )
            save_models_config(model_id, models)
            print("✅ 现有配置已导入为默认模型\n")
        else:
            _do_model_config_step()
    else:
        _do_model_config_step()

    # ── Step 2: Feishu config ─────────────────────────────────────────────────
    _print_step(2, TOTAL_STEPS, "配置飞书")
    print("请提供您的飞书应用配置信息\n")

    app_id = _input("飞书 App ID", default="")
    app_secret = _input("飞书 App Secret", password=True, default="")

    if not app_id or not app_secret:
        print("\n⚠️  飞书配置不完整，跳过（稍后可手动配置）")
        feishu_configured = False
    else:
        feishu_configured = True

    # ── Step 3: Proxy (optional) ─────────────────────────────────────────────
    _print_step(3, TOTAL_STEPS, "配置代理（可选）")
    print("如需使用代理，请配置以下信息（直接回车跳过）\n")

    use_proxy = _confirm("是否使用代理？", default=False)
    proxy_url = ""
    if use_proxy:
        proxy_url = _input("代理 URL", default="")

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
    if use_proxy and proxy_url:
        print(f"代理: {proxy_url}")
    else:
        print("代理: 未使用")

    print()

    if not _confirm("确认写入配置？", default=True):
        print("\n❌ 已取消安装引导")
        return False

    # ── Save feishu config ────────────────────────────────────────────────────
    if feishu_configured:
        from supercc.install.flow import save_config
        from supercc.install.api import AppRegistrationResult

        # Build a minimal AppRegistrationResult for save_config
        class FakeResult:
            def __init__(self, app_id, app_secret):
                self.app_id = app_id
                self.app_secret = app_secret
                self.user_open_id = ""
                self.domain = ""

        fake_result = FakeResult(app_id, app_secret)

        # Determine config path
        from supercc.config import resolve_config_path, init_config, get_config
        try:
            cfg_path, data_dir = resolve_config_path()
        except Exception:
            # Use default path in current directory
            cfg_path = os.path.join(os.getcwd(), "config.yaml")
            data_dir = os.path.join(os.getcwd(), ".supercc")

        Path(cfg_path).parent.mkdir(parents=True, exist_ok=True)
        save_config(fake_result, cfg_path, bypass_accepted=True)
        print(f"✅ 飞书配置已保存")

    # ── Save proxy config ─────────────────────────────────────────────────────
    if use_proxy and proxy_url:
        # Save proxy to config.yaml via the singleton
        from supercc.config import init_config, get_config, write_config
        try:
            cfg_path, _ = resolve_config_path()
            init_config(cfg_path)
            cfg = get_config()
            cfg.http_proxy = proxy_url
            write_config(cfg)
            print("✅ 代理配置已保存")
        except Exception:
            print("⚠️  代理配置保存失败（config.yaml 可能尚未初始化）")

    print("\n" + "=" * 60)
    print("✅ SuperCC 安装引导完成！")
    print("=" * 60)
    print()
    print("下一步：")
    print("  • 使用 `supercc start` 启动 SuperCC")
    print("  • 使用 `supercc config list` 查看模型配置")
    print("  • 使用 `supercc config switch <model_id>` 切换模型")
    print()

    return True


def _do_model_config_step() -> None:
    """Handle the model configuration step with provider selection."""
    from supercc.claude.model_providers import PROVIDERS

    # Step 1: 选择供应商
    print("请选择模型供应商：\n")
    provider_list = list(PROVIDERS.items())
    for i, (pid, p) in enumerate(provider_list, 1):
        auth_display = {"bearer": "Bearer Token", "api_key": "API Key", "azure": "Azure AD Token"}.get(p.auth_type, p.auth_type)
        print(f"  {i}. {p.name}")
        print(f"     端点: {p.base_url or '(用户填入)'}")
        print(f"     认证: {auth_display}")
        print()

    print(f"  0. 跳过（稍后手动配置）")
    print()

    choice_str = _input("请输入编号", default="")
    if not choice_str or choice_str == "0":
        print("\n⚠️  跳过模型配置（后续可使用 `supercc config add` 添加）")
        return

    try:
        idx = int(choice_str) - 1
        if idx < 0 or idx >= len(provider_list):
            raise ValueError()
        provider_id, provider = provider_list[idx]
    except ValueError:
        print("❌ 无效的选择")
        return

    # Step 2: 输入 API Key
    auth_display = {"bearer": "Bearer Token", "api_key": "API Key", "azure": "Azure AD Token"}.get(provider.auth_type, provider.auth_type)
    print(f"\n已选择: {provider.name}\n")
    token = _input(f"API Key（{auth_display}）", password=True, default="")
    if not token:
        print("\n⚠️  未提供 API Key，跳过模型配置")
        return

    # Step 3: 选择模型
    print(f"\n可用模型：\n")
    for i, m in enumerate(provider.models, 1):
        default_mark = " ← 默认" if i == 1 else ""
        print(f"  {i}. `{m}`{default_mark}")

    model_choice_str = _input("请输入模型编号（或直接回车使用默认）", default="1")
    try:
        model_idx = int(model_choice_str) - 1 if model_choice_str else 0
        if model_idx < 0 or model_idx >= len(provider.models):
            model_idx = 0
    except ValueError:
        model_idx = 0

    selected_model_id = provider.models[model_idx]

    # 保存配置
    model_id = provider_id
    name = f"{provider.name} ({selected_model_id})"
    description = f"供应商: {provider.name}"

    env = ModelEnv(
        ANTHROPIC_AUTH_TOKEN=token,
        ANTHROPIC_BASE_URL=provider.base_url,
        ANTHROPIC_MODEL=selected_model_id,
    )

    from supercc.claude.model_config import get_all_models, save_models_config
    models = get_all_models()
    models[model_id] = ModelEntry(
        name=name,
        description=description,
        env=env,
        is_default=True,
    )
    save_models_config(model_id, models)
    print(f"\n✅ 模型配置已保存")
    print(f"   供应商: {provider.name}")
    print(f"   模型: `{selected_model_id}`")
    print(f"   端点: {provider.base_url}")
