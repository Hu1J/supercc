# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.6.5] - 2026-04-22

### Added

- **SuperCC 迁移支持（最终版）**：当 PyPI 上存在 `supercc` 包时，`/update` 自动执行数据迁移后安装并切换到 SuperCC。迁移内容：家目录 `~/.cc-feishu-bridge/memories.db` → `~/.supercc/memories.db`；项目目录 `.cc-feishu-bridge/` → `.supercc/`（`cron_jobs.json`、清理后的 `config.yaml`、`skills/`）。此版本为 cc-feishu-bridge 最终版本，之后不再维护，所有新功能将在 SuperCC 中继续开发。
- **`auto-migration-update` Skill**：记录项目重命名/迁移时的自动化升级模式。

### Changed

- **重启指令动态化**：`_start_bridge`、`_restart_to`、`_do_update` 增加 `package` 参数，升级到 SuperCC 后自动使用 `supercc start` 启动，数据目录使用 `.supercc/`。
- **更新流程重构**：`/update` 检测到 supercc 时自动完成迁移、安装、重启，全程无需手动操作。

## [0.6.4] - 2026-04-22

### Fixed

- **cron notify_at 正确收集中间消息**：当 notify_at 设置时，禁用实时流式推送，所有输出（中间消息 + 最终响应）统一在 notify_at 时间发送。修复中间消息丢失的问题。
- **pending_store.remove bug**：修复 remove() 使用 job_id 而非 pending_key 导致永远删不掉 entry 的问题。

## [0.6.3] - 2026-04-21

### Fixed

- **Skill 自进化通知双发**：`poll_skill_changes_and_notify` 每 60s 轮询时统一发通知，`_detect_skill_changes` 新增 `notify=False` 参数，`trigger_skill_review` 和 cron skill scan job 传 `notify=False` 避免重复通知。

## [0.6.2] - 2026-04-21

### Fixed

- **get_message 支持图片消息**：`get_message()` 优先尝试 `.message`，失败后回退到 `.items[0]`，解决图片消息下载返回空的问题。
- **文件图标**：飞书文件消息通知图标从 📎 改为 🗃。

## [0.6.1] - 2026-04-21

### Fixed

- **Skill symlink 自动链接**：修复 `poll_skill_changes_and_notify` 中 `_ensure_symlinks` 只在检测到变更时才调用的问题，改为每次 tick 无条件调用（函数本身幂等）。同时首次运行（状态文件为空）时也会调用。新建或更新 Skill 时自动同步 symlink 到 `~/.claude/skills/`。

## [0.6.0] - 2026-04-21

### Added

- **Skill Self-Evolution（技能自进化）**：新增 `skill_nudge.py` 模块，每 10 次工具调用触发一次 skill 质量审核。独立 Claude session 在后台运行，评估 skill 改进建议并自动 git commit。启动时自动检测并初始化 skills 目录的 git 仓库。
- **记忆做梦（Memory Dream）**：新增 `dream.py` 模块，每日凌晨 3 点自动精炼记忆库（合并冗余、精简内容、删除过时），早上 8 点推送总结报告到飞书。
- **三次独立 Claude 实例**：主对话（`self.claude`）、记忆优化（`self.claude_memory`）、技能审核（`self.claude_skill`）三个实例各自独立 session，互不干扰。
- **Cron Verbose 模式**：支持流式中间过程推送，执行日志完整记录每个阶段（JOB_TRIGGERED → CLAUDE_QUERY → FEISHU_DELIVERY）。
- **Cron 统一渲染**：`_DiffMarker`、`_MemoryCardMarker`、`_AskUserQuestionMarker` 在 cron verbose 和主对话中复用同一套渲染逻辑。
- **`_MemoryCardMarker.render()` 和 `_DiffMarker.render()`**：为 cron verbose 模式新增 markdown 渲染方法，主对话走飞书卡片，cron verbose 走飞书帖子。
- **`notify_at` 延迟通知**：cron 任务支持 `notify_at` 字段，延迟到指定时间才推送执行结果。
- **项目记忆标题注入**：每次对话除了注入用户偏好，还自动注入当前项目最新 5 条记忆的标题。
- **`/cron` 命令**：飞书端管理定时任务（add/del/list/pause/resume/run）。

### Changed

- **`_get_tfidf_cache()` 锁优化**：从"全锁"改为双检查锁定（无锁快路径 + 有锁写入），允许并发读。
- **`inject_context` 内容截断**：用户偏好 content 截断到 200 字符，防止 context 膨胀。
- **`trigger_skill_review` 触发时机**：从 stream_callback 移到 finally block（typing off 之后），确保 skill 审核不在流式输出过程中打扰用户。
- **skills 目录 git 检测**：从 `git rev-parse --git-dir` 改为 `Path(".git").exists()`，避免父目录 .git 误判。
- **Cron 重叠防护**：新增 `_running_jobs` 集合防止同一 job 在 60s tick 内重复触发。

### Fixed

- **`_DiffMarker.render()` 垃圾输出**：修复将 `list[DiffLine]` 直接插 f-string 的问题，正确格式化 diff 文本。
- **`result.card_md` 属性不存在**：`send_interactive_reply` 失败时 fallback 使用本地 `card_md` 变量而非不存在的 `result.card_md`。
- **`Callable` 未导入**：修复 `memory_manager.py` 中 `Callable` 类型注解缺少导入的问题。
- **`max()` 空序列崩溃**：修复 `inject_context` 中 prefs 为空时 `max()` 抛 `ValueError` 的问题。
- **`remove_typing_reaction` finally 无保护**：Feishu API 失败时会替换原异常，现加局部 try/except。
- **`trigger_skill_review` finally 无保护**：启动失败会替换原异常，现加局部 try/except。
- **MCP 工具调用中文乱码**：`json.dumps` 使用 `ensure_ascii=False` 保留中文。
- **`session_id` 碰撞**：同一秒创建的多个 session 因 UUID 后缀碰撞 → 使用更长的 UUID。
- **Skill Nudge 在 query 完成前触发**：修复 skill 审核在流式输出过程中触发的问题，改为 query 完成后触发。

## [0.5.1] - 2026-04-20

### Fixed

- **Group @mention 鉴权**：修复群聊 @mention 消息被 `allowed_users` 白名单错误拦截的问题，群聊访问控制现完全由 `GroupConfigEntry` 管理

## [0.5.0] - 2026-04-20

### Added

- **MCP Cron Scheduler**：新增 7 个 BridgeCron* MCP 工具（Create/List/Delete/Pause/Resume/Trigger/Logs），支持 cron 表达式、间隔和一次性任务，自动通过 Feishu 推送执行结果
- **Feishu 独立消息发送**：新增 `send_post()` 和 `send_interactive_card()` 方法，无需 reply_to_message_id 即可发送独立消息
- **Cron 日志全链路追踪**：执行日志记录每个阶段（JOB_TRIGGERED → CLAUDE_QUERY → FEISHU_DELIVERY），包含真实总耗时

### Removed

- **ProactiveScheduler**：移除沉默检测主动推送功能，功能已被 BridgeCron 取代

## [0.4.3] - 2026-04-20

### Fixed

- **Group @mention 修复**：修复 lark-oapi 返回的 `Message` 对象类型导致 `mentions` 检测失效的问题，改用 `getattr` 安全访问；增加 `@_user_\d+` 正则回退检测
- **Proactive scheduler daily cap**：改为按用户维度聚合所有 session 的发送次数，而非按 session 独立计数
- **Group history sender 解析**：修复 `get_chat_history` 返回的 lark-oapi `Sender` 对象属性访问方式

### Changed

- **Typing emoji 改动**：开始处理 → 发 "OK" emoji，完成 → 发 "DONE" emoji（两者共存）
- **启动检查**：移除 Claude CLI 安装检测，依赖 SDK 自带的 Claude Code

## [0.4.2] - 2026-04-19

### Fixed

- **Proactive scheduler 修复**：
  - `last_message_at` 从未更新导致沉默检测失效、无限发送的问题
  - Daily cap 时区不匹配问题（UTC vs 本地时间导致 GMT+8 下凌晨时段 cap 失效）
  - 默认 `check_interval_minutes` 从 5 调整为 30，`cooldown_minutes` 从 60 调整为 120
- **Group history via Feishu API**：添加 `get_chat_history()` 方法，通过飞书 `im/v1/messages` 接口拉取群聊历史（需要 `im:message.group_msg` 权限）
- **Group history 存储修复**：将历史存储从 `_process_message` 移至 `handle()` 入口，确保命令消息也被正确记录
- **README**：补充 `im:message.group_msg` 权限说明

## [0.4.1] - 2026-04-19

### Added

- **群聊支持**：机器人现在支持飞书群聊 @
  - 自动识别群聊消息（`chat_type === 'group'`）
  - 自动探测 bot_open_id：启动时调用 `/open-apis/bot/v1/openclaw_bot/ping` API，无需手动配置
  - 所有群消息记录到内存（最近 20 条），@CC 时注入上下文让 AI 理解讨论背景
  - `@_user_1 /command` 格式的命令识别（mention 前缀自动剥离）
  - per-group 访问控制：`enabled`、`require_mention`、`allow_from` 三级配置
  - 新群自动注册到 `config.yaml`，默认 require_mention=True
  - 群聊 session 按 `user_id + chat_id` 隔离，与 P2P session 分开

### Changed

- `IncomingMessage` 新增字段：`is_group_chat`、`chat_type`、`mention_bot`、`mention_ids`、`group_name`
- `FeishuConfig` 新增字段：`bot_open_id`、`groups`

### Fixed

- **群聊命令识别**：修复 `@_user_1 /git` 因 mention 前缀导致斜杠命令无法识别的问题
- **session_id 碰撞**：同一秒创建的多个 session 因 UUID 后缀导致碰撞 → 已修复

## [0.3.24] - 2026-04-14

### Changed
- **SDK 版本要求**：升级 `claude-agent-sdk` 最低版本要求到 `>=0.1.59`

## [0.3.23] - 2026-04-14

### Fixed
- **主动推送重复发送**：修复同一用户多个 session 时会收到多次推送的问题，按 chat_id 去重
- **ClaudeIntegration mark_system_prompt_stale 缺失**：MessageHandler 初始化时找不到此方法导致启动报错

### Refactored
- **query 打断机制**：移除 interrupt_current 方法，改用 listener 协程 + stop_event 模式
- **清理未使用代码**：移除 integration.py 中未使用的 Optional 导入和 cwd 参数

## [0.3.22] - 2026-04-13

### Changed
- **/git 变更文件展示**：改用 git 标准字母（A/M/D/R 等），按类型着色（新增=绿，修改=橙，删除=红）
- **/status 会话ID**：改显示 sdk_session_id 而非内部 session_id
- **/restart 超时**：启动新实例超时从 8 秒延长到 60 秒

## [0.3.20] - 2026-04-13

### Fixed
- **/stop 打断机制**：简化 interrupt 流程，发 SIGINT 后直接 drain 残留消息，加 10 秒超时保护防止永久卡住
- **query 锁机制**：`async with _query_lock` 防止并发消费消息流
- **interrupt 重入保护**：`_interrupt_lock` 防止 `interrupt_current` 重入调用

### Added
- **新 Session 检测通知**：当 `sdk_session_id` 与上次不同时，飞书通知用户并显示新 Session ID

## [0.3.19] - 2026-04-13

### Fixed
- **Windows initialize() 超时**：移除 `cli_path` 参数，解决 Windows 上 `Control request timeout: initialize` 超时问题。根因是显式传入 `cli_path` 绕过 SDK 内置 bundled CLI，改为使用 npm 的 `claude.CMD` 包装器，Windows 上 `anyio.open_process` 处理 .CMD 文件有问题

### Changed
- **移除 max_turns 限制**：对话不再限制最大轮数

## [0.3.18] - 2026-04-12

### Fixed
- **主动推送与正常对话并发冲突**：ProactiveScheduler 每次推送创建独立 ClaudeIntegration 实例，用完立即 disconnect()，不再与 MessageHandler 共享进程
- **connect 3次重试机制**：缓解 Windows SDK 初始化超时（Control request timeout: initialize）
- **3次重试全失败时发送错误消息**：SDK 空响应重试 3 次全部失败，不再静默吞掉，主动通知用户

### Added
- **SDK 空响应自动重试**：收到空响应时自动重试，最多重试 3 次
- **ClaudeIntegration.ensure_connected()**：公共懒连接方法，统一 MessageHandler 和主动推送的连接逻辑
- **标题去重**：主动推送消息去除 Claude 回复中自带的 emoji 标题前缀，避免「📋 项目进展提醒」重复

### Changed
- **/stop 不再主动 disconnect**：保持 session 连续性，避免上下文丢失

## [0.3.17] - 2026-04-11

### Fixed
- **/stop 打断失效**：interrupt 后 disconnect CLI 进程，避免 session 上下文损坏导致下次对话牛头不对马嘴
- **/stop 重复调用 crash**：增加 `_worker_task.done()` 检查，防止连续两次调用导致 `AttributeError`

### Added
- **用户偏好工具自动获取 user_open_id**：MemoryAddUser/MemoryListUser/MemorySearchUser 不再需要传 user_open_id，MCP 内部自动从当前活跃会话获取
- **MEMORY_SYSTEM_GUIDE 用户记忆说明**：新增用户偏好工具说明

### Changed
- **移除分页机制**：用户偏好和项目记忆列表一次性输出，不再有翻页

## [0.3.16] - 2026-04-11

### Added
- **AskUserQuestion 精美卡片**：Claude Code 的 `AskUserQuestion` 工具调用渲染为飞书 Interactive Card，展示问题和选项，而非纯文本

### Changed
- **SDK session 管理简化**：只用 `continue_conversation=True`，不再手动传 session_id，代码大幅简化
- **CLI 懒连接机制**：`connect()` 只在第一条消息时调用，`disconnect()` 不再由外部调用

### Fixed
- **error_notifier 线程安全**：`asyncio.run_coroutine_threadsafe` 替代 `call_soon_threadsafe` 修复 `_send_async` 未正确 await 的问题
- **fork_session 会话冲突**：fork 冲突时自动降级到全新会话，不再报错
- **AskUserQuestion 卡片 tag 元素**：Feishu Card 不支持 `tag` 元素，改为 `markdown` 元素

## [0.3.14] - 2026-04-07

### Added
- **FeishuSendFile MCP 工具**：新增 `FeishuSendFile(file_paths: list[str])` MCP 工具，CC 在飞书对话中可直接调用发送文件/图片，自动判断文件类型（图片直接发送，其他文件先上传再发送），支持多文件并发
- **FEISHU_FILE_GUIDE**：飞书文件发送引导词注入 system prompt，CC 知道在用户要求发送文件时调用该工具
- **相对路径解析**：发送文件时自动尝试将相对路径解析为绝对路径（优先从 approved_directory 查找，兜底当前工作目录）

### Fixed
- **相对路径无法发送**：之前 CC 传相对路径时文件找不到，现在会自动解析

## [0.3.13] - 2026-04-07

### Added
- **Feishu Interactive Card 记忆卡片**：记忆 MCP 工具（add/update/list/search/delete）结果通过 Feishu 交互卡片展示，每种操作有专属渲染格式
- **用户偏好内存缓存 `_prefs_cache`**：按 `(db_path, user_open_id)` 隔离，`add/update/delete` 均主动失效缓存
- **system_prompt append 注入**：记忆指南和用户偏好通过 `--append-system-prompt` 追加到 claude_code 默认系统提示词，而非拼接在用户消息前
- **`__PREFS_VERSION` 热更新**：偏好更新后 `updated_at` 变化使版号改变，CC 下一条消息自动获取最新偏好

### Changed
- **记忆指南触发时机**：从"遇到问题"扩展为"收到用户提问"或"开始开发前"主动检索
- **项目记忆默认 project_path**：CC 调用时未传 `project_path` 则自动使用当前项目路径兜底

### Fixed
- **restart 偶发崩溃**：`_start_bridge` 子进程 `stdin=subprocess.DEVNULL`，避免父进程退出时文件描述符损坏导致 `Bad file descriptor`
- **delete 卡片"无结果"**：CC 先删 DB 再渲染卡片导致查不到记录，简化为展示被删记忆 ID
- **list/search 顶部 project_path 不一致**：为空时改用 `self._current_project_path` 兜底

## [0.3.12] - 2026-04-06

### Fixed
- **`.zip` 文件发送失败**：飞书 file_type 不支持 `zip` 类型，报 `Invalid request param`；改为 `stream` 解决

## [0.3.11] - 2026-04-06

### Fixed
- **`memory_manager` `TypeError`**：修复 `get_all_preferences()` 使用 `SELECT *` 时，数据库多余字段导致 `UserPreference` 构造失败的错误

### Changed
- **文件存储命名**：文件名格式改为 `原文件名_时间戳.后缀`，替换旧格式 `file_时间戳_message_id_文件名.后缀`
- **文件名保留 Unicode**：修复中文文件名被错误处理的问题，`sanitize_filename` 只移除文件系统真正危险的字符
- **飞书文件类型映射扩充**：新增 200+ 扩展名映射；`.txt` 从 `"txt"` 改为 `"stream"`（飞书不支持 txt 类型）；未知扩展名默认改为 `stream` 而非 `bin`

## [0.3.10] - 2026-04-06

### Fixed
- **`/update` 步骤越界崩溃**：修复重启流程从 4 步改为 5 步后，`/update` 总步骤数未同步更新导致 `list index out of range` 崩溃；步骤总数从 7 改为 8

## [0.3.9] - 2026-04-06

### Added
- **`/status` 显示 PID**：`/status` 新增展示当前 bridge 进程的 PID，便于排查

### Changed
- **重启流程重构**：从 4 步改为 5 步（准备重启 → 清理文件锁 → 启动新实例 → 检查新实例 → 重启完成），增加 pid 文件和 filelock 双重验证，确保旧进程彻底退出

## [0.3.8] - 2026-04-06

### Fixed
- **check_version 兼容性**：改用 PyPI JSON API 检查版本，不再依赖 `pip index`（experimental 命令，部分 pip 版本不支持）
- **更新展示优化**：`/update` 检查时展示当前版本和最新版本，已是最新时明确告知

## [0.3.7] - 2026-04-05

### Changed
- **FTS5 中文分词优化**：使用 jieba 预分词，插入和搜索时均使用 jieba 分词，大幅提升中文关键词搜索准确率
- **搜索排序优化**：改用 bm25 相关性排序，相关度高的结果优先返回
- **CLI 接口重构**：`/memory` 命令支持 `user` / `proj` 子命令（add / del / update / list / search）

### Fixed
- **FTS 表同步修复**：删除和更新操作现在同步 FTS 表，不再遗漏
- **工具引用修复**：修复 `memory_tools.py` 中工具列表引用错误的函数名

## [0.3.6] - 2026-04-05

### Changed
- **记忆系统完全重新设计**：简化为两张表，`user_preferences`（全局）和 `project_memories`（按项目隔离），统一字段为标题+内容+关键词
- **移除 problem_solution / project_context / user_preference 三种类型**：统一为用户偏好和项目记忆两类
- **inject_context 行为变更**：现在只返回用户偏好，不再搜 problem_solution

### Fixed
- **Read 工具展示优化**：有 offset/limit 参数时，标题行附加 `— offset N — limit M`

### Fixed
- **记忆查询作用域隔离**：修复 project_context 查询的 `project_path IS NULL` 误匹配问题

## [0.3.5] - 2026-04-05

### Fixed
- **记忆查询作用域隔离**：修复 `search()` 和 `get_by_project()` 中 project_context 查询的 `project_path IS NULL` 误匹配问题，确保项目背景严格按路径隔离

### Changed
- **Read 工具展示优化**：有 offset/limit 参数时，标题行附加 `— offset N — limit M`，便于快速了解读取范围

### Changed
- **记忆系统改由 CC 自驱**：移除简陋的关键词规则引擎 `_try_extract_memory`，改为在每次对话时向 CC 注入固定提示词，引导 CC 遇到报错时主动搜记忆、解决后主动问用户是否记住、用户说"记住"时直接写入

## [0.3.4] - 2026-04-05

## [0.3.3] - 2026-04-05

### Fixed
- **`/memory list` 报错**：`message_handler.py` 的 `_handle_memory()` 漏掉了 `list` 子命令处理，发 `/memory list` 时误报"未知子命令"，现已补全

## [0.3.0] - 2026-04-05

### Added
- **记忆增强系统**：本地 SQLite+FTS5 存储，记忆库位于 `~/.cc-feishu-bridge/memories.db`，所有项目全局共享
- **cc-memory-search skill**：CC 遇到报错时自动使用此 skill 搜索本地记忆库获取解决方案；skill 在 bridge 启动时自动安装到 `~/.claude/skills/`
- **`/memory` 指令**：飞书端管理记忆，支持 list / add / search / delete / clear 子命令
- **FTS5 全文搜索**：关键词检索，命中次数（use_count）越高的记忆越靠前
- **记忆类型与作用域**：
  - `problem_solution`（问题解决）— 全局共享，CC 通过 skill 按需搜索
  - `user_preference`（用户偏好）— 全局共享，每次对话自动注入 prompt
  - `project_context`（项目背景）— 项目隔离，每次对话自动注入 prompt
- **自动提取**：会话成功解决报错后，自动提取对话中的错误+解决方案写入记忆库

### Fixed
- **`/git` 工作区干净时不显示提交历史**：修复因条件判断错误导致无变更时 commit 历史被隐藏的问题

## [0.2.9] - 2026-04-05

### Fixed
- **`/help` 和 `/git` 报错**：`MessageHandler` 类缩进错误导致 `_safe_send`、`_handle_git` 等方法不在类内，引发 `AttributeError`
- **`/update` 已是最新时 bridge 意外死亡**：`os._exit(0)` 无条件执行，现改为 `run_update` 返回 bool，只有真正更新才 exit
- **`/update` 版本相同误触发更新**：`__version__` 硬编码为 `0.2.6`，现改为 `importlib.metadata` 动态读取，始终与 pip 安装版本一致
- **`/status` 显示版本号错误**：同上
- **Edit 工具降级路径报错**：fallback 分支错误引用 `marker.message_id`，`_DiffMarker` 无此字段，修复为 `message.message_id`
- **卡片行号对齐**：零填充（`01`、`02`…）替代空格右对齐，避免等宽字体压缩导致错位

### Changed
- **`__version__`**：从 `importlib.metadata` 动态读取，版本号与 PyPI 安装包始终一致

## [0.2.7] - 2026-04-05

### Added
- **`/restart` 飞书指令**：热重启当前 bridge 实例，所有通知卡片在退出前发完
- **`/update` 飞书指令**：检查 PyPI 最新版本，有更新则下载并自动 restart

### Removed
- **CLI 桌面客户端发布**：取消 GitHub Release 和 PyInstaller 多平台二进制打包，用户通过 pip 或源码安装；`/restart` 和 `/update` 指令保留，通过飞书指令使用

## [0.2.6] - 2026-04-05

### Changed
- **`cc-feishu-bridge stop`**：不再需要传入 PID，直接停掉当前目录下运行的 bridge 实例；当前目录无 bridge 时给出明确提示

## [0.2.5] - 2026-04-05

### Fixed
- **CLI switch 飞书通知**：修复 `cc-feishu-bridge switch` 执行时飞书消息不发送的问题——`asyncio.new_event_loop()` 创建后未设为当前线程 active loop，导致 FeishuClient/aiohttp 异步请求失败
- **approved_directory 路径重写**：切换项目时拷贝 config.yaml 同时重写 `claude.approved_directory` 为目标目录（之前只重写 `storage.db_path`）

## [0.2.4] - 2026-04-04

### Added
- **消息存储**：所有收到的用户消息自动写入 `messages` 表（原始 JSON + 处理后文本），为未来记忆增强打下基础

## [0.2.3] - 2026-04-04

### Changed
- **日志打印原始消息**：`ws_client.py` 日志字段从 `content`（已提取文本）改为 `raw_content`（原始 JSON 字符串），便于调试音频等特殊消息格式

### Removed
- **移除 server 配置**：删除了未使用的 `host`/`port`/`webhook` 配置项及其相关代码和 README 文档

### Added
- **README 文档完善**：新增主动推送功能说明、`/git` 指令使用说明及功能截图展示

## [0.2.2] - 2026-04-04

### Added
- **/git 命令**：直接展示当前项目 git status（emoji 状态标识）和最近 5 次提交（表格形式）
- **TodoWrite 卡片**：Claude 发出 TodoWrite 工具调用时自动拦截，渲染为待办事项表格，支持 pending/in_progress/completed 三种状态图标
- **README 截图展示**：新增功能截图展示区域

### Changed
- **Edit/Write 彩色 Diff 卡片**：使用 LCS 算法计算行级 diff，通过飞书 `lark_md` + `<font color>` 标签实现红色（删除）、绿色（新增）、灰色（上下文）着色，每行附带行号
- **Bash 工具格式化**：解析 `command` 和 `description` 字段，description 显示在标题行，命令以 ` ```bash ` 代码段呈现
- **Read 工具格式化**：提取 `file_path`，以换行 + backtick 包裹路径的形式展示
- **卡片发送失败降级**：当 Edit/Write 卡片发送失败时，自动降级为带图标的纯文本提示，确保用户始终收到通知
- **错误通知**：内部异常时主动向用户发送错误提示，而非静默丢弃
- **工具调用通知样式全面升级**：Read / Bash / Edit / Write 告别纯 backtick 格式，改为语义化展示

## [0.2.0] - 2026-04-04

### Added
- **Edit/Write 工具彩色 Diff 渲染**：使用飞书 `annotated_text` 逐行着色，红色为删除、绿色为新增、灰色为上下文，大幅提升代码变更可读性
- **主动联系冷却机制**：新增 `cooldown_minutes` 配置项，避免过于频繁地主动向用户推送消息

### Fixed
- **工具调用参数中文乱码**：修复 `json.dumps` 默认 `ensure_ascii=True` 导致中文被转义为 Unicode escape 的问题
- **文件扩展名错误**：修复下载 .txt 和 .csv 文件时被错误保存为 .bin 的问题

## [0.1.6] - 2026-04-03

### Added
- **启动 Banner**：终端和日志文件同步打印红色 `cc-feishu-bridge v{version}` + 绿色 `started at {timestamp}`
- **PyPI 自动发布**：推送 tag 时自动触发 GitHub Actions 构建 whl 并发布到 PyPI，同时验证 tag 版本与 pyproject.toml 一致

### Changed
- **主动推送默认开启**：`ProactiveConfig.enabled` 默认为 `True`（之前为 `False`）
- **旧配置自动升级**：老用户 config.yaml 无 proactive 字段时，首次启动自动补全

### Fixed
- **ProactiveScheduler 事件循环**：修复在同步上下文中调用 `start()` 时 `asyncio.create_task()` 报错的问题，改为独立 daemon 线程运行自己的事件循环

## [0.1.7] - 2026-04-03

### Fixed
- **主动推送冷却机制**：修复发完通知后无冷却期导致频繁重复提醒的问题，新增 `cooldown_minutes`（默认 60 分钟）配置；发完后记录 `last_proactive_at` 时间戳，同会话冷却期内不再触发

### Changed
- **沉默阈值调高**：`silence_threshold_minutes` 默认值从 60 分钟调整为 90 分钟，减少误触发

## [0.1.8] - 2026-04-03

### Added
- **`-v` / `--version` 参数**：支持 `cc-feishu-bridge -v` / `--version` 显示版本号，与 pyproject.toml 版本同步

### Fixed
- **文件扩展名修复**：接收文件时优先使用原始文件名扩展名，不再被飞书返回的 `file_type` 带跑（例如 txt 文件不会变成 .bin）；同时修正 `guess_file_type` 中 `.txt` → `"stream"` 的错误映射

## [0.1.9] - 2026-04-03

### Fixed
- **MCP 工具调用中文乱码**：修复 `json.dumps` 默认 `ensure_ascii=True` 导致工具参数中的中文被转义为 Unicode escape 的问题，改为 `ensure_ascii=False` 保留原始中文；同时移除对工具输入日志的截断

<!--
发版流程：
1. 在上方 [Unreleased] 区域填入本次变更内容
2. 创建 tag：git tag vx.x.x && git push --tags
3. GitHub Actions 自动读取本文件作为 Release 说明
4. 发版完成后，将 [Unreleased] 内容移至正式版本块，日期填当天，清空 [Unreleased]
-->

## [0.1.3] - 2026-04-02

### Fixed
- **Claude 检查提前**：在 WS 连接前检查 Claude CLI 可用性，找不到直接报错退出，不再先连上飞书才发现
- **/stop 修复**：修复偶发情况下 `/stop` 报"没有正在运行的查询"的问题（race condition：`create_task` 后协程未执行时 `task.done()` 已为 False）
- **Windows Claude 路径**：把 `cli_path="claude"` 解析成完整路径，解决 Windows npm 安装的 `claude.cmd` 子进程找不到的问题
- **Windows emoji 日志**：用 SafeStreamHandler 捕获 UnicodeEncodeError，避免 Windows GBK 控制台无法输出 emoji 导致日志报错
- **会话续接修复**：`continue_conversation` 必须在 `ClaudeSDKClient` 创建前设置，SDK 在 `__init__` 时已读取该选项

### Changed
- `/feishu` 帮助指令改名为 `/help`，更直观
- README 调整安装方式顺序，pip 安装推荐优先
- 移除 PyInstaller 中冗余的 `qrcode_terminal` 隐式导入

## [0.1.2] - 2026-04-02

### Added
- **全局消息队列**：所有用户消息统一进入 FIFO 队列，由单一 Worker 串行处理，支持多用户并发和同一用户连续消息有序执行
- **回复链（Threaded Reply）**：Claude 的所有回复均以飞书引用回复（Reply API）的形式发送，对话结构清晰
- **引用消息感知**：用户引用某条消息发送时，Claude 自动获取被引用内容并注入 prompt，格式为 `[引用消息: id] 发送者: 内容`；若引用消息不可用则降级显示 `[引用消息不可用: id]`
- **音频消息支持**：用户发送语音消息时下载为 `.opus` 文件，以 `[Audio: path]` 格式传给 Claude
- **/stop 打断指令**：用户发送 `/stop` 立即中断 Claude 当前查询，同时取消后台 Worker 任务
- **多文件并发发送**：`cc-feishu-bridge send` 支持一次传入多个文件，所有文件并发上传、并发发送，显著提升批量发送速度（图片、文件可混合）
- **Stream 实时推送**：Claude 生成回复时，文字片段实时推送到飞书（带缓冲，工具调用时 flush），避免碎片刷屏；如果流式过程中已发送过文字，则跳过最终完整回复，避免重复
- **工具图标**：未知工具的兜底图标从 🔧 改为 🤖
- **图片 prompt 格式修复**：接收图片时使用 `![image](path)` markdown 格式，确保 Claude Code CLI 的 `detectAndLoadPromptImages` 正确识别并描述图片
- **单实例锁**：使用 `filelock` 确保同一机器同时只有一个 bridge 进程运行，避免重复连接飞书 WS

### Changed
- `/feishu` 帮助指令改名为 `/help`，更直观

### Fixed
- 修复富文本消息（Rich Post）中图片 key 的提取
- 修复 WS 事件中图片消息 content 缺少 `image_key` 的问题（改用 API 获取）
- 修复 BytesIO 媒体下载后的读取方式（`response.file.read()`）
- 降低 WS 解析兜底日志级别（`warning` → `debug`）

## [0.1.1] - 2026-04-02

### Added
- **双向图片/文件传输**：用户发送图片或文件给机器人，Claude 可以读取并处理；Claude 生成的图片会自动发回飞书
  - 图片：下载保存至 `.cc-feishu-bridge/received_images/`，以本地路径传给 Claude
  - 文件：下载保存至 `.cc-feishu-bridge/received_files/`，以本地路径传给 Claude
  - Claude 返回的图片：以 base64 接收，上传至飞书后发回聊天

### Fixed
- 修复 `test_integration.py` 中引用不存在方法 `_parse_event` 的问题
- 修复 `test_main_ws.py` 中旧包名 `src.main` 的问题

## [0.1.0] - 2026-04-01

### Added
- 初始版本，支持飞书文字消息收发
- 扫码安装流程
- `/new` 和 `/status` 命令
- bypass 风险提示（首次确认后记录到配置）
- 回复内容记录到日志
