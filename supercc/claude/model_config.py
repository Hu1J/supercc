"""模型配置管理 — 全局 ~/.supercc/model.json + 项目路径映射"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from supercc.claude.model_providers import PROVIDERS, get_provider

logger = logging.getLogger(__name__)

# 全局模型配置路径
GLOBAL_MODEL_PATH = str(Path.home() / ".supercc" / "model.json")


# ── 数据模型 ─────────────────────────────────────────────────────────────────

@dataclass
class ModelEnv:
    """单个模型的 API 配置"""
    ANTHROPIC_AUTH_TOKEN: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com"
    ANTHROPIC_MODEL: str = "claude-opus-4-5"


@dataclass
class ProviderConfig:
    """单个供应商的配置"""
    api_key: str = ""
    models: list[str] = field(default_factory=list)


@dataclass
class ProjectModelEntry:
    """单个项目激活的模型"""
    provider_id: str = ""
    model_id: str = ""


# ── 全局单例 ─────────────────────────────────────────────────────────────────

_model_env_instance: ModelEnv | None = None
_model_json_path: str = ""


def init_model_env(project_path: str) -> ModelEnv:
    """初始化全局 ModelEnv 单例。

    加载 ~/.supercc/model.json，并用 project_path 对应的 projects 映射
    初始化当前激活的 ModelEnv。之后所有地方用 get_model_env() 获取同一对象。
    """
    global _model_env_instance, _model_json_path
    _model_json_path = project_path
    _model_env_instance = _load_and_resolve(project_path)
    return _model_env_instance


def get_model_env() -> ModelEnv:
    """获取全局 ModelEnv 单例。必须在 init_model_env() 之后调用。"""
    if _model_env_instance is None:
        raise RuntimeError("ModelEnv not initialized. Call init_model_env(path) first.")
    return _model_env_instance


def _model_json_path() -> str:
    return GLOBAL_MODEL_PATH


# ── JSON 文件读写 ─────────────────────────────────────────────────────────────

def _ensure_model_dir() -> None:
    Path(GLOBAL_MODEL_PATH).parent.mkdir(parents=True, exist_ok=True)


def _load_json() -> dict:
    """读取全局 model.json，返回字典。无文件则创建默认配置。"""
    if not os.path.exists(GLOBAL_MODEL_PATH):
        _create_default_config()
    with open(GLOBAL_MODEL_PATH) as f:
        return json.load(f) or {}


def _save_json(raw: dict) -> None:
    """将字典写入全局 model.json。"""
    _ensure_model_dir()
    with open(GLOBAL_MODEL_PATH, "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)


# ── 默认配置（同步预置供应商）──────────────────────────────────────────────────

def _sync_providers_from_presets() -> dict[str, ProviderConfig]:
    """从 model_providers.py 同步预置供应商配置到 providers dict。"""
    providers: dict[str, ProviderConfig] = {}
    for pid, provider in PROVIDERS.items():
        if pid == "custom":
            continue
        providers[pid] = ProviderConfig(
            api_key="",
            models=provider.models.copy(),
        )
    return providers


def _create_default_config() -> None:
    """创建默认配置：从预置同步所有供应商，projects 初始化为空。"""
    _ensure_model_dir()
    # 将 ProviderConfig 对象转换为 dict 以便 JSON 序列化
    presets = _sync_providers_from_presets()
    providers_raw = {}
    for pid, pcfg in presets.items():
        providers_raw[pid] = {"api_key": pcfg.api_key, "models": pcfg.models}
    raw = {
        "providers": providers_raw,
        "projects": {},
    }
    _save_json(raw)
    logger.info("已创建默认模型配置: %s", GLOBAL_MODEL_PATH)


# ── 内部解析 ─────────────────────────────────────────────────────────────────

def _resolve_active_env(project_path: str) -> ModelEnv:
    """根据当前 project_path 解析对应的 ModelEnv。"""
    raw = _load_json()
    projects: dict[str, dict] = raw.get("projects", {})
    entry = projects.get(project_path)

    if entry:
        provider_id = entry.get("provider_id", "")
        model_id = entry.get("model_id", "")
    else:
        provider_id = ""
        model_id = ""

    if not provider_id:
        return ModelEnv()

    providers: dict[str, dict] = raw.get("providers", {})
    pcfg = providers.get(provider_id)
    if not pcfg:
        return ModelEnv()

    provider = get_provider(provider_id)
    base_url = provider.base_url if provider else ""

    return ModelEnv(
        ANTHROPIC_AUTH_TOKEN=pcfg.get("api_key", ""),
        ANTHROPIC_BASE_URL=base_url,
        ANTHROPIC_MODEL=model_id,
    )


def _load_and_resolve(project_path: str) -> ModelEnv:
    """加载并解析当前项目的 ModelEnv。"""
    return _resolve_active_env(project_path)


# ── 公开 API ─────────────────────────────────────────────────────────────────

def get_active_model_for_project(project_path: str) -> tuple[str, str]:
    """获取指定项目当前激活的 (provider_id, model_id)。"""
    raw = _load_json()
    projects: dict[str, dict] = raw.get("projects", {})
    entry = projects.get(project_path, {})
    return entry.get("provider_id", ""), entry.get("model_id", "")


def get_all_providers() -> dict[str, ProviderConfig]:
    """获取所有供应商配置（每次直接读文件）。"""
    raw = _load_json()
    providers_raw: dict[str, dict] = raw.get("providers", {})
    result: dict[str, ProviderConfig] = {}
    for pid, pdata in providers_raw.items():
        result[pid] = ProviderConfig(
            api_key=pdata.get("api_key", ""),
            models=pdata.get("models", []),
        )
    return result


def get_provider_api_key(provider_id: str) -> str:
    """获取指定供应商的 API Key。"""
    providers = get_all_providers()
    return providers.get(provider_id, ProviderConfig()).api_key


def update_provider_api_key(provider_id: str, api_key: str) -> bool:
    """更新指定供应商的 API Key。"""
    raw = _load_json()
    providers: dict[str, dict] = raw.get("providers", {})
    if provider_id not in providers:
        return False
    providers[provider_id]["api_key"] = api_key
    raw["providers"] = providers
    _save_json(raw)

    # 如果当前项目的激活映射正好是这个 provider，刷新单例
    global _model_env_instance
    if _model_env_instance is not None:
        current_pid, current_mid = get_active_model_for_project(_model_json_path)
        if current_pid == provider_id:
            _model_env_instance = _resolve_active_env(_model_json_path)
    return True


def set_project_model(project_path: str, provider_id: str, model_id: str) -> bool:
    """为指定项目设置激活的 (provider_id, model_id)。"""
    raw = _load_json()
    providers: dict[str, dict] = raw.get("providers", {})
    if provider_id not in providers:
        return False

    projects: dict[str, dict] = raw.get("projects", {})
    projects[project_path] = {
        "provider_id": provider_id,
        "model_id": model_id,
    }
    raw["projects"] = projects
    _save_json(raw)

    # 刷新单例
    global _model_env_instance
    if _model_env_instance is not None:
        _model_env_instance = _resolve_active_env(project_path)
    return True


def get_providers_for_display() -> list[tuple[str, str, str, list[str], bool]]:
    """获取所有供应商的展示信息。

    Returns:
        [(provider_id, name, api_key_masked, models, is_active), ...]
    """
    raw = _load_json()
    providers_raw: dict[str, dict] = raw.get("providers", {})
    projects: dict[str, dict] = raw.get("projects", {})
    current_project = _model_json_path if _model_json_path else ""
    current_entry = projects.get(current_project, {})
    current_pid = current_entry.get("provider_id", "")

    result = []
    for pid, provider in PROVIDERS.items():
        if pid == "custom":
            continue
        pdata = providers_raw.get(pid, {})
        masked_key = _mask_api_key(pdata.get("api_key", ""))
        is_active = pid == current_pid
        result.append((pid, provider.name, masked_key, provider.models, is_active))
    return result


def _mask_api_key(key: str) -> str:
    if not key:
        return "—"
    if len(key) <= 10:
        return "****"
    return key[:6] + "***" + key[-4:]


# ── 验证 ─────────────────────────────────────────────────────────────────────

def validate_model_env(env: ModelEnv) -> tuple[bool, str]:
    """验证 API credentials 是否有效（发送一次 test 请求）。"""
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


def is_configured() -> bool:
    """检查是否已完成初始模型配置（至少有一个供应商配置了 API Key）。"""
    providers = get_all_providers()
    for pcfg in providers.values():
        if pcfg.api_key:
            return True
    return False
