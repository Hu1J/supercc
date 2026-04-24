"""模型配置管理 — 管理 ~/.supercc/models.yaml 和 ~/.claude/settings.json"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import yaml

# 路径常量
MODELS_CONFIG_PATH = str(Path.home() / ".supercc" / "models.yaml")
CLAUDE_SETTINGS_PATH = str(Path.home() / ".claude" / "settings.json")


@dataclass
class ModelEnv:
    """单个模型的 API 配置"""
    ANTHROPIC_AUTH_TOKEN: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com"
    ANTHROPIC_MODEL: str = "claude-opus-4-5"


@dataclass
class ModelEntry:
    """单个模型配置条目"""
    name: str
    description: str = ""
    env: ModelEnv = field(default_factory=ModelEnv)
    is_default: bool = False


def _ensure_models_dir() -> None:
    """确保 ~/.supercc 目录存在"""
    Path(MODELS_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)


def _load_yaml() -> dict:
    """直接读取 models.yaml，返回字典。无文件则创建默认配置。"""
    if not os.path.exists(MODELS_CONFIG_PATH):
        _create_default_config()
    with open(MODELS_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(raw: dict) -> None:
    """直接将字典写入 models.yaml。"""
    _ensure_models_dir()
    with open(MODELS_CONFIG_PATH, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)


def _parse_models(raw: dict) -> tuple[str, dict[str, ModelEntry]]:
    """解析 raw dict，返回 (active_model_id, models_dict)。"""
    active_id = raw.get("active_model", "default")
    models: dict[str, ModelEntry] = {}
    for model_id, model_data in raw.get("models", {}).items():
        env_data = model_data.get("env", {})
        env = ModelEnv(
            ANTHROPIC_AUTH_TOKEN=env_data.get("ANTHROPIC_AUTH_TOKEN", ""),
            ANTHROPIC_BASE_URL=env_data.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            ANTHROPIC_MODEL=env_data.get("ANTHROPIC_MODEL", ""),
        )
        models[model_id] = ModelEntry(
            name=model_data.get("name", model_id),
            description=model_data.get("description", ""),
            env=env,
            is_default=model_data.get("is_default", False),
        )
    return active_id, models


def _serialize_models(models: dict[str, ModelEntry]) -> dict:
    """将 models dict 序列化为 raw dict（不含 active_model）。"""
    raw_models = {}
    for model_id, entry in models.items():
        raw_models[model_id] = {
            "name": entry.name,
            "description": entry.description,
            "is_default": entry.is_default,
            "env": {
                "ANTHROPIC_AUTH_TOKEN": entry.env.ANTHROPIC_AUTH_TOKEN,
                "ANTHROPIC_BASE_URL": entry.env.ANTHROPIC_BASE_URL,
                "ANTHROPIC_MODEL": entry.env.ANTHROPIC_MODEL,
            },
        }
    return raw_models


def _create_default_config() -> None:
    """创建默认模型配置（自动从 ~/.claude/settings.json 导入已有配置）"""
    _ensure_models_dir()

    default_env = ModelEnv()
    default_entry = ModelEntry(
        name="Claude Opus 4",
        description="默认模型配置",
        env=default_env,
        is_default=True,
    )

    if os.path.exists(CLAUDE_SETTINGS_PATH):
        try:
            with open(CLAUDE_SETTINGS_PATH) as f:
                settings = json.load(f)
            env_cfg = settings.get("env", {})
            if env_cfg.get("ANTHROPIC_AUTH_TOKEN"):
                default_entry.env.ANTHROPIC_AUTH_TOKEN = env_cfg.get("ANTHROPIC_AUTH_TOKEN", "")
            if env_cfg.get("ANTHROPIC_BASE_URL"):
                default_entry.env.ANTHROPIC_BASE_URL = env_cfg.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            if env_cfg.get("ANTHROPIC_MODEL"):
                default_entry.env.ANTHROPIC_MODEL = env_cfg.get("ANTHROPIC_MODEL", "claude-opus-4-5")
            if env_cfg.get("ANTHROPIC_AUTH_TOKEN"):
                default_entry.name = f"导入配置 ({default_entry.env.ANTHROPIC_MODEL})"
        except Exception:
            pass

    raw = {
        "active_model": "default",
        "models": _serialize_models({"default": default_entry}),
    }
    _save_yaml(raw)


def get_all_models() -> dict[str, ModelEntry]:
    """获取所有模型配置（每次直接读文件）"""
    _, models = _parse_models(_load_yaml())
    return models


def get_active_model() -> Optional[ModelEntry]:
    """获取当前激活的模型配置（每次直接读文件）"""
    active_id, models = _parse_models(_load_yaml())
    return models.get(active_id)


def switch_model(model_id: str) -> bool:
    """切换到指定模型，返回是否成功"""
    raw = _load_yaml()
    active_id, models = _parse_models(raw)

    if model_id not in models:
        return False

    raw["active_model"] = model_id
    _save_yaml(raw)

    _update_claude_settings(models[model_id].env)
    _ensure_claude_onboarding()
    return True


def add_model(model_id: str, name: str, description: str, env: ModelEnv) -> bool:
    """添加新模型，返回是否成功（ID 冲突返回 False）"""
    raw = _load_yaml()
    _, models = _parse_models(raw)

    if model_id in models:
        return False

    models[model_id] = ModelEntry(
        name=name,
        description=description,
        env=env,
        is_default=False,
    )

    raw["models"] = _serialize_models(models)
    _save_yaml(raw)
    return True


def update_model_token(model_id: str, new_token: str) -> bool:
    """更新已有模型的 API Key。"""
    raw = _load_yaml()
    _, models = _parse_models(raw)

    if model_id not in models:
        return False

    models[model_id].env.ANTHROPIC_AUTH_TOKEN = new_token
    raw["models"] = _serialize_models(models)
    _save_yaml(raw)
    return True


def validate_model_env(env: ModelEnv) -> tuple[bool, str]:
    """验证 API credentials 是否有效（发送一次 test 请求）。

    Returns:
        (is_valid, error_message)
    """
    import urllib.request

    if not env.ANTHROPIC_AUTH_TOKEN:
        return False, "API Key 为空"

    payload = json.dumps({
        "model": env.ANTHROPIC_MODEL or "claude-sonnet-4-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{env.ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages",
        data=payload,
        headers={
            "Authorization": f"Bearer {env.ANTHROPIC_AUTH_TOKEN}",
            "Content-Type": "application/json",
            "x-api-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("error", {}).get("message", body)
        except Exception:
            msg = body[:200]
        return False, f"HTTP {e.code}: {msg}"
    except Exception as e:
        return False, str(e)
    return False, "未知错误"


def delete_model(model_id: str) -> bool:
    """删除模型，返回是否成功（不能删除当前激活的模型）"""
    raw = _load_yaml()
    active_id, models = _parse_models(raw)

    if model_id not in models:
        return False

    if model_id == active_id:
        return False

    del models[model_id]
    raw["models"] = _serialize_models(models)
    _save_yaml(raw)
    return True


def _ensure_claude_onboarding() -> None:
    """强制写入 ~/.claude.json，确保 hasCompletedOnboarding: true"""
    CLAUDE_JSON_PATH = str(Path.home() / ".claude.json")
    logger.info("Writing hasCompletedOnboarding=true to %s", CLAUDE_JSON_PATH)
    data = {}
    if os.path.exists(CLAUDE_JSON_PATH):
        try:
            with open(CLAUDE_JSON_PATH) as f:
                data = json.load(f)
        except Exception:
            logger.warning("Failed to read existing %s, overwriting.", CLAUDE_JSON_PATH)
    data["hasCompletedOnboarding"] = True
    try:
        Path(CLAUDE_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(CLAUDE_JSON_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("Successfully wrote %s", CLAUDE_JSON_PATH)
    except Exception as e:
        logger.error("Failed to write %s: %s", CLAUDE_JSON_PATH, e)


def _update_claude_settings(env: ModelEnv) -> None:
    """更新 ~/.claude/settings.json 的 env 字段（保留其他字段）"""
    logger.info(
        "Syncing Claude settings — model=%s base_url=%s",
        env.ANTHROPIC_MODEL,
        env.ANTHROPIC_BASE_URL,
    )
    settings = {"env": {}}

    if os.path.exists(CLAUDE_SETTINGS_PATH):
        try:
            with open(CLAUDE_SETTINGS_PATH) as f:
                settings = json.load(f)
        except Exception:
            logger.warning("Failed to read existing %s, overwriting.", CLAUDE_SETTINGS_PATH)

    if "env" not in settings:
        settings["env"] = {}

    settings["env"]["ANTHROPIC_AUTH_TOKEN"] = env.ANTHROPIC_AUTH_TOKEN
    settings["env"]["ANTHROPIC_BASE_URL"] = env.ANTHROPIC_BASE_URL
    settings["env"]["ANTHROPIC_MODEL"] = env.ANTHROPIC_MODEL

    try:
        Path(CLAUDE_SETTINGS_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(CLAUDE_SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        logger.info("Successfully wrote %s", CLAUDE_SETTINGS_PATH)
    except Exception as e:
        logger.error("Failed to write %s: %s", CLAUDE_SETTINGS_PATH, e)


def get_current_claude_settings() -> dict:
    """读取当前的 ~/.claude/settings.json"""
    if not os.path.exists(CLAUDE_SETTINGS_PATH):
        return {}
    try:
        with open(CLAUDE_SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def is_configured() -> bool:
    """检查是否已完成初始模型配置（至少有一个有效 API Key 的模型）"""
    for entry in get_all_models().values():
        if entry.env.ANTHROPIC_AUTH_TOKEN:
            return True
    return False
