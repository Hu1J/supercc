"""预置模型供应商配置 — 主流 API 供应商的 base_url 和可用模型列表。

所有 base_url 均为 Claude Code (Anthropic Messages API) 兼容格式。
模型 ID 来源：openclaw/openclaw 源码 extensions/ 目录下的 provider-catalog.ts / models.ts
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Provider:
    """单个模型供应商"""
    id: str               # 唯一标识，如 "minimax"
    name: str             # 显示名称，如 "MiniMax 海螺AI"
    base_url: str         # Anthropic Messages API 兼容端点
    auth_type: str        # "bearer" | "api_key" | "azure"
    models: list[str]     # 可用模型 ID 列表
    description: str      # 简短描述


# ── 预置供应商 ────────────────────────────────────────────────────────────────
# 只保留支持 Anthropic Messages API 格式（/v1/messages + Bearer）的供应商
# 剔除：OpenAI（/v1/chat/completions）、Gemini（Google 格式）、Azure（Azure 格式）
#       Groq/Together/Mistral/Cerebras/SiliconFlow（均为 OpenAI 兼容格式）
#
# 排序：国内常用（MiniMax/火山/千问/GLM/DeepSeek/Kimi）→ Anthropic → OpenRouter → Novita/Ollama

PROVIDERS: dict[str, Provider] = {

    # ── 国内常用 ──────────────────────────────────────────────────────────────

    "minimax": Provider(
        id="minimax",
        name="MiniMax 海螺AI",
        base_url="https://api.minimaxi.com/anthropic",
        auth_type="bearer",
        models=[
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
        ],
        description="MiniMax 海螺AI（M2.7 系列，高性价比）",
    ),

    "volcano": Provider(
        id="volcano",
        name="火山引擎 ARK",
        base_url="https://ark.cn-beijing.volces.com/api/coding",
        auth_type="bearer",
        models=[
            "doubao-seed-2.0-code",
            "doubao-seed-2.0-pro",
            "doubao-seed-2.0-lite",
            "doubao-seed-code",
            "minimax-m2.7",
            "minimax-m2.5",
            "glm-5.1",
            "glm-4.7",
            "deepseek-v3.2",
            "kimi-k2.6",
            "kimi-k2.5",
        ],
        description="火山引擎 ARK（豆包/MiniMax/GLM/DeepSeek/Kimi 系列）",
    ),

    "qwen": Provider(
        id="qwen",
        name="阿里云通义千问",
        base_url="https://dashscope.aliyuncs.com/apps/anthropic",
        auth_type="bearer",
        models=[
            "qwen3.5-plus",
            "qwen3.6-plus",
            "qwen3-max-2026-01-23",
            "qwen3-coder-next",
            "qwen3-coder-plus",
            "MiniMax-M2.5",
            "glm-5",
            "glm-4.7",
            "kimi-k2.5",
        ],
        description="阿里云通义千问（Qwen3 / GLM-5 / Kimi 系列）",
    ),

    "zhipu": Provider(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/anthropic",
        auth_type="bearer",
        models=[
            "glm-5",
            "glm-4.7",
        ],
        description="智谱 AI（GLM-5 / GLM-4.7 系列）",
    ),

    "deepseek": Provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/anthropic",
        auth_type="bearer",
        models=[
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        description="DeepSeek（DeepSeek V3 / Reasoner）",
    ),

    "kimi": Provider(
        id="kimi",
        name="Kimi 月之暗面",
        base_url="https://api.moonshot.cn/anthropic",
        auth_type="bearer",
        models=[
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "kimi-k2-turbo",
        ],
        description="Kimi 月之暗面（K2 系列）",
    ),

    # ── 海外 ──────────────────────────────────────────────────────────────────

    "anthropic": Provider(
        id="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com",
        auth_type="bearer",
        models=[
            "claude-opus-4-5",
            "claude-sonnet-4-5",
            "claude-sonnet-4-4",
            "claude-sonnet-4-3",
            "claude-3-5-sonnet-4-20250514",
            "claude-3-opus-3-20240229",
            "claude-3-sonnet-4-20240229",
            "claude-3-haiku-3-20240307",
        ],
        description="Anthropic 官方 API（Claude 系列）",
    ),

    "openrouter": Provider(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        auth_type="bearer",
        models=[
            "anthropic/claude-3.5-sonnet",
            "anthropic/claude-3-opus",
            "deepseek/deepseek-chat",
            "qwen/qwen-2-72b-instruct",
        ],
        description="OpenRouter（聚合 100+ 模型，支持 Anthropic 格式）",
    ),

    "ollama": Provider(
        id="ollama",
        name="Ollama（本地）",
        base_url="http://localhost:11434/v1",
        auth_type="bearer",
        models=[
            # 静态列表仅供参考，实际模型以 `ollama list` 查询为准
            "llama3.3",
            "llama3.2",
            "qwen2.5",
            "mistral",
            "codellama",
            "mixtral",
            "deepseek-coder-v2",
        ],
        description="Ollama 本地模型（需本地安装并启动 ollama 服务，用 `ollama list` 查看实际模型）",
    ),
}


def get_provider(provider_id: str) -> Optional[Provider]:
    return PROVIDERS.get(provider_id)


def list_providers() -> dict[str, Provider]:
    return PROVIDERS.copy()


def format_provider_help() -> str:
    """格式化供应商帮助信息（用于飞书消息）"""
    auth_display = {"bearer": "Bearer API Key"}
    lines = ["**支持的模型供应商：**\n"]
    for p in PROVIDERS.values():
        auth = auth_display.get(p.auth_type, p.auth_type)
        models_str = ", ".join(f"`{m}`" for m in p.models[:6])
        if len(p.models) > 6:
            models_str += f" ... (+{len(p.models) - 6})"
        lines.append(f"`{p.id}` — **{p.name}**  ({auth})")
        lines.append(f"  {p.description}")
        lines.append(f"  模型: {models_str}")
        lines.append("")
    lines.append("**用法：**")
    lines.append("`/model add --provider <provider_id> <api_key> <model>`")
    lines.append("示例: `/model add --provider minimax <api_key> MiniMax-M2.7`")
    return "\n".join(lines)
