"""Interactive onboarding flow for first-time SuperCC setup."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import questionary

from supercc.core.models.model_config import (
    ModelEnv,
    init_model_env,
    update_provider_api_key,
    set_project_model,
    validate_model_env,
    get_all_providers,
    is_configured,
)
from supercc.core.models.model_providers import PROVIDERS


def _print_step(step: int, total: int, title: str) -> None:
    """Print step header."""
    print(f"\n{'━' * 60}")
    print(f" Step {step}/{total}: {title}")
    print(f"{'━' * 60}\n")


def run_onboard_flow() -> bool:
    """Run the interactive onboard flow. Returns True if setup completed."""
    TOTAL_STEPS = 4

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

    _do_model_config_step()

    # ── Step 2: Platform selection ─────────────────────────────────────────────
    import asyncio
    from supercc.config import resolve_config_path
    try:
        cfg_path, data_dir = resolve_config_path()
    except Exception:
        cfg_path = os.path.join(os.getcwd(), "config.json")
        data_dir = os.path.join(os.getcwd(), ".supercc")

    # 检测已配置的平台
    _feishu_has_app = bool(os.path.exists(cfg_path))
    _feishu_has_app = False
    _wecom_has_corp = False
    try:
        from supercc.config import init_config, get_config
        init_config(cfg_path)
        cfg = get_config()
        feishu_cfg = getattr(cfg.channels, "feishu", None)
        wecom_cfg = getattr(cfg.channels, "wecom", None)
        _feishu_has_app = bool(feishu_cfg and getattr(feishu_cfg, "app_id", ""))
        _wecom_has_corp = bool(wecom_cfg and getattr(wecom_cfg, "corp_id", ""))
    except Exception:
        pass

    _print_step(2, TOTAL_STEPS, "选择平台")

    platform_choices = [
        questionary.Choice("飞书 (Feishu)" + ("  ✅ 已配置" if _feishu_has_app else ""), value="feishu"),
        questionary.Choice("企业微信 (WeCom)" + ("  ✅ 已配置" if _wecom_has_corp else ""), value="wecom"),
        questionary.Choice("⏭  跳过（稍后手动配置）", value="skip"),
    ]

    platform_choice = questionary.select(
        "请选择要配置的聊天平台（后续可随时通过 `supercc config` 修改）",
        choices=platform_choices,
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask()

    if platform_choice == "skip":
        print("\n⏭  跳过平台配置\n")
        feishu_configured = _feishu_has_app
        wecom_configured = _wecom_has_corp
    elif (platform_choice == "feishu" and _feishu_has_app) or (platform_choice == "wecom" and _wecom_has_corp):
        # 已配置的平台：提供管理选项
        platform_name = "飞书" if platform_choice == "feishu" else "企业微信"
        manage_choice = questionary.select(
            f"{platform_name} 已配置，请选择操作",
            choices=[
                questionary.Choice("🔄  重新配置", value="reconfigure"),
                questionary.Choice("🗑  删除配置", value="delete"),
                questionary.Choice("↩️  返回上一级", value="back"),
            ],
            style=questionary.Style([
                ("selected", "fg:#00AA00 bold"),
                ("choice", "fg:#CCCCCC"),
                ("pointer", "fg:#00AA00 bold"),
            ]),
        ).ask()

        if manage_choice == "back" or manage_choice is None:
            print("\n⏭  跳过平台配置\n")
            feishu_configured = _feishu_has_app
            wecom_configured = _wecom_has_corp
        elif manage_choice == "delete":
            # 清除配置
            try:
                from supercc.config import init_config, get_config, write_config
                init_config(cfg_path)
                cfg = get_config()
                channel = getattr(cfg.channels, platform_choice, None)
                if channel:
                    channel.app_id = ""
                    channel.app_secret = ""
                    channel.bot_open_id = ""
                    channel.enabled = False
                write_config(cfg)
                print(f"✅ {platform_name} 配置已删除\n")
            except Exception as e:
                print(f"⚠️  删除失败：{e}\n")
            feishu_configured = _feishu_has_app if platform_choice != "feishu" else False
            wecom_configured = _wecom_has_corp if platform_choice != "wecom" else False
        else:
            # reconfigure: 重新走安装流程
            feishu_configured = False
            wecom_configured = False
            _print_step(3, TOTAL_STEPS, "配置平台")
            if platform_choice == "feishu":
                try:
                    from supercc.install.flow import run_install_flow
                    asyncio.run(run_install_flow(cfg_path, bypass_accepted=True))
                    print("✅ 飞书配置完成\n")
                    feishu_configured = True
                except Exception as e:
                    print(f"⚠️  飞书配置出错：{e}（稍后可手动配置）\n")
            else:
                print("\n正在安装企业微信 SDK...\n")
                import subprocess
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "wecom-aibot-sdk-python", "--quiet"],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    print(f"⚠️  企业微信 SDK 安装失败：{result.stderr.strip()}\n")
                else:
                    print("✅ 企业微信 SDK 安装完成\n")
                print("请按照提示输入企业微信凭证...\n")
                try:
                    from supercc.install.wecom_flow import run_wecom_install_flow
                    run_wecom_install_flow(cfg_path, bypass_accepted=True)
                    print("✅ 企业微信配置完成\n")
                    wecom_configured = True
                except Exception as e:
                    print(f"⚠️  企业微信配置出错：{e}（稍后可手动配置）\n")
    else:
        # 未配置的平台：正常流程
        _print_step(3, TOTAL_STEPS, "配置平台")

        Path(cfg_path).parent.mkdir(parents=True, exist_ok=True)
        feishu_configured = False
        wecom_configured = False

        if platform_choice == "feishu":
            try:
                from supercc.install.flow import run_install_flow
                asyncio.run(run_install_flow(cfg_path, bypass_accepted=True))
                print("✅ 飞书配置完成\n")
                feishu_configured = True
            except Exception as e:
                print(f"⚠️  飞书配置出错：{e}（稍后可手动配置）\n")

        elif platform_choice == "wecom":
            print("\n正在安装企业微信 SDK...\n")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "wecom-aibot-sdk-python", "--quiet"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"⚠️  企业微信 SDK 安装失败：{result.stderr.strip()}（请手动运行 pip install wecom-aibot-sdk-python）\n")
            else:
                print("✅ 企业微信 SDK 安装完成\n")

            print("请按照提示输入企业微信凭证...\n")
            try:
                from supercc.install.wecom_flow import run_wecom_install_flow
                run_wecom_install_flow(cfg_path, bypass_accepted=True)
                print("✅ 企业微信配置完成\n")
                wecom_configured = True
            except Exception as e:
                print(f"⚠️  企业微信配置出错：{e}（稍后可手动配置）\n")

    # ── Auth mode selection (Plugin→Core) ────────────────────────────────────
    _print_step(3, TOTAL_STEPS, "配置认证方式")

    import secrets

    auth_choices = questionary.checkbox(
        "选择 plugin 连接 core WS 时的认证方式",
        choices=[
            questionary.Choice("Token 认证（自动生成，推荐）", value="token", checked=True),
            questionary.Choice("账号密码认证（需设置用户名和密码）", value="password"),
        ],
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask() or []

    token = ""
    username = ""
    password = ""

    if "token" in auth_choices:
        token = secrets.token_urlsafe(32)
        print(f"已生成 Token: {token}")

    if "password" in auth_choices:
        while True:
            username = questionary.text("请输入用户名", style=questionary.Style([("input", "fg:#CCCCCC")])).ask() or ""
            if username:
                break
            print("⚠️  用户名不能为空，请重新输入\n")
        while True:
            password = questionary.password("请输入密码", style=questionary.Style([("password", "fg:#CCCCCC")])).ask() or ""
            if password:
                break
            print("⚠️  密码不能为空，请重新输入\n")

    # ── Core listen address/port ───────────────────────────────────────────────
    _print_step(4, TOTAL_STEPS, "配置监听地址")

    listen_choice = questionary.select(
        "选择 SuperCC Core 的监听地址（plugin 通过此地址连接）",
        choices=[
            questionary.Choice("127.0.0.1:28888（推荐，仅本机可访问）", value="1"),
            questionary.Choice("0.0.0.0:28888（对外部开放）", value="2"),
        ],
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask() or "1"

    if listen_choice == "2":
        core_host = "0.0.0.0"
        core_port = 28888
        print("已设置: 0.0.0.0:28888（对外部开放）")
    else:
        core_host = "127.0.0.1"
        core_port = 28888
        print("已设置: 127.0.0.1:28888（仅本机）")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'━' * 60}")
    print(" 确认配置")
    print(f"{'━' * 60}\n")

    from supercc.core.models.model_config import get_active_model_for_project
    project_path = str(Path(data_dir).resolve().parent)  # 项目根路径
    pid, mid = get_active_model_for_project(project_path)
    if pid:
        provider = PROVIDERS.get(pid)
        print(f"模型: {provider.id if provider else pid} @ `{mid}`")
    else:
        print("模型: 未配置")
    print(f"飞书: {'已配置' if feishu_configured else '未配置'}")
    print(f"企业微信: {'已配置' if wecom_configured else '未配置'}")

    if token:
        print(f"认证: Token（{token[:8]}...）")
    if username:
        print(f"认证: 用户名密码（{username}）")
    print(f"监听: {core_host}:{core_port}")

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

    # ── Save bypass accepted + auth config ────────────────────────────────────
    from supercc.config import init_config, accept_bypass_warning, get_config, write_config
    try:
        cfg_path, _ = resolve_config_path()
        init_config(cfg_path)
        cfg = get_config()
        cfg.core.token = token
        cfg.core.username = username
        cfg.core.password = password
        cfg.core.host = core_host
        cfg.core.port = core_port
        accept_bypass_warning(cfg_path)
        write_config(cfg)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("✅ SuperCC 安装引导完成！")
    print("=" * 60)
    print()
    print("下一步：")
    print("  • 使用 `supercc gateway run` 启动 SuperCC")
    print("  • 使用 `supercc config` 管理模型配置")
    print("  • 查看插件状态请编辑 config.json 中 channels.xxx.enabled 字段")
    print()

    return True


def _do_model_config_step() -> None:
    """Handle the model configuration step with provider selection (TUI)."""
    # resolve_config_path() 返回 (cfg_path, data_dir)，cfg_path = {project}/.supercc/config.json
    # set_project_model 需要项目根路径，所以要取 cfg_path 的 parent.parent
    try:
        cfg_path, _ = resolve_config_path()
        project_path = str(Path(cfg_path).resolve().parent.parent)
    except Exception:
        project_path = os.getcwd()

    # 获取所有供应商及其配置状态
    all_providers = get_all_providers()

    # 分类：已配置的放前面，未配置的放后面
    configured = []
    unconfigured = []

    # 预置供应商
    for pid, p in PROVIDERS.items():
        pdata = all_providers.get(pid)
        has_key = pdata and pdata.api_key
        if has_key:
            configured.append((pid, p))
        else:
            unconfigured.append((pid, p))

    # 自定义供应商（存在于 all_providers 但不在 PROVIDERS 中）
    from dataclasses import dataclass

    @dataclass
    class _CustomProvider:
        id: str
        base_url: str
        models: list
        auth_type: str = "bearer"

    for pid in all_providers:
        if pid in PROVIDERS:
            continue
        pdata = all_providers.get(pid)
        if pdata and pdata.api_key:
            configured.append((pid, _CustomProvider(
                id=pid,
                base_url=pdata.base_url or "",
                models=pdata.models or [],
            )))

    # 构建选项：已配置的显示 "(已配置)"，未配置的不显示
    provider_choices = []
    default_index = 0

    for i, (pid, p) in enumerate(configured):
        provider_choices.append(questionary.Choice(
            f"{p.id}  ({p.base_url or '用户填入'})  （已配置）",
            value=pid,
        ))

    for pid, p in unconfigured:
        provider_choices.append(questionary.Choice(
            f"{p.id}  ({p.base_url or '用户填入'})",
            value=pid,
        ))

    # 新增自定义供应商
    provider_choices.append(questionary.Choice("─" * 40, value="__separator__", disabled=True))
    provider_choices.append(questionary.Choice(
        "✨ 新增自定义供应商",
        value="__add_custom__",
    ))

    if configured:
        provider_choices.append(questionary.Choice("─" * 40, value="__sep2__", disabled=True))
    provider_choices.append(questionary.Choice("⏭  跳过（稍后手动配置）", value="__skip__"))

    provider_id = questionary.select(
        "请选择模型供应商（已配置的供应商会自动跳过 API Key 输入）",
        choices=provider_choices,
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
            ("separator", "fg:#555555"),
        ]),
    ).ask()

    if not provider_id or provider_id == "__skip__":
        print("\n⚠️  跳过模型配置（后续可使用 `supercc config` 添加）\n")
        return

    # ── 新增自定义供应商模式 ───────────────────────────────────────────────
    if provider_id == "__add_custom__":
        base_url = questionary.text(
            "Base URL（例如 https://api.example.com/v1）",
            style=questionary.Style([("input", "fg:#CCCCCC")]),
        ).ask()
        if not base_url:
            print("\n⚠️  未提供 Base URL，跳过模型配置\n")
            return
        base_url = base_url.strip().rstrip("/")

        selected_model = questionary.text(
            "模型 ID（例如 gpt-4、my-model）",
            style=questionary.Style([("input", "fg:#CCCCCC")]),
        ).ask()
        if not selected_model:
            print("\n⚠️  未提供模型 ID，跳过\n")
            return
        selected_model = selected_model.strip()

        token = questionary.password(
            "API Key",
            style=questionary.Style([("password", "fg:#CCCCCC")]),
        ).ask()
        if not token:
            print("\n⚠️  未提供 API Key，跳过模型配置\n")
            return

        provider_name_raw = questionary.text(
            "供应商名称（例如 myProvider）",
            style=questionary.Style([("input", "fg:#CCCCCC")]),
        ).ask()
        provider_name = provider_name_raw.strip() if provider_name_raw else "custom"

        # 验证 provider_name 只允许大小写英文+数字，防止注入
        import re
        if not re.fullmatch(r'[a-zA-Z0-9]+', provider_name):
            print("\n❌ 供应商名称只支持大小写英文字母和数字，不能包含特殊字符\n")
            return

        # 验证不能与预置供应商名称冲突
        if provider_name in PROVIDERS:
            print(f"\n❌ 供应商名称 '{provider_name}' 是内置供应商名称，请使用其他名称\n")
            return

        env = ModelEnv(
            ANTHROPIC_AUTH_TOKEN=token,
            ANTHROPIC_BASE_URL=base_url,
            ANTHROPIC_MODEL=selected_model,
        )

        while True:
            valid, err_msg = validate_model_env(env)
            if valid:
                break
            print(f"\n❌ API 验证失败: {err_msg}")
            retry = questionary.confirm("是否重新输入 API Key？", default=True).ask()
            if not retry:
                print("\n⚠️  跳过模型配置\n")
                return
            token = questionary.password("API Key", style=questionary.Style([("password", "fg:#CCCCCC")])).ask()
            if not token:
                print("\n⚠️  未提供 API Key，跳过模型配置\n")
                return
            env.ANTHROPIC_AUTH_TOKEN = token

        # 自定义供应商存到 model.json
        from supercc.core.models.model_config import _load_json, _save_json
        raw = _load_json()
        providers_raw = raw.get("providers", {})
        providers_raw[provider_name] = {
            "api_key": token,
            "models": [selected_model],
            "base_url": base_url,
            "provider_name": provider_name,
        }
        raw["providers"] = providers_raw
        _save_json(raw)

        # 设置项目激活映射
        set_project_model(project_path, provider_name, selected_model)
        init_model_env(project_path)

        print(f"\n✅ 自定义供应商配置已保存")
        print(f"   供应商: {provider_name}")
        print(f"   Base URL: {base_url}")
        print(f"   模型: `{selected_model}`\n")
        return

    # 获取 provider 对象（预置供应商从 PROVIDERS，自定义供应商从 all_providers）
    provider = PROVIDERS.get(provider_id) if provider_id in PROVIDERS else None
    if not provider:
        # 自定义供应商（存在于 all_providers 但不在 PROVIDERS 中）
        pdata = all_providers.get(provider_id)
        if pdata:
            provider = _CustomProvider(
                id=provider_id,
                base_url=pdata.base_url or "",
                models=pdata.models or [],
            )

    # ── 预置/自定义供应商模式 ───────────────────────────────────────────────

    auth_display = {"bearer": "Bearer API Key", "api_key": "API Key", "azure": "Azure AD Token"}.get(provider.auth_type, provider.auth_type)
    pdata = all_providers.get(provider_id)
    has_existing_key = pdata and pdata.api_key

    # 如果该供应商已有 API Key，询问用户是否需要更新
    if has_existing_key:
        update_key = questionary.confirm(
            f"检测到 {provider_id} 已配置 API Key，是否要更新？",
            default=False,
            style=questionary.Style([("selected", "fg:#00AA00 bold")]),
        ).ask()
        if update_key:
            token = questionary.password(
                f"新的 API Key（{auth_display}）",
                style=questionary.Style([("password", "fg:#CCCCCC")]),
            ).ask()
            if not token:
                print("\n⚠️  未提供 API Key，跳过模型配置\n")
                return
        else:
            token = pdata.api_key
    else:
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
        f"请选择模型（{provider.id}）",
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
            print("\n⚠️  跳过模型配置（后续可使用 `supercc config` 添加）\n")
            return
        token = questionary.password(
            f"API Key（{auth_display}）",
            style=questionary.Style([("password", "fg:#CCCCCC")]),
        ).ask()
        if not token:
            print("\n⚠️  未提供 API Key，跳过模型配置\n")
            return
        env.ANTHROPIC_AUTH_TOKEN = token

    # 保存配置：更新供应商 API Key + 设置项目激活映射
    ok, err = update_provider_api_key(provider_id, token)
    if not ok:
        print(f"\n❌ API Key 验证失败: {err}\n")
        return
    set_project_model(project_path, provider_id, selected_model)
    init_model_env(project_path)  # 刷新全局单例

    print(f"\n✅ 模型配置已保存")
    print(f"   供应商: {provider.id}")
    print(f"   模型: `{selected_model}`")
    print(f"   端点: {provider.base_url}\n")
