"""SuperCC MCP 工具统一入口 — 所有内置 MCP 工具注册在一个 'SuperCC' server 下。"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server

# 导入已装饰的工具对象（SdkMcpTool），直接使用
from supercc.claude.memory_tools import (
    memory_add_user,
    memory_delete_user,
    memory_update_user,
    memory_list_user,
    memory_search_user,
    memory_add_proj,
    memory_delete_proj,
    memory_update_proj,
    memory_list_proj,
    memory_search_proj,
)
from supercc.claude.feishu_file_tools import feishu_send_file
from supercc.claude.cron_tools import (
    cron_create,
    cron_list,
    cron_delete,
    cron_pause,
    cron_resume,
    cron_trigger,
    cron_logs,
)
from supercc.claude.skill_search_tools import skill_search
from supercc.claude.model_tools import (
    list_models,
    set_model_tool,
)


def get_supercc_mcp_server():
    return create_sdk_mcp_server(
        name="SuperCC",
        version="1.0.0",
        tools=[
            memory_add_user,
            memory_delete_user,
            memory_update_user,
            memory_list_user,
            memory_search_user,
            memory_add_proj,
            memory_delete_proj,
            memory_update_proj,
            memory_list_proj,
            memory_search_proj,
            feishu_send_file,
            cron_create,
            cron_list,
            cron_delete,
            cron_pause,
            cron_resume,
            cron_trigger,
            cron_logs,
            skill_search,
            list_models,
            set_model_tool,
        ],
    )


# 仅包含记忆相关工具的 MCP server，用于 cloud/memory 实例限制工具范围
_MEMORY_TOOLS = [
    memory_add_user,
    memory_delete_user,
    memory_update_user,
    memory_list_user,
    memory_search_user,
    memory_add_proj,
    memory_delete_proj,
    memory_update_proj,
    memory_list_proj,
    memory_search_proj,
]


def get_memory_only_mcp_server():
    return create_sdk_mcp_server(
        name="SuperCC",
        version="1.0.0",
        tools=_MEMORY_TOOLS,
    )
