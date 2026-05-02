"""模型配置管理 — 管理项目级 .supercc/model.json 和全局 ~/.claude/settings.json"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from supercc.claude.model_providers import PROVIDERS

logger = logging.getLogger(__name__)

# 全局 Claude 设置路径（不变）
CLAUDE_SETTINGS_PATH = str(Path.home() / ".claude" / "settings.json")
OLD_MODELS_PATH = str(Path.home() / ".supercc" / "models.yaml")


def _get_data_dir() -> str:
    """获取当前项目的数据目录（.supercc/）。"""
    from supercc.config import resolve_config_path
    _, data_dir = resolve_config_path()
    return data_dir


def _get_model_config_path() -> str:
    """获取模型配置 JSON 文件路径：{data_dir}/model.json"""
    return os.path.join(_get_data_dir(), "model.json")


# ── 旧版 YAML 迁移 ────────────────────────────────────────────────────────────

def _migrate_from_yaml() -> dict:
    """从旧版 ~/.supercc/models.yaml 迁移配置到 JSON 格式。返回迁移后的 raw dict。"""
    import yaml

    if not os.path.exists(OLD_MODELS_PATH):
        return {}

    try:
        with open(OLD_MODELS_PATH) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("迁移旧模型配置失败: %s", e)
        return {}

    # 转换为 JSON 格式（结构不变，只是换个文件存）
    return raw


# ── JSON 文件读写 ─────────────────────────────────────────────────────────────

def _ensure_models_dir() -> None:
    """确保 .supercc 目录存在"""
    Path(_get_model_config_path()).parent.mkdir(parents=True, exist_ok=True)


def _load_json() -> dict:
    """读取 model.json，返回字典。无文件则迁移旧配置或创建默认配置。"""
    path = _get_model_config_path()

    # 首次：从旧版 YAML 迁移
    if not os.path.exists(path) and os.path.exists(OLD_MODELS_PATH):
        raw = _migrate_from_yaml()
        if raw:
            _save_json(raw)
            logger.info("已从 %s 迁移模型配置到 %s", OLD_MODELS_PATH, path)
            return raw

    if not os.path.exists(path):
        _create_default_config()
    with open(path) as f:
        return json.load(f) or {}


def _save_json(raw: dict) -> None:
    """直接将字典写入 model.json。"""
    _ensure_models_dir()
    with open(_get_model_config_path(), "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)


# ── 数据模型 ─────────────────────────────────────────────────────────────────

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
    provider_name: str = "custom"
    description: str = ""
    env: ModelEnv = field(default_factory=ModelEnv)
    is_default: bool = False


# ── 序列化/反序列化 ────────────────────────────────────────────────────────────

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
        base_url = env_data.get("ANTHROPIC_BASE_URL", "")
        provider_name = model_data.get("provider_name")
        if provider_name is None:
            provider_name = "custom"
            for p in PROVIDERS.values():
                if p.base_url and p.base_url == base_url:
                    provider_name = p.name
                    break

        models[model_id] = ModelEntry(
            name=model_data.get("name", model_id),
            provider_name=provider_name,
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
            "provider_name": entry.provider_name,
            "description": entry.description,
            "is_default": entry.is_default,
            "env": {
                "ANTHROPIC_AUTH_TOKEN": entry.env.ANTHROPIC_AUTH_TOKEN,
                "ANTHROPIC_BASE_URL": entry.env.ANTHROPIC_BASE_URL,
                "ANTHROPIC_MODEL": entry.env.ANTHROPIC_MODEL,
            },
        }
    return raw_models


# ── 默认配置 ─────────────────────────────────────────────────────────────────

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
    _save_json(raw)


# ── 公开 API ─────────────────────────────────────────────────────────────────

def get_all_models() -> dict[str, ModelEntry]:
    """获取所有模型配置（每次直接读文件）"""
    _, models = _parse_models(_load_json())
    return models


def get_active_model() -> Optional[ModelEntry]:
    """获取当前激活的模型配置（每次直接读文件）"""
    active_id, models = _parse_models(_load_json())
    return models.get(active_id)


def switch_model(model_id: str) -> bool:
    """切换到指定模型，返回是否成功"""
    raw = _load_json()
    active_id, models = _parse_models(raw)

    if model_id not in models:
        return False

    raw["active_model"] = model_id
    _save_json(raw)

    # 模型配置现在通过 ClaudeAgentOptions.env 直接传给 SDK，不再写全局文件
    return True


def add_model(model_id: str, name: str, description: str, env: ModelEnv, provider_name: str = "custom") -> bool:
    """添加新模型，返回是否成功（ID 冲突返回 False）"""
    raw = _load_json()
    _, models = _parse_models(raw)

    if model_id in models:
        return False

    models[model_id] = ModelEntry(
        name=name,
        provider_name=provider_name,
        description=description,
        env=env,
        is_default=False,
    )

    raw["models"] = _serialize_models(models)
    _save_json(raw)
    return True


def update_model_env(model_id: str, env: ModelEnv, provider_name: str | None = None) -> bool:
    """更新已有模型的完整 env（token + model + base_url）。可选更新 provider_name。"""
    raw = _load_json()
    _, models = _parse_models(raw)

    if model_id not in models:
        return False

    models[model_id].env = env
    if provider_name is not None:
        models[model_id].provider_name = provider_name
    raw["models"] = _serialize_models(models)
    _save_json(raw)
    return True


def update_model_token(model_id: str, new_token: str) -> bool:
    """更新已有模型的 API Key。"""
    raw = _load_json()
    _, models = _parse_models(raw)

    if model_id not in models:
        return False

    models[model_id].env.ANTHROPIC_AUTH_TOKEN = new_token
    raw["models"] = _serialize_models(models)
    _save_json(raw)
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
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    body_json = json.loads(body)
                    if body_json.get("error"):
                        err = body_json["error"]
                        msg = err.get("message", err.get("type", body)) if isinstance(err, dict) else str(err)
                        return False, f"API 返回错误：{msg}"
                except Exception:
                    pass
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
    raw = _load_json()
    active_id, models = _parse_models(raw)

    if model_id not in models:
        return False

    if model_id == active_id:
        return False

    del models[model_id]
    raw["models"] = _serialize_models(models)
    _save_json(raw)
    return True


# ── Claude全局设置（不变）─────────────────────────────────────────────────────

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
