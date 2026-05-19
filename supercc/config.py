"""Configuration loading and validation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict

import yaml  # 仅用于旧版 YAML 配置迁移

# sessions.db 固定放在家目录下，不同项目通过 session.project_path 区分
SESSIONS_DB_PATH = str(Path.home() / ".supercc" / "sessions.db")

# ── 全局单例 ──────────────────────────────────────────────────────────────────
_cfg_instance: Config | None = None


def init_config(path: str, data_dir: str = "") -> Config:
    """初始化全局单例 Config 对象。

    应用启动时调用一次，之后所有地方用 get_config() 获取同一对象。
    所有变更直接修改返回的 cfg 对象，最后 write_config(cfg) 写回磁盘。
    """
    global _cfg_instance
    _cfg_instance = load_config(path, data_dir)
    _cfg_instance.cfg_path = path  # attach path for reload_config
    return _cfg_instance


def get_config() -> Config:
    """获取全局单例 Config 对象。

    必须在 init_config() 之后调用。
    """
    if _cfg_instance is None:
        raise RuntimeError("Config not initialized. Call init_config(path) first.")
    return _cfg_instance


def reload_config() -> Config:
    """重新从磁盘加载 config.json，丢弃当前内存缓存。

    用于 external 修改了 config.json 后让当前进程看到最新数据（如 pairing approve 后）。
    """
    global _cfg_instance
    if _cfg_instance is None:
        raise RuntimeError("Config not initialized. Call init_config(path) first.")
    cfg_path = getattr(_cfg_instance, "cfg_path", None)
    if not cfg_path:
        raise RuntimeError("Config path unknown. Call init_config(path) first.")
    data_dir = getattr(_cfg_instance, "data_dir", "") or ""
    _cfg_instance = load_config(cfg_path, data_dir)
    _cfg_instance.cfg_path = cfg_path
    return _cfg_instance


def write_config(cfg: Config) -> None:
    """将 cfg 对象写回磁盘（使用 init_config 时保存的路径）。

    所有对 config 的变更完成后调用此方法持久化。
    """
    cfg_path = getattr(cfg, "cfg_path", None)
    if not cfg_path:
        raise RuntimeError("Config path unknown. Call init_config(path) first.")
    _write_config_to_path(cfg_path, cfg)


@dataclass
class GroupConfigEntry:
    """Per-group configuration for group chat access control."""
    enabled: bool = True              # 是否启用该群
    require_mention: bool = True     # 是否必须 @CC 才响应
    allow_from: list[str] = field(default_factory=list)  # 白名单 open_id（空=不限）


# Type alias for group config dict: group_id -> GroupConfigEntry
GroupConfig = Dict[str, GroupConfigEntry]


@dataclass
class FeishuChannelConfig:
    enabled: bool = True
    app_id: str = ""
    app_secret: str = ""
    bot_name: str = "Claude"
    bot_open_id: str = ""        # 机器人的 open_id，用于检测群聊 @CC
    domain: str = "feishu"
    groups: dict = field(default_factory=dict)  # group_id -> GroupConfigEntry dict
    allowed_users: List[str] = field(default_factory=list)  # 飞书 P2P 白名单


@dataclass
class DingTalkChannelConfig:
    enabled: bool = False
    app_key: str = ""
    app_secret: str = ""
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class WeComChannelConfig:
    """企业微信（WeCom）插件配置。Phase 3 新增。"""
    enabled: bool = False
    corp_id: str = ""
    agent_id: str = ""
    corp_secret: str = ""
    # 扫码接入获得的 WebSocket 凭证（优先级高于 agent_id/corp_secret）
    bot_id: str = ""
    secret: str = ""
    bot_name: str = "Claude"
    groups: dict = field(default_factory=dict)
    allowed_users: list = field(default_factory=list)


@dataclass
class TelegramChannelConfig:
    """Telegram 插件配置。"""
    enabled: bool = False
    bot_token: str = ""
    bot_name: str = "Claude"
    groups: dict = field(default_factory=dict)
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class QQChannelConfig:
    """QQ 插件配置。"""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    bot_openid: str = ""
    groups: dict = field(default_factory=dict)
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class WhatsAppChannelConfig:
    """WhatsApp 插件配置（需要 Node.js bridge）。"""
    enabled: bool = False
    bridge_port: int = 3000
    session_dir: str = ""
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class WeChatChannelConfig:
    """微信个人（iLink）插件配置。"""
    enabled: bool = False
    token: str = ""
    account_id: str = ""
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class ChannelsConfig:
    feishu: FeishuChannelConfig = field(default_factory=FeishuChannelConfig)
    dingtalk: DingTalkChannelConfig = field(default_factory=DingTalkChannelConfig)
    wecom: WeComChannelConfig = field(default_factory=WeComChannelConfig)
    telegram: TelegramChannelConfig = field(default_factory=TelegramChannelConfig)
    qq: QQChannelConfig = field(default_factory=QQChannelConfig)
    whatsapp: WhatsAppChannelConfig = field(default_factory=WhatsAppChannelConfig)
    wechat: WeChatChannelConfig = field(default_factory=WeChatChannelConfig)


@dataclass
class AuthConfig:
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class ClaudeConfig:
    cli_path: str = "claude"
    max_turns: int = 50
    approved_directory: str = ""


@dataclass
class CodexCaptureConfig:
    capture_mode: bool = True


@dataclass
class CodexMcpConfig:
    enabled: bool = True
    cli_path: str = ""
    model: str = "gpt-5.5"
    sandbox: str = "workspace-write"
    approval: str = "on-request"
    auto_configure_mcp: bool = True
    capture: CodexCaptureConfig = field(default_factory=CodexCaptureConfig)


@dataclass
class SkillNudgeConfig:
    enabled: bool = True
    interval: int = 10
    current_user: str = ""  # matched against skill author for auto-evolve


@dataclass
class VerboseChannelEntry:
    """Per-chat-id verbose settings for a specific platform."""
    evolve: bool = True
    step: bool = True


@dataclass
class CoreConfig:
    host: str = "127.0.0.1"
    port: int = 28888
    token: str = ""           # Plugin 连接凭证
    username: str = ""        # 账号密码模式
    password: str = ""        # 账号密码模式


@dataclass
class Config:
    channels: ChannelsConfig
    auth: AuthConfig
    claude: ClaudeConfig
    core: CoreConfig = field(default_factory=CoreConfig)
    codex: CodexMcpConfig = field(default_factory=CodexMcpConfig)
    skill_nudge: SkillNudgeConfig = field(default_factory=SkillNudgeConfig)
    verbose: dict[str, dict[str, VerboseChannelEntry]] = field(default_factory=dict)
    data_dir: str = ""
    bypass_accepted: bool = False
    daemon: bool = False


def _migrate_yaml_to_json(yaml_path: str, json_path: str) -> dict:
    """从旧版 YAML 配置迁移到 JSON 格式。返回迁移后的 raw dict。"""
    if not Path(yaml_path).exists():
        return {}
    try:
        with open(yaml_path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("迁移旧配置失败: %s", e)
        return {}

    # 应用旧版迁移逻辑（去除废弃字段等）
    raw.pop("server", None)
    codex = raw.get("codex") or {}
    model_migrations = {
        "gpt-5.5-codex": "gpt-5.5",
        "gpt-5.3-codex": "gpt-5.4",
    }
    if codex.get("model") in model_migrations:
        codex["model"] = model_migrations[codex["model"]]
        raw["codex"] = codex

    # 旧格式迁移（feishu 在顶层 → channels）
    if "channels" not in raw and "feishu" in raw:
        raw["channels"] = {
            "feishu": raw.pop("feishu"),
            "dingtalk": {"enabled": False},
        }

    return raw


def _load_json_config(path: str) -> dict:
    """读取 config.json，无则尝试从旧 YAML 迁移。"""
    if Path(path).exists() and Path(path).stat().st_size > 0:
        try:
            with open(path) as f:
                return json.load(f) or {}
        except json.JSONDecodeError:
            pass  # 损坏则当不存在处理

    # 尝试从旧 YAML 迁移
    yaml_path = Path(path).with_suffix(".yaml")
    if yaml_path.exists():
        raw = _migrate_yaml_to_json(str(yaml_path), path)
        if raw:
            with open(path, "w") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
            import logging
            logging.getLogger(__name__).info("已从 %s 迁移配置到 %s", yaml_path, path)
            return raw

    return {}


def load_config(path: str, data_dir: str = "") -> Config:
    """Load and validate configuration from JSON file（自动从 YAML 迁移）。"""
    raw = _load_json_config(path)

    # Deserialize groups: convert raw dicts to GroupConfigEntry objects
    # Filter out unknown fields to tolerate future config additions gracefully.
    _known_group_keys = {"enabled", "require_mention", "allow_from"}
    raw_groups = raw.get("channels", {}).get("feishu", {}).get("groups", {})
    groups = {
        gid: GroupConfigEntry(**{k: v for k, v in gentry.items() if k in _known_group_keys})
        for gid, gentry in raw_groups.items()
    }

    feishu_raw = raw.get("channels", {}).get("feishu", {}).copy()
    feishu_raw["groups"] = groups
    # 读取 allowed_users（从 channels.feishu，兼容旧版 auth.allowed_users）
    auth_allowed_users = raw.get("auth", {}).get("allowed_users", [])
    feishu_allowed_users = feishu_raw.get("allowed_users", [])
    if auth_allowed_users and not feishu_allowed_users:
        feishu_allowed_users = auth_allowed_users
    feishu_raw["allowed_users"] = feishu_allowed_users
    feishu_cfg = FeishuChannelConfig(**feishu_raw)

    dingtalk_raw = raw.get("channels", {}).get("dingtalk", {}).copy()
    dingtalk_allowed_users = dingtalk_raw.get("allowed_users", [])
    dingtalk_raw["allowed_users"] = dingtalk_allowed_users
    dingtalk_cfg = DingTalkChannelConfig(**dingtalk_raw)

    wecom_raw = raw.get("channels", {}).get("wecom", {}).copy()
    wecom_allowed_users = wecom_raw.get("allowed_users", [])
    wecom_raw["allowed_users"] = wecom_allowed_users
    wecom_cfg = WeComChannelConfig(**wecom_raw)

    channels_cfg = ChannelsConfig(
        feishu=feishu_cfg,
        dingtalk=dingtalk_cfg,
        wecom=wecom_cfg,
        telegram=TelegramChannelConfig(**raw.get("channels", {}).get("telegram", {})),
        qq=QQChannelConfig(**raw.get("channels", {}).get("qq", {})),
        whatsapp=WhatsAppChannelConfig(**raw.get("channels", {}).get("whatsapp", {})),
        wechat=WeChatChannelConfig(**raw.get("channels", {}).get("wechat", {})),
    )

    # Deserialize codex.capture if present
    codex_raw = raw.get("codex") or {}
    _known_codex_keys = {"enabled", "cli_path", "model", "sandbox", "approval", "auto_configure_mcp", "capture"}
    _known_capture_keys = {"capture_mode"}
    capture_raw = codex_raw.get("capture") or {}
    codex_cfg = CodexMcpConfig(
        **{k: v for k, v in codex_raw.items() if k in _known_codex_keys and k != "capture"},
        capture=CodexCaptureConfig(
            **{k: v for k, v in (capture_raw or {}).items() if k in _known_capture_keys}
        ) if capture_raw else CodexCaptureConfig(),
    )

    # Deserialize verbose: platform -> chat_id -> VerboseChannelEntry
    _known_verbose_keys = {"evolve", "step"}
    verbose_raw = raw.get("verbose") or {}
    verbose: dict[str, dict[str, VerboseChannelEntry]] = {}
    for platform, chat_entries in verbose_raw.items():
        verbose[platform] = {}
        for chat_id, entry in (chat_entries or {}).items():
            verbose[platform][chat_id] = VerboseChannelEntry(
                **{k: v for k, v in entry.items() if k in _known_verbose_keys}
            )

    return Config(
        channels=channels_cfg,
        auth=AuthConfig(),
        claude=ClaudeConfig(**{k: v for k, v in raw.get("claude", {}).items() if k in {"cli_path", "max_turns", "approved_directory"}}),
        codex=codex_cfg,
        skill_nudge=SkillNudgeConfig(**raw.get("skill_nudge", {})),
        verbose=verbose,
        data_dir=data_dir,
        bypass_accepted=raw.get("bypass_accepted", False),
        daemon=raw.get("daemon", False),
        core=CoreConfig(
            host=raw.get("core", {}).get("host", "127.0.0.1"),
            port=raw.get("core", {}).get("port", 28888),
            token=raw.get("core", {}).get("token", ""),
            username=raw.get("core", {}).get("username", ""),
            password=raw.get("core", {}).get("password", ""),
        ),
    )

    # 校验 approved_directory 不能为空
    if not cfg.claude.approved_directory:
        raise ValueError("claude.approved_directory cannot be empty")


def save_config(path: str, feishu_app_id: str, feishu_app_secret: str,
                domain: str, bot_name: str,
                bot_open_id: str,
                allowed_users: list[str],
                claude_cli_path: str, claude_max_turns: int,
                claude_approved_directory: str,
                storage_db_path: str = "",
                bypass_accepted: bool = False,
                groups: dict | None = None) -> None:
    """Save a complete config to a JSON file（legacy param-based signature）。"""
    # 如果文件已存在，保留 codex、skill_nudge 和 verbose 配置
    existing_codex = None
    existing_skill_nudge = None
    existing_verbose = None
    if Path(path).exists():
        try:
            existing_cfg = load_config(path)
            existing_codex = existing_cfg.codex
            existing_skill_nudge = existing_cfg.skill_nudge
            existing_verbose = existing_cfg.verbose
        except Exception:
            pass

    cfg = Config(
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=True,
                app_id=feishu_app_id,
                app_secret=feishu_app_secret,
                bot_name=bot_name,
                bot_open_id=bot_open_id,
                domain=domain,
                groups=groups or {},
            ),
            dingtalk=DingTalkChannelConfig(enabled=False),
        ),
        auth=AuthConfig(allowed_users=allowed_users),
        claude=ClaudeConfig(
            cli_path=claude_cli_path,
            max_turns=claude_max_turns,
            approved_directory=claude_approved_directory,
        ),
        codex=existing_codex if existing_codex is not None else CodexMcpConfig(),
        skill_nudge=existing_skill_nudge if existing_skill_nudge is not None else SkillNudgeConfig(),
        verbose=existing_verbose if existing_verbose is not None else {},
        bypass_accepted=bypass_accepted,
    )
    _write_config_to_path(path, cfg)


def _write_config_to_path(path: str, cfg: Config) -> None:
    """内部函数：将 cfg 写入 JSON 文件。保留 path 参数给跨场景使用。"""
    _known_group_keys = {"enabled", "require_mention", "allow_from"}
    feishu_groups_raw = {}
    for gid, entry in cfg.channels.feishu.groups.items():
        feishu_groups_raw[gid] = {
            "enabled": entry.enabled,
            "require_mention": entry.require_mention,
            "allow_from": entry.allow_from,
        }

    raw = {
        "channels": {
            "feishu": {
                "enabled": cfg.channels.feishu.enabled,
                "app_id": cfg.channels.feishu.app_id,
                "app_secret": cfg.channels.feishu.app_secret,
                "bot_name": cfg.channels.feishu.bot_name,
                "bot_open_id": cfg.channels.feishu.bot_open_id,
                "domain": cfg.channels.feishu.domain,
                "groups": feishu_groups_raw,
                "allowed_users": cfg.channels.feishu.allowed_users,
            },
            "dingtalk": {
                "enabled": cfg.channels.dingtalk.enabled,
                "app_key": cfg.channels.dingtalk.app_key,
                "app_secret": cfg.channels.dingtalk.app_secret,
                "allowed_users": cfg.channels.dingtalk.allowed_users,
            },
            "wecom": {
                "enabled": cfg.channels.wecom.enabled,
                "corp_id": cfg.channels.wecom.corp_id,
                "agent_id": cfg.channels.wecom.agent_id,
                "corp_secret": cfg.channels.wecom.corp_secret,
                "bot_id": cfg.channels.wecom.bot_id,
                "secret": cfg.channels.wecom.secret,
                "bot_name": cfg.channels.wecom.bot_name,
                "groups": cfg.channels.wecom.groups,
                "allowed_users": cfg.channels.wecom.allowed_users,
            },
            "telegram": {
                "enabled": cfg.channels.telegram.enabled,
                "bot_token": cfg.channels.telegram.bot_token,
                "bot_name": cfg.channels.telegram.bot_name,
                "groups": cfg.channels.telegram.groups,
                "allowed_users": cfg.channels.telegram.allowed_users,
            },
            "qq": {
                "enabled": cfg.channels.qq.enabled,
                "app_id": cfg.channels.qq.app_id,
                "app_secret": cfg.channels.qq.app_secret,
                "bot_openid": cfg.channels.qq.bot_openid,
                "groups": cfg.channels.qq.groups,
                "allowed_users": cfg.channels.qq.allowed_users,
            },
            "whatsapp": {
                "enabled": cfg.channels.whatsapp.enabled,
                "bridge_port": cfg.channels.whatsapp.bridge_port,
                "session_dir": cfg.channels.whatsapp.session_dir,
                "allowed_users": cfg.channels.whatsapp.allowed_users,
            },
            "wechat": {
                "enabled": cfg.channels.wechat.enabled,
                "token": cfg.channels.wechat.token,
                "account_id": cfg.channels.wechat.account_id,
                "allowed_users": cfg.channels.wechat.allowed_users,
            },
        },
        "claude": {
            "cli_path": cfg.claude.cli_path,
            "max_turns": cfg.claude.max_turns,
            "approved_directory": cfg.claude.approved_directory,
        },
        "codex": {
            "enabled": cfg.codex.enabled,
            "cli_path": cfg.codex.cli_path,
            "model": cfg.codex.model,
            "sandbox": cfg.codex.sandbox,
            "approval": cfg.codex.approval,
            "auto_configure_mcp": cfg.codex.auto_configure_mcp,
            "capture": {
                "capture_mode": cfg.codex.capture.capture_mode,
            },
        },
        "skill_nudge": {
            "enabled": cfg.skill_nudge.enabled,
            "interval": cfg.skill_nudge.interval,
            "current_user": cfg.skill_nudge.current_user,
        },
        "verbose": {
            platform: {
                chat_id: {"evolve": e.evolve, "step": e.step}
                for chat_id, e in chat_entries.items()
            }
            for platform, chat_entries in cfg.verbose.items()
        },
        "bypass_accepted": cfg.bypass_accepted,
        "daemon": cfg.daemon,
        "core": {
            "host": cfg.core.host,
            "port": cfg.core.port,
            "token": cfg.core.token,
            "username": cfg.core.username,
            "password": cfg.core.password,
        },
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)


def register_group_config(config_path: str, group_id: str, entry: GroupConfigEntry | None = None) -> bool:
    """Auto-register a group in the config file. Creates default entry if none provided.

    Returns True if the group was newly registered, False if it already existed.
    """
    if entry is None:
        entry = GroupConfigEntry()

    cfg = load_config(config_path)

    if group_id in cfg.channels.feishu.groups:
        return False  # already registered

    cfg.channels.feishu.groups[group_id] = entry
    _write_config_to_path(config_path, cfg)
    return True  # newly registered


def accept_bypass_warning(config_path: str) -> None:
    """Record that the bypass permissions risk warning has been accepted."""
    cfg = load_config(config_path)
    cfg.bypass_accepted = True
    _write_config_to_path(config_path, cfg)


README_CONTENT = """# .supercc

This directory is created automatically by `supercc` and contains the config for this project instance.

## Contents

- `config.json` — Bot credentials and configuration（2026-05-02 起从 YAML 迁移）
- `model.json` — 模型配置（per-project 隔离）
- `skills/` — Private skills for this project
- `cron_jobs.json` — Cron job definitions

Note: sessions.db and memories.db live in ~/.supercc/ (home dir, shared across projects).
Other data (cron, logs, skills, media, pid) lives in {project}/.supercc/.

The `storage` section is no longer needed — sessions.db path is fixed to ~/.supercc/sessions.db.

## Git Ignore

This directory is gitignored. It should never be committed.

"""


def resolve_config_path() -> tuple[str, str]:
    """Resolve config and data directories.

    Config lives in project dir: {cwd}/.supercc/config.json（2026-05-02 起从 YAML 迁移）
    Data (sessions, logs, PID) also lives in {cwd}/.supercc/.

    Auto-creates both directories if not found.

    Raises OSError if cwd is at a filesystem root (/) since that indicates
    the launchd/systemd script did not cd to the project directory, and
    falling back to ~/.supercc/ would mix data from multiple projects.
    """
    import os
    cwd = os.getcwd()
    cfg_dir = Path(cwd).resolve() / ".supercc"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    cfg_path.touch(exist_ok=True)
    readme_path = cfg_dir / "README.md"
    readme_path.write_text(README_CONTENT, errors="replace")

    data_dir = str(cfg_dir)
    return (str(cfg_path), data_dir)
