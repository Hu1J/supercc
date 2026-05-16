"""CLI entry point — starts WebSocket long connection to Feishu.

Config, sessions, and data all live in .supercc/ subdirectory of the current working directory.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from supercc.banner import print_banner, write_log_banner
import shutil
import signal
import sys
from pathlib import Path


AGENTS_MD_CONTENT = """你是龙王 SuperCC，你是基于 claude code 的顶级智能体 Agent，你的任务是配合用户完成相关任务。

# 浏览器自动化
当用户需要浏览器自动化、阅读链接、搜索资料（打开本地浏览器使用百度搜索）相关的操作时，优先考虑使用：
- web access ( https://github.com/eze-is/web-access )
- browser harness ( https://github.com/browser-use/browser-harness.git )
- agent reach ( https://github.com/Panniantong/Agent-Reach )

# 编码工具
当用户需要写代码开发项目时，优先考虑使用两个技能：
- gstack ( https://github.com/garrytan/gstack )
- superpower ( https://github.com/obra/superpowers )

# 编码原则
## 四个原则详解

### 1. 编码前思考
不要假设。不要隐藏困惑。呈现权衡。

- 明确说明假设 — 如果不确定，询问而不是猜测
- 呈现多种解释 — 当存在歧义时，不要默默选择
- 适时提出异议 — 如果存在更简单的方法，说出来
- 困惑时停下来 — 指出不清楚的地方并要求澄清

### 2. 简洁优先
用最少的代码解决问题。不要过度推测。

- 不要添加要求之外的功能
- 不要为一次性代码创建抽象
- 不要为未要求的"灵活性"或"可配置性"
- 不要为不可能发生的场景做错误处理
- 如果 200 行代码可以写成 50 行，重写它

**检验标准：** 资深工程师会觉得这过于复杂吗？如果是，简化。

### 3. 精准修改
只碰必须碰的。只清理自己造成的混乱。

编辑现有代码时：
- 不要"改进"相邻的代码、注释或格式
- 不要重构没坏的东西
- 匹配现有风格，即使你更倾向于不同的写法
- 如果注意到无关的死代码，提一下 —— 不要删除它

当你的改动产生孤儿代码时：
- 删除因你的改动而变得无用的导入/变量/函数
- 不要删除预先存在的死代码，除非被要求

**检验标准：** 每一行修改都应该能直接追溯到用户的请求。

### 4. 目标驱动执行
定义成功标准。循环验证直到达成。

将指令式任务转化为可验证的目标：
| 不要这样做... | 转化为... |
|---|---|
| "添加验证" | "为无效输入编写测试，然后让它们通过" |
| "修复 bug" | "编写重现 bug 的测试，然后让它通过" |
| "重构 X" | "确保重构前后测试都能通过" |

对于多步骤任务，说明一个简短的计划：
1. [步骤] → 验证: [检查]
2. [步骤] → 验证: [检查]
3. [步骤] → 验证: [检查]

**强有力的成功标准**让 LLM 能够独立循环执行。弱标准（"让它工作"）需要不断澄清。
"""



def _ensure_agents_md(project_dir: str) -> None:
    """Ensure AGENTS.md exists in project_dir; create with default content if missing."""
    agents_md = Path(project_dir) / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(AGENTS_MD_CONTENT, encoding="utf-8")


from supercc.config import init_config, get_config, write_config, resolve_config_path, SESSIONS_DB_PATH
from supercc.adapter.feishu.client import FeishuClient, IncomingMessage
from supercc.adapter.feishu.ws_client import FeishuWSClient
from supercc.cron_scheduler import CronScheduler, _get_active_chat_id, _is_group_chat
from supercc.claude.cron_tools import set_cron_scheduler

logger = logging.getLogger(__name__)


def _register_skill_optimization_job(data_dir: str, scheduler) -> None:
    """Register a daily skill optimization scan job.

    Creates a cron job that delivers results to the active user's P2P chat.
    Only registers if active chat is P2P (not group).
    Only recreates job if the prompt has changed from the existing one.
    """
    from supercc.cron_scheduler import list_jobs, create_job, delete_job

    chat_id = _get_active_chat_id(data_dir)
    if not chat_id:
        logger.info("[skill_optimize] no active chat_id, skipping")
        return

    if _is_group_chat(data_dir, chat_id):
        logger.info("[skill_optimize] active chat is a group, skipping registration")
        return

    prompt = """【Skill 优化扫描 — 直接动手，不要只给建议】

你是熟练的工程师，直接动手解决问题，不要只给建议。发现确定的问题就立即修复。

**操作步骤：**
1. 先查看 {SKILLS_DIR}/ 目录下已有的 Skill
2. 发现有以下情况就直接动手：
   - **过时/错误内容** → 直接更新 SKILL.md（不要给建议）
   - **多个 Skill 内容重复** → 合并到最完整的一个，删除其余
   - **发现新的值得推广的模式** → 直接新建 Skill
   - **Skill 内容已无价值** → **必须先问用户确认**（删除是唯一需要确认的操作）

3. 删除前必须先向用户确认，格式：
   ```
   发现 Skill「<skill-name>」可能过时，确定要删除吗？
   ```
   用户确认后才能删除，用户拒绝则跳过

4. 每次操作后立即 `git add` + `git commit`，不要等到最后才提交

5. {SKILLS_DIR}/ 本身是一个 Git 仓库。写入 SKILL.md 后，在 {SKILLS_DIR}/ 目录下执行：
   ```
   cd {SKILLS_DIR} && git add <skill-name>/ && git commit -m "<中文 commit message>"
   ```
   commit message 必须用中文，清晰说明本次改动内容

完成后输出简短报告：做了哪些新建/更新/合并/删除操作。"""

    # Only recreate if prompt changed
    existing = list_jobs(data_dir)
    for j in existing:
        if j.get("name") == "Skill 优化扫描":
            if j.get("prompt") == prompt:
                logger.info("[skill_optimize] prompt unchanged, skipping recreation")
                return
            delete_job(j["id"], data_dir)
            logger.info("[skill_optimize] prompt changed, removed old job, will recreate")
            break

    try:
        create_job(
            prompt=prompt,
            schedule="0 4 * * *",  # 每天凌晨4点执行
            chat_id=chat_id,
            name="Skill 优化扫描",
            repeat=None,
            data_dir=data_dir,
            verbose=False,  # 不推送中间过程，只在 notify_at 发最终结果
            notify_at="0 8 * * *",  # 早上8点通知结果
        )
        logger.info("[skill_optimize] registered daily scan at 4am, notify at 8am")
    except Exception as e:
        logger.warning(f"[skill_optimize] failed to register: {e}")


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that silently ignores UnicodeEncodeError on Windows GBK consoles."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            # Fallback: encode with errors='replace' and write directly
            try:
                msg = self.format(record) + self.terminator
                encoded = msg.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                self.stream.write(encoded)
                self.flush()
            except Exception:
                pass


def _ensure_codex_mcp(config) -> None:
    """Best-effort Codex MCP setup for Claude Code."""
    try:
        from supercc.claude.codex_mcp import ensure_codex_mcp_configured

        status = ensure_codex_mcp_configured(config.codex)
        logger.info("Codex MCP status: %s", status.state)
        if status.state in {"missing_cli", "conflict", "error"}:
            logger.warning("Codex MCP not ready: %s", status.message or status.state)
    except Exception:
        logger.warning("Failed to configure Codex MCP", exc_info=True)


# ANSI color codes for terminal output


class _BaseLogFormatter(logging.Formatter):
    """Shared helpers for log formatters.

    Format: [MM-DD HH:MM:SS.mmm]  LEVEL  [module]  message
    """

    def _format_time(self, record: logging.LogRecord) -> str:
        """Format timestamp as HH:MM:SS.mmm in gray."""
        ct = record.created
        ms = int((ct - int(ct)) * 1000)
        st = self.converter(ct)
        return f"\033[90m{st.tm_hour:02d}:{st.tm_min:02d}:{st.tm_sec:02d}.{ms:03d}\033[0m"

    def _get_module(self, record: logging.LogRecord) -> str:
        """Derive short module name from logger name.

        'supercc.adapter.feishu.message_handler' -> 'feishu'
        'supercc.claude.integration' -> 'claude'
        'supercc' -> 'root'
        """
        name = record.name
        if name == "root" or not name:
            return "root"
        # websockets 库显示为 ws
        if name == "websockets":
            return "ws"
        parts = name.split(".")
        # Skip 'supercc' prefix, return first sub-module name
        if len(parts) >= 2:
            return parts[-2]
        return parts[-1]


class ColoredFormatter(_BaseLogFormatter):
    """Add ANSI color codes to log records based on level and module.

    Format: [MM-DD HH:MM:SS.mmm] LEVEL [module] message
    Colors: LEVEL by level, [module] + msg by module.
    """

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    MODULE_COLORS = {
        "supercc": "\033[38;5;214m",  # bright gold
        "evolve": "\033[32m",       # green
        "feishu": "\033[36m",       # cyan
        "adapter": "\033[33m",      # yellow
        "claude": "\033[35m",       # purple
        "gateway": "\033[31m",      # red
        "security": "\033[38;5;208m",  # orange
        "skill_search": "\033[38;5;213m",  # pink
        "core": "\033[38;5;75m",    # light blue
    }
    RESET = "\033[0m"

    def _get_module_color(self, module: str) -> str:
        for name, color in self.MODULE_COLORS.items():
            if module.startswith(name):
                return color
        return self.RESET

    def format(self, record: logging.LogRecord) -> str:
        ts = self._format_time(record)
        level_color = self.COLORS.get(record.levelname, self.RESET)
        level = f"{level_color}{record.levelname:>5}{self.RESET}"
        module = self._get_module(record)
        module_color = self._get_module_color(module)
        module_part = f"{module_color}[{module}] {self.RESET}"
        msg = f"{module_color}{record.getMessage()}{self.RESET}"
        return f"{ts} {level} {module_part}{msg}"


class PlainFormatter(_BaseLogFormatter):
    """Plain log formatter for file output (no colors).

    Format: HH:MM:SS.mmm LEVEL [module] message
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self._format_time(record)
        level = record.levelname
        module = self._get_module(record)
        return f"{ts} {level:>5} [{module}] {record.getMessage()}"


def write_pid(pid_file: str) -> None:
    """Write current PID to file."""
    Path(pid_file).write_text(str(os.getpid()))


def remove_pid(pid_file: str) -> None:
    """Remove PID file."""
    Path(pid_file).unlink(missing_ok=True)


RISK_WARNING = """
⚠️  安全风险警告 / Security Risk Warning
==============================================================

supercc 以 bypassPermissions 模式运行。
Claude Code 可以执行任意终端命令、读写本地文件，无需每次授权确认。

这意味着如果有人通过飞书向机器人发送恶意指令，攻击者可以：
  • 在你的电脑上执行任意命令
  • 读取、修改或删除你的本地文件
  • 访问你的敏感信息

请仅在可信任的网络环境下使用本工具。

supercc runs in bypassPermissions mode.
Claude Code can execute arbitrary terminal commands and read/write local files
without asking for permission each time.

Do you understand and accept these risks? (yes/no): """


def confirm_risk_warning(config_path: str) -> bool:
    """Show risk warning and get user confirmation. Saves acceptance to config on 'yes'."""
    from supercc.config import accept_bypass_warning
    print(RISK_WARNING)
    while True:
        try:
            response = input().strip().lower()
            if response in ("yes", "y"):
                accept_bypass_warning(config_path)
                print("已记录，下次启动将不再提示。")
                return True
            elif response in ("no", "n", ""):
                print("Cancelled — not starting SuperCC.")
                return False
            else:
                print("Please enter 'yes' or 'no': ", end="")
        except EOFError:
            print("no (EOF)")
            return False


async def start_bridge(config_path: str, data_dir: str) -> None:
    """Start SuperCC: core + plugins all in one asyncio event loop."""
    # Check if another instance is already running (pure PID file detection)
    pid_file = os.path.join(data_dir, "supercc.pid")
    if os.path.exists(pid_file):
        try:
            old_pid = int(Path(pid_file).read_text().strip())
            os.kill(old_pid, 0)  # Signal 0 checks if process exists
            print(f"错误：当前已有一个 SuperCC 实例正在运行 (PID {old_pid}, {data_dir})")
            print("请先停止运行中的实例: supercc stop")
            sys.exit(1)
        except (ValueError, OSError):
            # PID file is stale (invalid or process dead), continue
            pass

    config = init_config(config_path)

    # 检测是否被系统服务托管
    from supercc.gateway.platform import _is_service_installed
    is_daemon = _is_service_installed(data_dir)
    cfg = get_config()
    if cfg.daemon != is_daemon:
        cfg.daemon = is_daemon
        write_config(cfg)

    # Startup: initialize model env singleton with global ~/.supercc/model.json
    from supercc.claude.model_config import init_model_env, ensure_project_model_config
    init_model_env(config.claude.approved_directory)
    ensure_project_model_config(config.claude.approved_directory)

    # Startup: ensure Claude Code onboarding is complete (幂等，重复调用无影响)
    _ensure_codex_mcp(config)

    _ensure_agents_md(config.claude.approved_directory)

    # Write PID file for process management
    pid_file = os.path.join(data_dir, "supercc.pid")
    write_pid(pid_file)

    logger.info(f"Starting SuperCC (async mode) — data: {data_dir}")

    # Warn if git is not available
    from supercc.banner import _check_git_available
    if not _check_git_available():
        logger.warning("[SuperCC] 未检测到 git，跳过 Git 相关功能。如需使用 /git 命令，请安装 git；或跟SuperCC说: \"安装 git\"")

    # Create media subdirectories
    for sub in ("received_images", "received_files"):
        sub_dir = os.path.join(data_dir, sub)
        os.makedirs(sub_dir, exist_ok=True)

    # ── Phase 2: Core WsServer（在同一个 event loop 中）─────────────────────
    core_port = config.core.port
    from supercc.core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH
    from supercc.core.worker import WorkerPool
    from supercc.core.executor import CoreExecutor
    from supercc.core.server import WsServer

    session_manager = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
    worker_pool = WorkerPool()
    executor = CoreExecutor(
        session_manager=session_manager,
        worker_pool=worker_pool,
        config=config,
        data_dir=data_dir,
        config_path=config_path,
    )
    core_server = WsServer(
        host="127.0.0.1",
        port=core_port,
        executor=executor,
    )
    await core_server.start()
    logger.info(f"[Phase2] Core WsServer started on port {core_port}")

    # ── Plugin restart helper ───────────────────────────────────────────────
    async def _run_plugin_with_restart(name: str, config, data_dir, delay: float = 5.0):
        """运行一个 plugin task，崩溃后自动重启。"""
        while True:
            try:
                if name == "feishu":
                    from supercc.adapter.feishu.__main__ import run_plugin as _run
                elif name == "wecom":
                    from supercc.plugin.wecom.__main__ import run_plugin as _run
                await _run(config, data_dir)
            except asyncio.CancelledError:
                raise  # 有序关闭时会被外层 cancel，不继续重启
            except Exception:
                import traceback
                logger.error(f"plugin crashed, restarting in {delay}s\n{traceback.format_exc()}")
                await asyncio.sleep(delay)

    # ── Phase 3: Plugin async tasks（不复用旧 plugin __main__，直接 import）──
    plugin_tasks: list[asyncio.Task] = []

    # 飞书
    _feishu_cfg = getattr(config.channels, "feishu", None)
    if _feishu_cfg and getattr(_feishu_cfg, "enabled", False) and getattr(_feishu_cfg, "app_id", ""):
        task = asyncio.create_task(
            _run_plugin_with_restart("feishu", config, data_dir),
            name="feishu-plugin"
        )
        plugin_tasks.append(task)
        logger.info("[Bridge] Feishu plugin started (async task)")
    else:
        logger.info("[Bridge] Feishu not enabled (skipping)")

    # 企业微信
    _wecom_cfg = getattr(config.channels, "wecom", None)
    if _wecom_cfg and getattr(_wecom_cfg, "enabled", False) and getattr(_wecom_cfg, "corp_id", ""):
        task = asyncio.create_task(
            _run_plugin_with_restart("wecom", config, data_dir),
            name="wecom-plugin"
        )
        plugin_tasks.append(task)
        logger.info("[Bridge] WeCom plugin started (async task)")
    else:
        logger.info("[Bridge] WeCom not configured (skipping)")

    # ── Phase 4: Cron scheduler ────────────────────────────────────────────
    cron_scheduler = CronScheduler(config, data_dir)
    set_cron_scheduler(cron_scheduler, config)
    cron_scheduler.start()
    cron_task = asyncio.create_task(cron_scheduler._run(), name="cron-scheduler")
    logger.info("[Phase4] CronScheduler started")

    # Ensure skills directory is a git repo (init if needed)
    from supercc.evolve.skill_nudge import _ensure_skills_git_repo
    _ensure_skills_git_repo(Path(data_dir) / "skills")

    # Register daily skill optimization scan
    _register_skill_optimization_job(data_dir, cron_scheduler)

    # Register nightly dream job (memory refinement at 3am)
    from supercc.evolve.dream import register_dream_job
    register_dream_job(data_dir)

    # ── Graceful shutdown ──────────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _on_signal():
        logger.info("Received shutdown signal, stopping...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    try:
        await stop_event.wait()
    finally:
        # 1. Cancel plugin tasks
        for task in plugin_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        # Wait 0.5s for plugin graceful shutdown
        await asyncio.sleep(0.5)

        # 2. Cancel cron task
        if 'cron_task' in locals() and not cron_task.done():
            cron_task.cancel()
            try:
                await cron_task
            except asyncio.CancelledError:
                pass
        await asyncio.sleep(0.5)

        # 3. Stop core server
        if core_server:
            await core_server.stop()

        remove_pid(pid_file)
        logger.info("SuperCC stopped gracefully")
        import os as _os
        _os._exit(0)


def start_core_only(config_path: str, data_dir: str):
    """仅启动 Core (WsServer + CronScheduler)，不启动任何 Channel。

    进程隔离后，这是 supercc-main 服务的入口。
    """
    # 1. 获取锁（复用现有代码）
    lock_file = os.path.join(data_dir, ".instance.lock")
    lock = filelock.FileLock(lock_file, timeout=1)
    global _active_lock
    _active_lock = lock
    try:
        lock.acquire()
    except filelock.Timeout:
        print(f"错误：当前已有一个 SuperCC 实例正在运行 ({data_dir})")
        print("如果确认没有实例在运行，请删除 .instance.lock 文件后重试。")
        sys.exit(1)

    # 2. 初始化 config, model env（复用现有代码）
    config = init_config(config_path)
    from supercc.claude.model_config import init_model_env, ensure_project_model_config
    init_model_env(config.claude.approved_directory)
    ensure_project_model_config(config.claude.approved_directory)
    _ensure_codex_mcp(config)
    _ensure_agents_md(config.claude.approved_directory)

    # 3. 写 PID 文件
    pid_file = os.path.join(data_dir, "supercc.pid")
    write_pid(pid_file)

    # 4. 创建 media 子目录
    for sub in ("received_images", "received_files"):
        sub_dir = os.path.join(data_dir, sub)
        os.makedirs(sub_dir, exist_ok=True)

    # 5. 启动 Core WsServer（从 config 读取端口！）
    core_port = config.core.port  # 从 config 读，不是写死 8765
    logger.info(f"[Phase2] Starting Core WsServer on port {core_port}")

    # 用于 cleanup handler
    cron_scheduler = None
    core_server = None

    def run_core_server():
        import asyncio
        from supercc.core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH
        from supercc.core.worker import WorkerPool
        from supercc.core.executor import CoreExecutor
        from supercc.core.server import WsServer

        session_manager = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
        worker_pool = WorkerPool()
        executor = CoreExecutor(
            session_manager=session_manager,
            worker_pool=worker_pool,
            config=config,
            data_dir=data_dir,
            config_path=config_path,
        )
        nonlocal core_server
        core_server = WsServer(
            host="127.0.0.1",
            port=core_port,  # 从 config 读取
            executor=executor,
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(core_server.start())
        loop.run_forever()

    import threading
    core_thread = threading.Thread(target=run_core_server, daemon=True)
    core_thread.start()
    logger.info("[Phase2] Core WsServer started in background thread")

    # 6. 启动 CronScheduler
    cron_scheduler = CronScheduler(config, data_dir)
    set_cron_scheduler(cron_scheduler, config)
    cron_scheduler.start()
    logger.info("[Phase2] CronScheduler started")

    # 7. 注册 cleanup signal handler
    def cleanup(signum, frame):
        nonlocal cron_scheduler, core_server
        if cron_scheduler:
            cron_scheduler.stop()
        if core_server:
            import asyncio as _asyncio
            _asyncio.run(core_server.stop())
        remove_pid(pid_file)
        lock.release()
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 8. 阻塞主线程（让 daemon thread 一直运行）
    import time
    while True:
        time.sleep(3600)


def list_bridges() -> None:
    """List SuperCC instances by checking the current directory's .supercc/ directory."""
    project_data_dir = os.path.join(os.getcwd(), ".supercc")
    pid_file = os.path.join(project_data_dir, "supercc.pid")
    print(f"\nSuperCC data directory: {project_data_dir}")
    print(f"{'PID':<8} {'Status':<20}")
    print("-" * 40)

    if not os.path.exists(pid_file):
        print("No running instances found.")
        print()
        return

    try:
        pid = int(Path(pid_file).read_text().strip())
        try:
            os.kill(pid, 0)
            status = "running"
        except OSError:
            status = "dead (clean up pid file)"
        print(f"{pid:<8} {status}")
    except (ValueError, OSError):
        print("Invalid PID file.")
    print()


def stop_bridge(pid: int) -> None:
    """Stop a SuperCC instance by PID."""
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped PID {pid}")
    except OSError as e:
        print(f"Failed to stop PID {pid}: {e}")


def detect_config() -> tuple[bool, bool]:
    """Check config usability and YAML presence (does NOT create files).

    Returns (is_installed, yaml_exists):
    - is_installed: True if config.json is non-empty
    - yaml_exists: True if config.yaml exists (for migration)
    """
    # Use same logic as resolve_config_path() but WITHOUT creating files
    cwd = os.getcwd()
    cfg_dir = Path(cwd).resolve() / ".supercc"
    json_path = cfg_dir / "config.json"
    yaml_path = cfg_dir / "config.yaml"
    yaml_exists = yaml_path.exists() and yaml_path.stat().st_size > 0
    is_installed = json_path.exists() and json_path.stat().st_size > 0
    return (is_installed, yaml_exists)


async def interactive_install() -> tuple[str, str]:
    """Run the QR-code install flow. Returns (cfg_path, data_dir) on success."""
    from supercc.install.flow import run_install_flow
    cfg_path, data_dir = resolve_config_path()
    await run_install_flow(cfg_path)
    return cfg_path, data_dir


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


def run_send_command(file_paths: list[str], config_path: str) -> None:
    """Send one or more files to the active Feishu chat."""
    import os
    from pathlib import Path

    # 1. Load config
    if not os.path.exists(config_path):
        print(f"Error: config file not found: {config_path}")
        return
    from supercc.config import init_config
    config = init_config(config_path)

    # 2. Locate sessions.db (in ~/.supercc/)
    data_dir = str(Path(config_path).parent.resolve())
    db_path = SESSIONS_DB_PATH
    if not os.path.exists(db_path):
        print("Error: sessions.db not found. Has SuperCC ever been run?")
        return

    # 3. Find the most recently active session's chat_id
    from supercc.claude.session_manager import SessionManager
    sm = SessionManager(db_path=db_path)
    project_path = config.claude.approved_directory
    session = sm.get_active_session_by_chat_id(project_path=project_path, platform="feishu")
    if not session or not session.chat_id:
        print("Error: no active chat session found. Make sure SuperCC has been used.")
        return
    chat_id = session.chat_id
    print(f"Sending to chat: {chat_id}")

    # 4. Create FeishuClient
    from supercc.adapter.feishu.client import FeishuClient
    feishu = FeishuClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
    )

    # 5. Process each file
    import asyncio
    try:
        from supercc.adapter.feishu.media import guess_file_type
    except ImportError:
        guess_file_type = None

    async def send_one(file_path: str) -> str:
        """Send a single file. Raises on error so gather() can collect it."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        with open(file_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"{file_path} exceeds 30MB limit")
            data = f.read()

        ext = os.path.splitext(file_path)[1].lower()
        file_name = os.path.basename(file_path)

        if ext in SUPPORTED_IMAGE_EXTS:
            image_key = await feishu.upload_image(data)
            msg_id = await feishu.send_image(chat_id, image_key)
            print(f"Sent image: {file_name} → {msg_id}")
        else:
            if guess_file_type is not None:
                file_type = guess_file_type(ext)
            else:
                file_type = None
            file_key = await feishu.upload_file(data, file_name, file_type)
            msg_id = await feishu.send_file(chat_id, file_key, file_name)
            print(f"Sent file: {file_name} → {msg_id}")

        return msg_id

    async def main_async():
        # Upload all files concurrently, then send all concurrently.
        # Feishu renders consecutive image messages grouped together.
        results = await asyncio.gather(*[send_one(fp) for fp in file_paths], return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                print(f"Error sending {file_paths[i]}: {result}")

    asyncio.run(main_async())


def _run_memory_command(args) -> None:
    """Handle supercc memory <scope> <action> [args]."""
    from supercc.claude.memory_manager import get_memory_manager

    mm = get_memory_manager()
    scope = args.memory_scope  # "user" or "proj"
    action = getattr(args, "memory_action", None)  # "add", "del", "update", "list", "search"
    raw_args = " ".join(args.memory_args) if isinstance(getattr(args, "memory_args", None), list) else (getattr(args, "memory_args", "") or "")

    # 无参数时显示帮助
    if scope is None or action is None:
        print("【记忆系统指令】\n")
        print("/memory user add <title>|<content>|<keywords> — 新增用户偏好")
        print("/memory user del <id> — 删除用户偏好")
        print("/memory user update <id> <title>|<content>|<keywords> — 编辑用户偏好")
        print("/memory user list — 列出用户偏好")
        print("/memory user search <关键词> — 搜索用户偏好")
        print("")
        print("/memory proj add <title>|<content>|<keywords> — 新增项目记忆")
        print("/memory proj del <id> — 删除项目记忆")
        print("/memory proj update <id> <title>|<content>|<keywords> — 编辑项目记忆")
        print("/memory proj list — 列出项目记忆")
        print("/memory proj search <关键词> — 搜索项目记忆")
        print("")
        print("关键词用逗号分隔（若有多个）")
        return

    # Try to send results to Feishu if we're in a SuperCC session
    feishu_client = None
    feishu_chat_id = None
    config = None
    try:
        _, data_dir = resolve_config_path()
        config = get_config()
        from supercc.adapter.feishu.client import FeishuClient
        from supercc.claude.session_manager import SessionManager
        feishu_client = FeishuClient(
            app_id=config.channels.feishu.app_id,
            app_secret=config.channels.feishu.app_secret,
        )
        sm = SessionManager(db_path=SESSIONS_DB_PATH)
        project_path = config.claude.approved_directory
        session = sm.get_active_session_by_chat_id(project_path=project_path, platform="feishu")
        feishu_chat_id = session.chat_id if session and session.chat_id else None
    except Exception as e:
        logger.debug(f"Feishu push skipped (not in SuperCC session): {e}")

    def _get_user_open_id() -> str:
        if config and config.channels.feishu.allowed_users:
            return config.channels.feishu.allowed_users[0]
        return "cli-owner"

    def _get_bot_id() -> str:
        if config and hasattr(config.channels.feishu, "bot_id"):
            return config.channels.feishu.bot_id or ""
        return ""

    async def _send_feishu(text: str):
        if feishu_client and feishu_chat_id:
            await feishu_client.send_text(feishu_chat_id, text)

    def _print(text: str):
        print(text)
        # Avoid nested asyncio.run() in Python 3.10+ when already in an event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop:
            loop.create_task(_send_feishu(text))
        else:
            asyncio.run(_send_feishu(text))

    def _parse_args(args_str: str) -> list[str]:
        """Split by pipe to get title/content/keywords or id/title/content/keywords."""
        return [p.strip() for p in args_str.split("|")]

    # ── user ────────────────────────────────────────────────────────────────
    if scope == "user":
        if action == "add":
            parts = _parse_args(raw_args)
            if len(parts) < 3:
                _print("用法: supercc memory user add <title>|<content>|<keywords>")
                return
            title, content, keywords = parts[0], parts[1], parts[2]
            user_open_id = _get_user_open_id()
            p = mm.add_preference(user_open_id, title, content, keywords, bot_id=_get_bot_id())
            _print(f"✅ 用户偏好已保存 (id={p.id})")

        elif action == "del":
            if not raw_args.strip():
                _print("用法: supercc memory user del <id>")
                return
            user_open_id = _get_user_open_id()
            ok = mm.delete_preference(raw_args, user_open_id=user_open_id, bot_id=_get_bot_id())
            if ok:
                _print(f"🗑️ 用户偏好 {raw_args} 已删除。")
            else:
                _print(f"未找到 id={raw_args} 的用户偏好")

        elif action == "update":
            parts = _parse_args(raw_args)
            if len(parts) < 4:
                _print("用法: supercc memory user update <id>|<title>|<content>|<keywords>")
                return
            pref_id, title, content, keywords = parts[0], parts[1], parts[2], parts[3]
            user_open_id = _get_user_open_id()
            ok = mm.update_preference(pref_id, title, content, keywords, user_open_id=user_open_id, bot_id=_get_bot_id())
            if ok:
                _print(f"✅ 用户偏好 {pref_id} 已更新")
            else:
                _print(f"未找到 id={pref_id} 的用户偏好")

        elif action == "list":
            user_open_id = _get_user_open_id()
            prefs = mm.get_preferences_by_user(user_open_id, platform="feishu", bot_id=_get_bot_id())
            if not prefs:
                _print("📭 暂无用户偏好记录")
                return
            for p in prefs:
                print(f"\n👤 **{p.title}**  (id={p.id})")
                print(f"  {p.content}")
                print(f"  关键词: {p.keywords}")
            print(f"\n共 {len(prefs)} 条用户偏好。")
            asyncio.run(_send_feishu(f"👤 用户偏好（共 {len(prefs)} 条）"))

        elif action == "search":
            if not raw_args.strip():
                _print("用法: supercc memory user search <关键词>")
                return
            user_open_id = _get_user_open_id()
            results = mm.search_preferences(raw_args, user_open_id=user_open_id, platform="feishu", bot_id=_get_bot_id())
            if not results:
                _print(f"未找到与「{raw_args}」相关的用户偏好")
                return
            for p in results:
                print(f"\n👤 **{p.title}**  (id={p.id})")
                print(f"  {p.content}")
                print(f"  关键词: {p.keywords}")
            print(f"\n共 {len(results)} 条用户偏好。")
            asyncio.run(_send_feishu(f"🔍 找到 {len(results)} 条用户偏好"))

    # ── proj ────────────────────────────────────────────────────────────────
    elif scope == "proj":
        project_path = args.project or (config.claude.approved_directory if config else "")

        if action == "add":
            parts = _parse_args(raw_args)
            if len(parts) < 3:
                _print("用法: supercc memory proj add <title>|<content>|<keywords>")
                return
            title, content, keywords = parts[0], parts[1], parts[2]
            m = mm.add_project_memory(project_path, title, content, keywords, platform="feishu", chat_id="")
            _print(f"✅ 项目记忆已保存 (id={m.id})")

        elif action == "del":
            if not raw_args.strip():
                _print("用法: supercc memory proj del <id>")
                return
            ok = mm.delete_project_memory(raw_args, project_path, platform="feishu", chat_id="")
            if ok:
                _print(f"🗑️ 项目记忆 {raw_args} 已删除。")
            else:
                _print(f"未找到 id={raw_args} 的项目记忆")

        elif action == "update":
            parts = _parse_args(raw_args)
            if len(parts) < 4:
                _print("用法: supercc memory proj update <id>|<title>|<content>|<keywords>")
                return
            mem_id, title, content, keywords = parts[0], parts[1], parts[2], parts[3]
            ok = mm.update_project_memory(mem_id, title, content, keywords, project_path, platform="feishu", chat_id="")
            if ok:
                _print(f"✅ 项目记忆 {mem_id} 已更新")
            else:
                _print(f"未找到 id={mem_id} 的项目记忆")

        elif action == "list":
            mems = mm.get_project_memories(project_path, platform="feishu", chat_id="")
            if not mems:
                _print("📭 暂无项目记忆记录")
                return
            for m in mems:
                print(f"\n📁 **{m.title}**  (id={m.id})")
                print(f"  {m.content}")
                print(f"  关键词: {m.keywords}")
            print(f"\n共 {len(mems)} 条项目记忆。")
            asyncio.run(_send_feishu(f"📁 项目记忆（共 {len(mems)} 条）"))

        elif action == "search":
            if not raw_args.strip():
                _print("用法: supercc memory proj search <关键词>")
                return
            results = mm.search_project_memories(raw_args, project_path, platform="feishu", chat_id="")
            if not results:
                _print(f"未找到与「{raw_args}」相关的项目记忆")
                return
            for r in results:
                m = r.memory
                print(f"\n📁 **{m.title}**  (id={m.id})")
                print(f"  {m.content}")
                print(f"  关键词: {m.keywords}")
            print(f"\n共 {len(results)} 条项目记忆。")
            asyncio.run(_send_feishu(f"🔍 找到 {len(results)} 条项目记忆"))


def _run_config_interactive() -> None:
    """交互式模型配置菜单（TUI 体验：上下选择 + 回车确认）。"""
    import questionary
    from supercc.claude.model_config import (
        ModelEnv,
        ModelEntry,
        add_model,
        get_all_models,
        get_active_model,
        switch_model,
        delete_model,
    )
    from supercc.claude.model_providers import PROVIDERS

    auth_display_map = {"bearer": "Bearer API Key", "api_key": "API Key", "azure": "Azure AD Token"}

    while True:
        choice = questionary.select(
            "模型配置",
            choices=[
                questionary.Choice("➕  添加模型", value="add"),
                questionary.Choice("🔄  切换模型", value="switch"),
                questionary.Choice("🗑  删除模型", value="delete"),
                questionary.Choice("📋  查看供应商列表", value="providers"),
                questionary.Choice("❌  退出", value="quit"),
            ],
            style=questionary.Style([
                ("selected", "fg:#00AA00 bold"),
                ("choice", "fg:#CCCCCC"),
                ("pointer", "fg:#00AA00 bold"),
            ]),
        ).ask()

        if choice == "quit" or choice is None:
            break

        elif choice == "add":
            # 1. 选供应商
            provider_choices = [
                questionary.Choice(
                    f"{p.id}  ({p.base_url or '用户填入'})",
                    value=pid,
                )
                for pid, p in PROVIDERS.items()
            ]
            provider_id = questionary.select(
                "请选择供应商",
                choices=provider_choices,
                style=questionary.Style([
                    ("selected", "fg:#00AA00 bold"),
                    ("choice", "fg:#CCCCCC"),
                    ("pointer", "fg:#00AA00 bold"),
                ]),
            ).ask()
            if not provider_id:
                continue
            provider = PROVIDERS[provider_id]

            # ── custom 模式 ─────────────────────────────────────────────────
            if provider_id == "custom":
                base_url = questionary.text(
                    "Base URL（例如 https://api.example.com/v1）",
                    style=questionary.Style([("input", "fg:#CCCCCC")]),
                ).ask()
                if not base_url:
                    print("⚠️  未提供 Base URL，已取消\n")
                    continue
                base_url = base_url.strip().rstrip("/")

                selected_model = questionary.text(
                    "模型 ID（例如 gpt-4、my-model）",
                    style=questionary.Style([("input", "fg:#CCCCCC")]),
                ).ask()
                if not selected_model:
                    print("⚠️  未提供模型 ID，已取消\n")
                    continue
                selected_model = selected_model.strip()

                token = questionary.password(
                    "API Key",
                    style=questionary.Style([("password", "fg:#CCCCCC")]),
                ).ask()
                if not token:
                    print("⚠️  未提供 API Key，已取消\n")
                    continue

                provider_name_raw = questionary.text(
                    "供应商名称（可选，回车跳过使用默认 'custom'）",
                    style=questionary.Style([("input", "fg:#CCCCCC")]),
                ).ask()
                provider_name = provider_name_raw.strip() or "custom"

                import hashlib
                model_id = f"custom-{hashlib.md5(selected_model.encode()).hexdigest()[:8]}"
                name = selected_model
                env = ModelEnv(
                    ANTHROPIC_AUTH_TOKEN=token,
                    ANTHROPIC_BASE_URL=base_url,
                    ANTHROPIC_MODEL=selected_model,
                )
                added = add_model(
                    model_id, name, f"自定义供应商: {provider_name}", env, provider_name=provider_name,
                )
                switch_model(model_id)
                if not added:
                    print(f"⚠️  模型 ID `{model_id}` 已存在，已切换到该模型\n")
                else:
                    print(f"\n✅ 自定义模型 **{name}** (`{model_id}`) 已添加并设为激活")
                    print(f"   供应商: {provider_name}")
                    print(f"   Base URL: `{base_url}`")
                    print(f"   模型: `{selected_model}`\n")
                continue
            # ── 预置供应商模式 ─────────────────────────────────────────────

            # 2. 选模型
            model_choices = [
                questionary.Choice(f"`{m}`", value=m)
                for m in provider.models
            ]
            selected_model = questionary.select(
                f"请选择模型（{provider.id}）",
                choices=model_choices,
                style=questionary.Style([
                    ("selected", "fg:#00AA00 bold"),
                    ("choice", "fg:#CCCCCC"),
                    ("pointer", "fg:#00AA00 bold"),
                ]),
            ).ask()
            if not selected_model:
                continue

            # 3. 输入 Token
            auth_label = auth_display_map.get(provider.auth_type, provider.auth_type)
            token = questionary.password(
                f"API Key（{auth_label}）",
                style=questionary.Style([
                    ("password", "fg:#CCCCCC"),
                ]),
            ).ask()
            if not token:
                print("⚠️  未提供 API Key，已取消")
                continue

            # 4. 保存
            model_id = provider_id
            name = f"{provider.id} ({selected_model})"
            env = ModelEnv(
                ANTHROPIC_AUTH_TOKEN=token,
                ANTHROPIC_BASE_URL=provider.base_url,
                ANTHROPIC_MODEL=selected_model,
            )
            added = add_model(model_id, name, f"供应商: {provider.id}", env, provider_name=provider.id)
            if not added:
                print(f"⚠️  模型 ID `{model_id}` 已存在，请先切换：`supercc config switch {model_id}`")
                continue
            switch_model(model_id)
            print(f"\n✅ 模型 **{name}** (`{model_id}`) 已添加并设为激活")
            print(f"   端点: `{provider.base_url}`")
            print(f"   模型: `{selected_model}`\n")

        elif choice == "switch":
            models = get_all_models()
            if not models:
                print("⚠️  没有任何已配置的模型\n")
                continue
            active_entry = get_active_model()
            active_id = None
            if active_entry:
                for mid, mentry in models.items():
                    if mentry.env.ANTHROPIC_BASE_URL == active_entry.env.ANTHROPIC_BASE_URL:
                        active_id = mid
                        break

            model_choices = [
                questionary.Choice(
                    f"{mentry.name} (`{mid}`)" + ("  ✅" if mid == active_id else ""),
                    value=mid,
                )
                for mid, mentry in models.items()
            ]
            target_id = questionary.select(
                "请选择要切换的模型",
                choices=model_choices,
                style=questionary.Style([
                    ("selected", "fg:#00AA00 bold"),
                    ("choice", "fg:#CCCCCC"),
                    ("pointer", "fg:#00AA00 bold"),
                ]),
            ).ask()
            if not target_id or target_id == active_id:
                continue

            ok = switch_model(target_id)
            if not ok:
                print("❌ 切换失败\n")
                continue
            entry = models[target_id]
            print(f"\n✅ 已切换到 **{entry.name}**")
            print(f"   模型: `{entry.env.ANTHROPIC_MODEL}`")
            print(f"   端点: `{entry.env.ANTHROPIC_BASE_URL}`")
            print(f"\n注意: Claude Code 需要重启才能生效，使用 `supercc restart` 命令重启。\n")

        elif choice == "delete":
            models = get_all_models()
            if not models:
                print("⚠️  没有任何已配置的模型\n")
                continue
            active_entry = get_active_model()
            active_id = None
            if active_entry:
                for mid, mentry in models.items():
                    if mentry.env.ANTHROPIC_BASE_URL == active_entry.env.ANTHROPIC_BASE_URL:
                        active_id = mid
                        break

            model_choices = [
                questionary.Choice(
                    f"{mentry.name} (`{mid}`)" + ("  （当前激活）" if mid == active_id else ""),
                    value=mid,
                )
                for mid, mentry in models.items()
            ]
            target_id = questionary.select(
                "请选择要删除的模型",
                choices=model_choices,
                style=questionary.Style([
                    ("selected", "fg:#FF5555 bold"),
                    ("choice", "fg:#CCCCCC"),
                    ("pointer", "fg:#FF5555 bold"),
                ]),
            ).ask()
            if not target_id:
                continue
            if target_id == active_id:
                print("❌ 无法删除当前激活的模型，请先切换到其他模型\n")
                continue

            confirm = questionary.confirm(
                f"确认删除模型 `{target_id}`？",
                default=False,
                style=questionary.Style([
                    ("selected", "fg:#FF5555 bold"),
                ]),
            ).ask()
            if not confirm:
                continue

            ok = delete_model(target_id)
            if ok:
                print(f"✅ 模型 `{target_id}` 已删除\n")
            else:
                print("❌ 删除失败\n")

        elif choice == "providers":
            from supercc.claude.model_providers import PROVIDERS
            lines = ["支持的模型供应商：\n"]
            for pid, p in PROVIDERS.items():
                auth = auth_display_map.get(p.auth_type, p.auth_type)
                models_preview = ", ".join(p.models[:3])
                if len(p.models) > 3:
                    models_preview += f" ... (+{len(p.models) - 3})"
                lines.append(f"  `{pid}`")
                lines.append(f"    端点: {p.base_url or '(用户填入)'}")
                lines.append(f"    认证: {auth}")
                lines.append(f"    模型: {models_preview}")
                lines.append("")
            lines.append("用法: supercc config add --provider <provider_id> <api_key> <model>")
            print("\n".join(lines))


def _run_config_command(args) -> None:
    """Handle supercc config <action> [args]."""
    from supercc.claude.model_config import (
        get_all_models,
        get_active_model,
        switch_model,
        add_model,
        delete_model,
        ModelEnv,
        is_configured,
    )

    action = getattr(args, "config_action", None)
    raw_args = getattr(args, "config_args", "") or ""
    if isinstance(raw_args, list):
        raw_args = " ".join(raw_args)

    def _parse_args(args_str: str) -> list[str]:
        return [p.strip() for p in args_str.split("|")]

    def _fmt_model(model_id: str, entry, is_active: bool) -> str:
        active_mark = "✅ " if is_active else "   "
        env = entry.env
        token_display = f"***{env.ANTHROPIC_AUTH_TOKEN[-4:]:>4}" if env.ANTHROPIC_AUTH_TOKEN else "(未设置)"
        return (
            f"{active_mark}**{entry.name}** (`{model_id}`)\n"
            f"    描述: {entry.description or '(无)'}\n"
            f"    模型: `{env.ANTHROPIC_MODEL}`\n"
            f"    端点: `{env.ANTHROPIC_BASE_URL}`\n"
            f"    Token: ...{token_display}"
        )

    # 无 action（交互式菜单）或 list（只显示）
    if action is None or action == "list":
        if not is_configured():
            current_settings = {}
            try:
                from supercc.claude.model_config import get_current_claude_settings
                current_settings = get_current_claude_settings()
            except Exception:
                pass
            env_cfg = current_settings.get("env", {})
            if env_cfg.get("ANTHROPIC_AUTH_TOKEN"):
                print("📋 **检测到您已配置过 Claude Code**\n")
                print("您的现有配置：")
                print(f"- 模型: `{env_cfg.get('ANTHROPIC_MODEL', '未设置')}`")
                print(f"- 端点: `{env_cfg.get('ANTHROPIC_BASE_URL', '未设置')}`")
                print("\n💡 **建议**: 使用 `supercc config add ...` 将现有配置导入为第一个模型。")
                print("\n用法: supercc config add --provider <provider_id> <api_key> <model>")
            else:
                print("📋 **尚未配置任何模型**")
                print("\n用法: supercc config add --provider <provider_id> <api_key> <model>")
                print("\n可用供应商: supercc config providers")
            # action is None 时进入交互菜单
            if action is None:
                _run_config_interactive()
            return

        models = get_all_models()
        active_entry = get_active_model()
        active_id = None
        if active_entry:
            for mid, mentry in models.items():
                if mentry.env.ANTHROPIC_BASE_URL == active_entry.env.ANTHROPIC_BASE_URL:
                    active_id = mid
                    break

        print("🤖 **已配置的模型**\n")
        for model_id, entry in models.items():
            print(_fmt_model(model_id, entry, is_active=(model_id == active_id)))
            print()
        print(f"\n当前激活: `{(active_id or '未知')}`")

        # action is None 时进入交互菜单
        if action is None:
            _run_config_interactive()
        return

    if action == "add":
        provider_id = getattr(args, "provider", "") or ""

        if provider_id:
            # --provider 快捷模式
            from supercc.claude.model_providers import get_provider, PROVIDERS
            provider = get_provider(provider_id)
            if not provider:
                available = ", ".join(f"`{p}`" for p in PROVIDERS.keys())
                print(f"未知供应商 `{provider_id}`\n可用供应商: {available}")
                return
            if provider_id == "custom":
                print("错误: 不支持 `--provider custom` 快捷方式")
                print("用法: supercc config add <model_id>|<name>|<description>|<api_key>|<base_url>|<model>")
                print("示例: supercc config add my-model|MyModel|自定义|gpt-4-api-key|https://api.example.com/v1|gpt-4")
                return

            pos_args = raw_args.split() if raw_args else []
            if len(pos_args) < 2:
                print(f"用法: supercc config add --provider {provider_id} <api_key> <model> [model_id] [name]")
                print(f"\n{provider.id} 可用模型:")
                for m in provider.models:
                    print(f"  `{m}`")
                return

            import hashlib
            token, model = pos_args[0], pos_args[1]
            # 默认用 md5(provider_id + model) 生成唯一 ID，可指定第3参数覆盖
            model_id = pos_args[2] if len(pos_args) > 2 else hashlib.md5(f"{provider_id}{model}".encode()).hexdigest()[:8]
            name = pos_args[3] if len(pos_args) > 3 else provider.id

            env = ModelEnv(
                ANTHROPIC_AUTH_TOKEN=token,
                ANTHROPIC_BASE_URL=provider.base_url,
                ANTHROPIC_MODEL=model,
            )
            ok = add_model(model_id, name, f"供应商: {provider.id}", env)
            if not ok:
                print(f"❌ 模型 ID `{model_id}` 已存在，请使用其他 ID")
                return
            print(f"✅ 模型 **{name}** (`{model_id}`) 已添加")
            print(f"   供应商: {provider.id}")
            print(f"   模型: `{model}`")
            print(f"   端点: `{provider.base_url}`")
            print(f"\n使用 `supercc config switch {model_id}` 切换到新模型。")
            return

        if not raw_args.strip():
            print("用法: supercc config add --provider <provider_id> <api_key> <model> [model_id] [name]")
            print("       supercc config add <model_id>|<name>|<description>|<api_key>|<base_url>|<model>")
            print("\n可用供应商:")
            from supercc.claude.model_providers import PROVIDERS
            for pid, p in PROVIDERS.items():
                print(f"  `{pid}`")
            return

        parts = _parse_args(raw_args)
        if len(parts) < 6:
            print("错误: 需要 6 个参数，以 | 分隔")
            print("用法: supercc config add <model_id>|<name>|<description>|<api_key>|<base_url>|<model>")
            return
        model_id, name, description, token, base_url, model = parts
        env = ModelEnv(
            ANTHROPIC_AUTH_TOKEN=token,
            ANTHROPIC_BASE_URL=base_url,
            ANTHROPIC_MODEL=model,
        )
        ok = add_model(model_id, name, description, env)
        if not ok:
            print(f"❌ 模型 ID `{model_id}` 已存在，请使用其他 ID")
            return
        print(f"✅ 模型 **{name}** (`{model_id}`) 已添加")
        print(f"   模型: `{model}`")
        print(f"   端点: `{base_url}`")
        print(f"\n使用 `supercc config switch {model_id}` 切换到新模型。")
        return

    if action == "switch":
        if not raw_args.strip():
            print("用法: supercc config switch <model_id>")
            return
        model_id = raw_args.strip()
        ok = switch_model(model_id)
        if not ok:
            print(f"❌ 未找到模型 ID: `{model_id}`")
            return
        models = get_all_models()
        entry = models[model_id]
        print(f"✅ 已切换到 **{entry.name}**\n")
        print(f"   模型: `{entry.env.ANTHROPIC_MODEL}`")
        print(f"   端点: `{entry.env.ANTHROPIC_BASE_URL}`")
        print(f"\n注意: Claude Code 需要重启才能生效，使用 `supercc restart` 命令重启。")
        return

    if action == "delete":
        if not raw_args.strip():
            print("用法: supercc config delete <model_id>")
            return
        model_id = raw_args.strip()
        ok = delete_model(model_id)
        if not ok:
            print(f"❌ 删除模型 `{model_id}` 失败。可能原因：模型不存在或为当前激活模型。")
            return
        print(f"✅ 模型 `{model_id}` 已删除。")
        return

    if action == "providers":
        from supercc.claude.model_providers import PROVIDERS
        auth_display = {"bearer": "Bearer API Key", "api_key": "API Key", "azure": "Azure AD Token"}
        print("支持的模型供应商：\n")
        for pid, p in PROVIDERS.items():
            auth = auth_display.get(p.auth_type, p.auth_type)
            models_preview = ", ".join(p.models[:4])
            if len(p.models) > 4:
                models_preview += f" ... (+{len(p.models) - 4})"
            print(f"  `{pid}`")
            print(f"    端点: {p.base_url or '(用户填入)'}")
            print(f"    认证: {auth}")
            print(f"    模型: {models_preview}")
            print()
        print("用法: supercc config add --provider <provider_id> <api_key> <model>")
        return


def main(args=None):
    # Read version once — shared by --version flag and startup banner
    try:
        from importlib.metadata import version as _get_version

        _version = _get_version("pysupercc")
    except Exception:
        _version = "dev"

    parser = argparse.ArgumentParser(
        description="SuperCC — 超级 Claude Code，支持飞书等多平台"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"supercc {_version}",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # start (default)
    start_parser = subparsers.add_parser("start", help="Start SuperCC (default)")

    # list
    list_parser = subparsers.add_parser("list", help="List all running instances")

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop the SuperCC instance in the current directory")

    restart_parser = subparsers.add_parser("restart", help="Restart current SuperCC instance")
    update_parser = subparsers.add_parser("update", help="Check for updates and restart if needed")

    # send
    send_parser = subparsers.add_parser("send", help="Send a file or image to the active Feishu chat")
    send_parser.add_argument("files", nargs="+", help="Path(s) to the file(s) to send")
    send_parser.add_argument("--config", required=True, help="Path to config.yaml for this SuperCC instance")

    # memory
    memory_parser = subparsers.add_parser(
        "memory",
        help="Manage local memory store: user or proj subcommands",
    )
    memory_subparsers = memory_parser.add_subparsers(dest="memory_scope", help="user or proj")

    # /memory user add|del|update|list|search
    user_parser = memory_subparsers.add_parser("user", help="User preference commands")
    user_actions = user_parser.add_subparsers(dest="memory_action", help="Action")

    ua = user_actions.add_parser("add", help="Add user preference")
    ua.add_argument("memory_args", help="title|content|keywords")

    ud = user_actions.add_parser("del", help="Delete user preference")
    ud.add_argument("memory_args", help="<id>")

    uu = user_actions.add_parser("update", help="Update user preference")
    uu.add_argument("memory_args", help="id|title|content|keywords")

    ul = user_actions.add_parser("list", help="List user preferences")
    ul.add_argument("memory_args", nargs="*", default=[], help="(ignored)")

    us = user_actions.add_parser("search", help="Search user preferences")
    us.add_argument("memory_args", help="<query>")

    # /memory proj add|del|update|list|search
    proj_parser = memory_subparsers.add_parser("proj", help="Project memory commands")
    proj_actions = proj_parser.add_subparsers(dest="memory_action", help="Action")

    pa = proj_actions.add_parser("add", help="Add project memory")
    pa.add_argument("memory_args", help="title|content|keywords")
    pa.add_argument("--project", default=None, help="Project path")

    pd = proj_actions.add_parser("del", help="Delete project memory")
    pd.add_argument("memory_args", help="<id>")

    pu = proj_actions.add_parser("update", help="Update project memory")
    pu.add_argument("memory_args", help="id|title|content|keywords")

    pl = proj_actions.add_parser("list", help="List project memories")
    pl.add_argument("memory_args", nargs="*", default=[], help="(ignored)")
    pl.add_argument("--project", default=None, help="Project path")

    ps = proj_actions.add_parser("search", help="Search project memories")
    ps.add_argument("memory_args", help="<query>")
    ps.add_argument("--project", default=None, help="Project path")

    # config
    config_parser = subparsers.add_parser("config", help="Manage model configurations")
    config_subparsers = config_parser.add_subparsers(dest="config_action", help="Action")
    config_subparsers.required = False  # 允许 `supercc config` 回车进入交互菜单

    ca_list = config_subparsers.add_parser("list", help="List all models")
    ca_list.add_argument("config_args", nargs="*", default=[], help="(ignored)")

    ca_add = config_subparsers.add_parser("add", help="Add a new model")
    ca_add.add_argument("--provider", help="预设供应商 ID（如 openrouter, anthropic）")
    ca_add.add_argument("config_args", nargs="*", default=[], help="<api_key> <model> [model_id] [name] [description]")

    ca_switch = config_subparsers.add_parser("switch", help="Switch to another model")
    ca_switch.add_argument("config_args", help="<model_id>")

    ca_delete = config_subparsers.add_parser("delete", help="Delete a model")
    ca_delete.add_argument("config_args", help="<model_id>")

    ca_providers = config_subparsers.add_parser("providers", help="List available model providers")

    # onboard
    onboard_parser = subparsers.add_parser("onboard", help="Interactive first-time setup")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View SuperCC logs")
    logs_parser.add_argument("--follow", action="store_true", help="Follow log output (Ctrl+C to exit)")
    logs_parser.add_argument("--tail", type=int, default=100, help="Number of recent lines to show (default: 100)")

    # gateway
    gateway_parser = subparsers.add_parser("gateway", help="Gateway management (后台常驻服务)")
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_action", help="Action")

    gw_install = gateway_subparsers.add_parser("install", help="Install gateway as a system service (开机自启动)")
    gw_start = gateway_subparsers.add_parser("start", help="Start gateway (auto-install if not installed)")
    gw_run = gateway_subparsers.add_parser("run", help="Run gateway in foreground (实时打印日志)")
    gw_stop = gateway_subparsers.add_parser("stop", help="Stop gateway")
    gw_status = gateway_subparsers.add_parser("status", help="Show gateway status")
    gw_uninstall = gateway_subparsers.add_parser("uninstall", help="Uninstall gateway and stop")

    # core-only
    core_only_parser = subparsers.add_parser("core-only", help="Start Core only (no channel plugins, internal use)")
    core_only_parser.add_argument("--config", required=True, help="Path to config.json")
    core_only_parser.add_argument("--data-dir", required=True, help="Path to data directory")

    # plugin
    plugin_parser = subparsers.add_parser("plugin", help="Manage plugin enable/disable/status")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_action", help="Action")
    pa_status = plugin_subparsers.add_parser("status", help="Show plugin status")
    pa_enable = plugin_subparsers.add_parser("enable", help="Enable a plugin")
    pa_enable.add_argument("plugin_name", help="Plugin name (feishu or wecom)")
    pa_disable = plugin_subparsers.add_parser("disable", help="Disable a plugin")
    pa_disable.add_argument("plugin_name", help="Plugin name (feishu or wecom)")

    args = parser.parse_args(args)

    # Print banner before any logging setup
    print_banner(_version)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # Python < 3.7
    _stdout_handler = _SafeStreamHandler(sys.stdout)
    _stdout_handler.setLevel(args.log_level)
    _stdout_handler.setFormatter(ColoredFormatter())
    logging.root.addHandler(_stdout_handler)
    logging.root.setLevel(args.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qrcode").setLevel(logging.WARNING)

    command = args.command

    if command == "list":
        list_bridges()
        return

    if command == "restart":
        from supercc.core.commands.restart_impl import run_restart_cli, RestartError as RestartErr
        try:
            for step in run_restart_cli(_active_lock):
                bar = "━" * (step.step - 1) + "▓" + "░" * (step.total - step.step)
                if step.status == "final":
                    print(f"\r[{bar}] ✓ {step.label} {step.detail}")
                else:
                    print(f"\r[{bar}] {step.label}...")
            print()
            import os as _os
            _os._exit(0)
        except RestartErr as e:
            print(f"\n❌ 重启失败: {e}")
            sys.exit(1)
        return

    if command == "update":
        from supercc.core.commands.restart_impl import run_update_cli, RestartError as UpdateErr
        try:
            for step in run_update_cli(_active_lock):
                bar = "━" * (step.step - 1) + "▓" + "░" * (step.total - step.step)
                if step.status == "skip":
                    print(f"✅ 当前版本 {step.detail} 已是最新")
                    return
                if step.status == "final":
                    print(f"\r[{bar}] ✓ {step.label} {step.detail}")
                else:
                    detail_str = f"  {step.detail}" if step.detail else ""
                    print(f"\r[{bar}] {step.label}...{detail_str}")
            print()
            import os as _os
            _os._exit(0)
        except UpdateErr as e:
            print(f"\n❌ 更新失败: {e}")
            sys.exit(1)
        return

    if command == "stop":
        # Read PID from current directory's .supercc/ directory
        try:
            _, data_dir = resolve_config_path()
        except Exception:
            print("当前目录未初始化，无法停止。")
            return
        pid_file = os.path.join(data_dir, "supercc.pid")
        if not os.path.exists(pid_file):
            print("当前目录无运行中的 SuperCC 实例。")
            return
        try:
            pid = int(Path(pid_file).read_text().strip())
        except (ValueError, OSError):
            print("PID 文件损坏，无法停止。")
            return
        stop_bridge(pid)
        return

    if command == "send":
        from supercc.main import run_send_command
        run_send_command(args.files, args.config)
        return

    if command == "memory":
        _run_memory_command(args)
        return

    if command == "config":
        _run_config_command(args)
        return

    if command == "logs":
        from supercc.logs import view_logs
        view_logs(follow=args.follow, tail=args.tail)
        return

    if command == "gateway":
        from supercc.gateway.cli import (
            run_gateway_install,
            run_gateway_start,
            run_gateway_stop,
            run_gateway_status,
            run_gateway_uninstall,
            run_gateway_run,
            run_gateway_restart,
        )
        action = getattr(args, "gateway_action", None)
        if action == "install":
            run_gateway_install()
        elif action == "start":
            run_gateway_start()
        elif action == "stop":
            run_gateway_stop()
        elif action == "status":
            run_gateway_status()
        elif action == "uninstall":
            run_gateway_uninstall()
        elif action == "run":
            try:
                run_gateway_run()
            except Exception as e:
                print(f"\n❌ Gateway run failed: {e}")
                sys.exit(1)
        elif action == "restart":
            run_gateway_restart()
        else:
            run_gateway_status()
        return

    if command == "core-only":
        cfg_path = args.config
        d_dir = args.data_dir
        init_config(cfg_path)
        start_core_only(cfg_path, d_dir)
        return

    if command == "onboard":
        from supercc.onboard import run_onboard_flow
        ok = run_onboard_flow()
        if ok:
            cfg_path, data_dir = resolve_config_path()
            init_config(cfg_path)
            asyncio.run(start_bridge(cfg_path, data_dir))
        return

    if command == "plugin":
        from supercc.config import load_config, write_config
        try:
            cfg_path, _ = resolve_config_path()
            init_config(cfg_path)
            cfg = load_config(cfg_path)
        except Exception:
            print("❌ 无法读取配置，请确保在项目目录下运行")
            return

        action = args.plugin_action
        plugin_name = getattr(args, "plugin_name", None)

        if action == "status":
            feishu = cfg.channels.feishu
            wecom = cfg.channels.wecom
            feishu_creds = "✅ 已配置" if feishu.app_id else "❌ 未配置"
            wecom_creds = "✅ 已配置" if wecom.corp_id else "❌ 未配置"
            print(f"飞书:      enabled={feishu.enabled}  {feishu_creds}")
            print(f"企业微信:  enabled={wecom.enabled}  {wecom_creds}")
            print()
            print("说明：修改 enabled 后需重启 SuperCC（supercc start）才能生效")
            return

        if plugin_name not in ("feishu", "wecom"):
            print(f"❌ 不支持的插件：{plugin_name}（支持：feishu, wecom）")
            return

        channel = getattr(cfg.channels, plugin_name, None)
        if not channel:
            print(f"❌ 未知错误：找不到 {plugin_name} 配置")
            return

        if action == "enable":
            if plugin_name == "feishu" and not channel.app_id:
                print("❌ 飞书未配置凭证（app_id 为空），无法启用。请先运行 onboard")
                return
            if plugin_name == "wecom" and not channel.corp_id:
                print("❌ 企业微信未配置凭证（corp_id 为空），无法启用。请先运行 onboard")
                return
            channel.enabled = True
            write_config(cfg)
            print(f"✅ {plugin_name} 已启用（重启后生效）")

        elif action == "disable":
            channel.enabled = False
            write_config(cfg)
            print(f"✅ {plugin_name} 已禁用（重启后生效）")
        return

    # Default: start (both `supercc` and `supercc start`)
    is_installed, yaml_exists = detect_config()
    if not is_installed:
        # Only run onboard if NEITHER config.json NOR config.yaml exists.
        # If config.json is empty but config.yaml has content, init_config will migrate.
        if yaml_exists:
            logger.info("Config.json is empty but config.yaml found — migrating...")
        else:
            logger.info("No config found, running onboard flow...")
            from supercc.onboard import run_onboard_flow
            ok = run_onboard_flow()
            if not ok:
                return
        cfg_path, data_dir = resolve_config_path()
    else:
        cfg_path, data_dir = resolve_config_path()

    init_config(cfg_path)

    config = get_config()

    # Risk warning must be acknowledged before starting (skip if already accepted in config)
    if config.bypass_accepted:
        logger.info("Bypass warning already accepted, skipping.")
    else:
        if not confirm_risk_warning(cfg_path):
            return
    log_file = os.path.join(data_dir, "supercc.log")
    Path(data_dir).mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file, mode="w")
    fh.setFormatter(PlainFormatter())
    logging.getLogger().addHandler(fh)
    write_log_banner(_version)
    logger.info("Starting SuperCC...")
    asyncio.run(start_bridge(cfg_path, data_dir))


if __name__ == "__main__":
    main()
