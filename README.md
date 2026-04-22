# SuperCC

**超级 Cloud Code** — 支持多平台（飞书、钉钉、微信、QQ 等）的 Claude Code 增强客户端，让 AI 工作搭档无处不在。

SuperCC 是 Claude Code 的超级增强版：跨平台接入、记忆自优化、技能自进化，让你可以在任意 IM 平台中与本地 Claude Code 对话。

## 核心能力

| 能力 | 说明 |
|------|------|
| **多平台接入** | 飞书为主，框架设计支持钉钉/微信/QQ 等扩展 |
| **记忆自优化** | 每日凌晨自动精炼记忆库 — 合并冗余、精简啰嗦、删除过时 |
| **技能自进化** | 每次对话后自动检查 skills 目录变化，有更新自动 git commit |
| **三次独立对话** | 主对话 / 记忆优化 / 技能审核互不干扰，各有独立 Claude session |
| **Cron 定时任务** | 标准 cron 表达式，自动推送执行结果 |
| **记忆系统** | SQLite + FTS5 中文全文搜索，每次对话注入相关记忆 |

## 快速开始

```bash
pip install -U pysupercc
supercc
```

首次运行会自动进入安装流程（飞书扫码授权 → 创建机器人）。

## 核心命令

| 命令 | 说明 |
|------|------|
| `/new` | 创建新会话 |
| `/status` | 查看当前会话状态 |
| `/stop` | 打断 Claude 当前正在执行的查询 |
| `/restart` | 重启当前实例 |
| `/update` | 检查更新，如有则自动迁移/升级 |
| `/memory` | 管理本地记忆库 |
| `/cron` | 管理定时任务 |
| `/help` | 查看所有可用命令 |

## 多平台愿景

SuperCC 的目标是让 Claude Code 在任何 IM 平台都能工作：

```
SuperCC Core（各平台共用）
├── 记忆系统（全局共享）
├── 技能进化（全局共享）
├── 对话引擎（独立 session）
└── 平台适配层（插件化）
    ├── FeishuAdapter（当前）
    ├── DingTalkAdapter（规划中）
    ├── WeChatAdapter（规划中）
    └── QQAdapter（规划中）
```

当前已实现飞书接入，钉钉/微信/QQ 接入正在规划中。

## 获取帮助

如有问题请提交 [Issue](https://github.com/Hu1J/supercc/issues)。

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)。
