"""预置模型供应商配置 — 主流 API 供应商的 base_url 和可用模型列表。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Provider:
    """单个模型供应商"""
    id: str               # 唯一标识，如 "openrouter"
    name: str             # 显示名称，如 "OpenRouter"
    base_url: str         # API base URL
    auth_type: str        # "bearer" | "api_key" | "azure"（影响 token 前缀处理）
    models: list[str]     # 可用模型列表（常用/推荐）
    description: str     # 简短描述


# ── 预置供应商 ────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, Provider] = {
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
        description="Anthropic 官方 API（支持 Claude 系列模型）",
    ),

    "openrouter": Provider(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        auth_type="bearer",
        models=[
            # Anthropic
            "anthropic/claude-3.5-sonnet",
            "anthropic/claude-3-opus",
            "anthropic/claude-3-haiku",
            "anthropic/claude-3.5-haiku",
            # OpenAI
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-4-turbo",
            # Google
            "google/gemini-pro-1.5",
            "google/gemini-2.0-flash",
            # Mistral
            "mistral/mistral-large",
            "mistral/mistral-7b-instruct",
            # Meta
            "meta-llama/llama-3-70b-instruct",
            "meta-llama/llama-3-8b-instruct",
            # DeepSeek
            "deepseek/deepseek-chat",
            # Qwen
            "qwen/qwen-2-72b-instruct",
            # 其他
            "openai/chatgpt-4o-latest",
            "x-ai/grok-2",
            "perplexity/llama-3.1-sonar-large-128k-online",
        ],
        description="聚合多模型，支持 100+ 模型（Anthropic/OpenAI/Google/Meta 等）",
    ),

    "openai": Provider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        auth_type="bearer",
        models=[
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-3.5-turbo",
        ],
        description="OpenAI 官方 API（GPT-4o / GPT-4 / GPT-3.5）",
    ),

    "azure": Provider(
        id="azure",
        name="Azure OpenAI",
        base_url="",   # Azure 需要用户填入自己的 endpoint
        auth_type="azure",
        models=[
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-35-turbo",
        ],
        description="Azure OpenAI Service（企业版，需提供 Azure endpoint URL）",
    ),

    "groq": Provider(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        auth_type="bearer",
        models=[
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma-7b-it",
        ],
        description="Groq（超低延迟推理，LLaMA/Mixtral/Gemma）",
    ),

    "gemini": Provider(
        id="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com",
        auth_type="api_key",
        models=[
            "gemini-2.0-flash",
            "gemini-2.0-flash-exp",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-pro",
        ],
        description="Google Gemini API（gemini-2.0 / gemini-1.5 系列）",
    ),

    "deepseek": Provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        auth_type="bearer",
        models=[
            "deepseek-chat",
            "deepseek-coder",
            "deepseek-reasoner",
        ],
        description="DeepSeek（DeepSeek Chat / Coder / Reasoner）",
    ),

    "ollama": Provider(
        id="ollama",
        name="Ollama（本地）",
        base_url="http://localhost:11434/v1",
        auth_type="bearer",
        models=[
            "llama3.3",
            "llama3.2",
            "llama3.1",
            "qwen2.5",
            "mistral",
            "codellama",
            "phi3",
            "mixtral",
            "deepseek-coder-v2",
        ],
        description="Ollama 本地模型（需本地安装并启动 ollama 服务）",
    ),

    "zhipu": Provider(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        auth_type="bearer",
        models=[
            "glm-4-plus",
            "glm-4",
            "glm-4-flash",
            "glm-4-long",
            "glm-3-turbo",
        ],
        description="智谱 AI（GLM-4 / GLM-3 系列，国产大模型）",
    ),

    "siliconflow": Provider(
        id="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        auth_type="bearer",
        models=[
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V2.5",
            "anthropic/claude-3.5-sonnet",
            "meta-llama/Meta-Llama-3.1-70B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.2",
        ],
        description="SiliconFlow 硅基流动（聚合多个开源模型，国内可访问）",
    ),

    "together": Provider(
        id="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        auth_type="bearer",
        models=[
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.1-405B-Instruct",
            "meta-llama/Llama-3.1-70B-Instruct",
            "mistralai/Mixtral-8x22B-Instruct",
            "Qwen/Qwen2-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "google/gemma-2-27b-it",
        ],
        description="Together AI（低价高质开源模型，LLaMA/Qwen/Mixtral）",
    ),

    "mistral": Provider(
        id="mistral",
        name="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        auth_type="bearer",
        models=[
            "mistral-large-2411",
            "mistral-small-2501",
            "ministral-3b",
            "ministral-8b",
        ],
        description="Mistral AI 官方 API（Mistral Large / Small）",
    ),

    "cerebras": Provider(
        id="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        auth_type="bearer",
        models=[
            "llama-3.3-70b",
            "llama-3.1-405b",
            "llama-3.1-70b",
            "llama-3.1-8b",
        ],
        description="Cerebras（全球最快推理，Llama 系列）",
    ),

    "novita": Provider(
        id="novita",
        name="Novita AI",
        base_url="https://api.novita.ai/v3",
        auth_type="bearer",
        models=[
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
            "meta-llama/Llama-3.1-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
        description="Novita AI（聚合多模型，性价比高）",
    ),
}


def get_provider(provider_id: str) -> Optional[Provider]:
    return PROVIDERS.get(provider_id)


def list_providers() -> dict[str, Provider]:
    return PROVIDERS.copy()


def format_provider_help() -> str:
    """格式化供应商帮助信息（用于飞书消息）"""
    auth_display = {"bearer": "Bearer Token", "api_key": "API Key", "azure": "Azure AD Token"}
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
    lines.append("`/model add --provider <provider_id> <token> <model>`")
    lines.append("示例: `/model add --provider openrouter sk-or-xxx anthropic/claude-3.5-sonnet`")
    return "\n".join(lines)
