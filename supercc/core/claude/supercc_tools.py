"""SuperCC MCP 工具统一入口 — 所有内置 MCP 工具注册在一个 'SuperCC' server 下。"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server

# 导入已装饰的工具对象（SdkMcpTool），直接使用
from supercc.core.mcps.memory_tools import (
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
from supercc.core.mcps.cron_tools import (
    cron_create,
    cron_list,
    cron_delete,
    cron_pause,
    cron_resume,
    cron_trigger,
    cron_logs,
)
from supercc.core.claude.skill_search_tools import skill_search
from supercc.core.mcps.model_tools import (
    add_custom_provider_tool,
    list_models,
    set_model_tool,
)
from supercc.core.mcps.feishu_file_tools import feishu_send_file, get_chat_members
from supercc.core.mcps.feishu_history_tools import feishu_chat_history
from supercc.core.claude.wecom_tools import wecom_send_file


def get_supercc_mcp_server(include_feishu: bool = True, include_wecom: bool = False):
    tools = [
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
        add_custom_provider_tool,
    ]
    if include_feishu:
        tools.extend([feishu_send_file, get_chat_members, feishu_chat_history])
    if include_wecom:
        tools.append(wecom_send_file)
    return create_sdk_mcp_server(
        name="SuperCC",
        version="1.0.0",
        tools=tools,
    )


def get_memory_only_mcp_server():
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
        ],
    )
