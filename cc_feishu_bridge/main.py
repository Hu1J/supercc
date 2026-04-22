"""CLI entry point — starts WebSocket long connection to Feishu.

Data is stored in .cc-feishu-bridge/ subdirectory of the current working directory,
enabling natural multi-instance isolation.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from cc_feishu_bridge.banner import print_banner, write_log_banner
import shutil
import signal
import sys
from pathlib import Path

CLAUDE_MD_CONTENT = """\
# 编码原则

## 四个原则详解

### 1. 编码前思考
不要假设。不要隐藏困惑。呈现权衡。

LLM 经常默默选择一种解释然后执行。这个原则强制明确推理：
- 明确说明假设 — 如果不确定，询问而不是猜测
- 呈现多种解释 — 当存在歧义时，不要默默选择
- 适时提出异议 — 如果存在更简单的方法，说出来
- 困惑时停下来 — 指出不清楚的地方并要求澄清

### 2. 简洁优先
用最少的代码解决问题。不要过度推测。

对抗过度工程的倾向：
- 不要添加要求之外的功能
- 不要为一次性代码创建抽象
- 不要添加未要求的"灵活性"或"可配置性"
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


def _ensure_claude_md(project_dir: str) -> None:
    """Ensure CLAUDE.md exists in project_dir; prepend content if it already exists."""
    claude_md = Path(project_dir) / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if CLAUDE_MD_CONTENT.strip() in existing:
            return  # already present
        claude_md.write_text(CLAUDE_MD_CONTENT + "\n" + existing, encoding="utf-8")
    else:
        claude_md.write_text(CLAUDE_MD_CONTENT, encoding="utf-8")

import filelock

_active_lock: "filelock.FileLock | None" = None

from cc_feishu_bridge.config import load_config, resolve_config_path
from cc_feishu_bridge.feishu.client import FeishuClient, IncomingMessage
from cc_feishu_bridge.feishu.ws_client import FeishuWSClient
from cc_feishu_bridge.feishu.message_handler import MessageHandler
from cc_feishu_bridge.feishu.error_notifier import setup as setup_error_notifier, update_chat_id as notifier_update_chat_id
from cc_feishu_bridge.security.auth import Authenticator
from cc_feishu_bridge.security.validator import SecurityValidator
from cc_feishu_bridge.claude.integration import ClaudeIntegration
from cc_feishu_bridge.claude.session_manager import SessionManager
from cc_feishu_bridge.format.reply_formatter import ReplyFormatter
from cc_feishu_bridge.cron_scheduler import CronScheduler
from cc_feishu_bridge.claude.cron_tools import set_cron_scheduler

logger = logging.getLogger(__name__)


def _register_skill_optimization_job(data_dir: str, scheduler) -> None:
    """Register a daily skill optimization scan job.

    Creates a cron job that delivers results to the active user's chat.
    """
    # Get chat_id from active session
    from cc_feishu_bridge.cron_scheduler import list_jobs, create_job
    chat_id = _get_active_chat_id(data_dir)
    if not chat_id:
        logger.info("[skill_optimize] no active chat_id, skipping")
        return

    # Idempotency: skip if a "Skill 优化扫描" job already exists
    existing = list_jobs(data_dir)
    if any(j.get("name") == "Skill 优化扫描" for j in existing):
        logger.info("[skill_optimize] job already registered, skipping")
        return

    prompt = """【Skill 优化扫描】

请扫描 {SKILLS_DIR}/ 目录下所有 Skill，分析哪些值得更新或新建。

**操作步骤：**
1. 先查看 {SKILLS_DIR}/ 目录下已有的 Skill
2. 把完整内容直接写入 {SKILLS_DIR}/<skill-name>/SKILL.md
3. 格式：YAML frontmatter (name/description/author/version) + Markdown body
4. {SKILLS_DIR}/ 本身是一个 Git 仓库。写入 SKILL.md 后，在 {SKILLS_DIR}/ 目录下执行 `git add <skill-name>/ && git commit -m "<中文 commit message>"`，commit message 必须用中文，清晰说明本次改动内容

请给出优化建议列表。"""

    try:
        create_job(
            prompt=prompt,
            schedule="0 3 * * *",  # 每天凌晨3点执行
            chat_id=chat_id,
            name="Skill 优化扫描",
            repeat=None,
            data_dir=data_dir,
            notify_at="0 9 * * *",  # 早上9点通知结果
        )
        logger.info("[skill_optimize] registered daily scan at 3am, notify at 9am")
    except Exception as e:
        logger.warning(f"[skill_optimize] failed to register: {e}")


def _get_active_chat_id(data_dir: str) -> str | None:
    """Get the most recent active session's chat_id."""
    db_path = os.path.join(data_dir, "sessions.db")
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT chat_id FROM sessions WHERE chat_id IS NOT NULL ORDER BY last_used DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["chat_id"] if row else None
    except Exception:
        return None


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


# ANSI color codes for terminal output
class ColoredFormatter(logging.Formatter):
    """Add ANSI color codes to log records based on level. Used for terminal only."""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def create_handler(config, data_dir: str, config_path: str | None = None) -> MessageHandler:
    """Create MessageHandler with all dependencies wired up."""
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
        data_dir=data_dir,
    )
    setup_error_notifier(feishu)
    authenticator = Authenticator(allowed_users=config.auth.allowed_users)
    validator = SecurityValidator(approved_directory=config.claude.approved_directory)
    claude = ClaudeIntegration(
        cli_path=config.claude.cli_path,
        max_turns=config.claude.max_turns,
        approved_directory=config.claude.approved_directory,
    )
    db_path = os.path.join(data_dir, "sessions.db")
    session_manager = SessionManager(db_path=db_path)
    formatter = ReplyFormatter()

    # Initialize Hermes-style skill nudge
    from cc_feishu_bridge.skill_nudge import make_nudge
    skill_nudge = make_nudge(config.skill_nudge)

    handler = MessageHandler(
        feishu_client=feishu,
        authenticator=authenticator,
        validator=validator,
        claude=claude,
        session_manager=session_manager,
        formatter=formatter,
        approved_directory=config.claude.approved_directory,
        config=config,
        data_dir=data_dir,
        feishu_groups=config.feishu.groups,
        config_path=config_path,
        skill_nudge=skill_nudge,
    )
    return handler


async def handle_message(message: IncomingMessage, handler: MessageHandler) -> None:
    """Callback for incoming Feishu messages — dispatch to handler."""
    # Keep error notifier's chat_id fresh for error reporting
    notifier_update_chat_id(message.chat_id)
    # Store raw message for memory enhancement
    session = None
    if message.user_open_id:
        session = handler.sessions.get_active_session(message.user_open_id)
        if session:
            handler.sessions.update_session(session.session_id, update_last_message=True)
            handler.sessions.store_message(
                message_id=message.message_id,
                session_id=session.session_id,
                chat_id=message.chat_id,
                user_open_id=message.user_open_id,
                message_type=message.message_type,
                raw_content=message.raw_content,
                content=message.content,
                direction="incoming",
            )
    try:
        await handler.handle(message)
    except Exception as e:
        logger.exception(f"Error handling message: {e}")


def ensure_skill_installed() -> None:
    """Install or update bundled skills to ~/.claude/skills/.

    Idempotent: skips individual skills if version matches.
    Skill content is bundled inside the package so this works via pip or PyInstaller.
    """
    import os

    skills = [
        ("cc_feishu_bridge.skill_md", "SKILL_MD", "SKILL_NAME", "SKILL_VERSION"),
    ]

    for module_path, md_attr, name_attr, ver_attr in skills:
        try:
            mod = __import__(module_path, fromlist=[md_attr, name_attr, ver_attr])
            skill_md = getattr(mod, md_attr)
            skill_name = getattr(mod, name_attr)
            skill_version = getattr(mod, ver_attr)
        except Exception:
            logger.warning(f"Could not load skill from {module_path}, skipping.")
            continue

        dest_dir = os.path.expanduser(f"~/.claude/skills/{skill_name}")
        dest_path = os.path.join(dest_dir, "skill.md")
        version_marker = os.path.join(dest_dir, ".version")

        current_version = ""
        if os.path.exists(version_marker):
            current_version = open(version_marker).read().strip()
        if current_version == skill_version:
            logger.info(f"Skill {skill_name} v{skill_version} already installed, skipping.")
            continue

        os.makedirs(dest_dir, exist_ok=True)
        open(dest_path, "w", encoding="utf-8").write(skill_md)
        open(version_marker, "w").write(skill_version)
        logger.info(f"Installed skill {skill_name} v{skill_version} to {dest_dir}")


def write_pid(pid_file: str) -> None:
    """Write current PID to file."""
    Path(pid_file).write_text(str(os.getpid()))


def remove_pid(pid_file: str) -> None:
    """Remove PID file."""
    Path(pid_file).unlink(missing_ok=True)


RISK_WARNING = """
⚠️  安全风险警告 / Security Risk Warning
==============================================================

cc-feishu-bridge 以 bypassPermissions 模式运行。
Claude Code 可以执行任意终端命令、读写本地文件，无需每次授权确认。

这意味着如果有人通过飞书向机器人发送恶意指令，攻击者可以：
  • 在你的电脑上执行任意命令
  • 读取、修改或删除你的本地文件
  • 访问你的敏感信息

请仅在可信任的网络环境下使用本工具。

cc-feishu-bridge runs in bypassPermissions mode.
Claude Code can execute arbitrary terminal commands and read/write local files
without asking for permission each time.

Do you understand and accept these risks? (yes/no): """


def confirm_risk_warning(config_path: str) -> bool:
    """Show risk warning and get user confirmation. Saves acceptance to config on 'yes'."""
    from cc_feishu_bridge.config import accept_bypass_warning
    print(RISK_WARNING)
    while True:
        try:
            response = input().strip().lower()
            if response in ("yes", "y"):
                accept_bypass_warning(config_path)
                print("已记录，下次启动将不再提示。")
                return True
            elif response in ("no", "n", ""):
                print("Cancelled — not starting the bridge.")
                return False
            else:
                print("Please enter 'yes' or 'no': ", end="")
        except EOFError:
            print("no (EOF)")
            return False


def start_bridge(config_path: str, data_dir: str) -> None:
    """Start the bridge: load config and run WebSocket connection."""
    # Acquire exclusive lock before starting — prevents multiple instances in the same directory
    lock_file = os.path.join(data_dir, ".instance.lock")
    lock = filelock.FileLock(lock_file, timeout=1)
    global _active_lock
    _active_lock = lock
    try:
        lock.acquire()
    except filelock.Timeout:
        print(f"错误：当前目录下已有一个 cc-feishu-bridge 实例正在运行 ({data_dir})")
        print("如果确认没有实例在运行，请删除 .instance.lock 文件后重试。")
        sys.exit(1)

    config = load_config(config_path)
    handler = create_handler(config, data_dir, config_path=config_path)
    _ensure_claude_md(config.claude.approved_directory)

    ws_client = FeishuWSClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
        bot_open_id=config.feishu.bot_open_id,
        domain=config.feishu.domain,
        on_message=lambda msg: handle_message(msg, handler),
    )

    # Write PID file for process management
    pid_file = os.path.join(data_dir, "cc-feishu-bridge.pid")
    write_pid(pid_file)

    # Clean up PID file and lock on exit
    cron_scheduler = None
    def cleanup(signum, frame):
        cron_scheduler.stop()
        remove_pid(pid_file)
        lock.release()
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    logger.info(f"Starting Feishu bridge (WS mode) — data: {data_dir}")

    # Auto-install Claude skill for file sending
    ensure_skill_installed()

    # Create media subdirectories
    for sub in ("received_images", "received_files"):
        sub_dir = os.path.join(data_dir, sub)
        os.makedirs(sub_dir, exist_ok=True)

    # Start cron scheduler (定时任务后台调度器)
    cron_scheduler = CronScheduler(config, data_dir)
    set_cron_scheduler(cron_scheduler, config)
    cron_scheduler.start()

    # Ensure skills directory is a git repo (init if needed)
    from cc_feishu_bridge.skill_nudge import _ensure_skills_git_repo
    _ensure_skills_git_repo(Path(data_dir) / "skills")

    # Register daily skill optimization scan
    _register_skill_optimization_job(data_dir, cron_scheduler)

    # Register nightly dream job (memory refinement at 3am)
    from cc_feishu_bridge.dream import register_dream_job
    register_dream_job(data_dir)

    # CLI 进程在第一条消息到达时才会建立连接（_ensure_connected 懒加载）。
    # SDK 通过 continue_conversation=True 自动维护 session，无需手动 fork。
    ws_client.start()


def list_bridges() -> None:
    """List all cc-feishu-bridge instances by scanning .cc-feishu-bridge/*.pid files."""
    print("\nRunning cc-feishu-bridge instances:")
    print(f"{'PID':<8} {'Directory':<40} {'PID File':<50}")
    print("-" * 100)

    found = False
    for root, dirs, files in os.walk("."):
        # Only look in .cc-feishu-bridge directories
        if ".cc-feishu-bridge" not in dirs:
            continue
        cc_dir = os.path.join(root, ".cc-feishu-bridge")
        pid_file = os.path.join(cc_dir, "cc-feishu-bridge.pid")
        if not os.path.exists(pid_file):
            continue
        try:
            pid = int(Path(pid_file).read_text().strip())
            # Check if process is alive
            try:
                os.kill(pid, 0)
                status = "running"
            except OSError:
                status = "dead (clean up pid file)"
            print(f"{pid:<8} {os.path.abspath(root):<40} {pid_file:<50} {status}")
            found = True
        except (ValueError, OSError):
            pass

    if not found:
        print("No running instances found.")
    print()


def stop_bridge(pid: int) -> None:
    """Stop a cc-feishu-bridge instance by PID."""
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped PID {pid}")
    except OSError as e:
        print(f"Failed to stop PID {pid}: {e}")


def detect_config() -> bool:
    """Check if .cc-feishu-bridge/config.yaml exists and is non-empty."""
    cfg, _ = resolve_config_path()
    p = Path(cfg)
    return p.exists() and p.stat().st_size > 0


async def interactive_install() -> tuple[str, str]:
    """Run the QR-code install flow. Returns (cfg_path, data_dir) on success."""
    from cc_feishu_bridge.install.flow import run_install_flow
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
    from cc_feishu_bridge.config import load_config
    config = load_config(config_path)

    # 2. Locate sessions.db (same directory as config)
    data_dir = str(Path(config_path).parent.resolve())
    db_path = os.path.join(data_dir, "sessions.db")
    if not os.path.exists(db_path):
        print("Error: sessions.db not found. Has the bridge ever been run?")
        return

    # 3. Find the most recently active session's chat_id
    from cc_feishu_bridge.claude.session_manager import SessionManager
    sm = SessionManager(db_path=db_path)
    session = sm.get_active_session_by_chat_id()
    if not session or not session.chat_id:
        print("Error: no active chat session found. Make sure the bridge has been used.")
        return
    chat_id = session.chat_id
    print(f"Sending to chat: {chat_id}")

    # 4. Create FeishuClient
    from cc_feishu_bridge.feishu.client import FeishuClient
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
    )

    # 5. Process each file
    import asyncio
    try:
        from cc_feishu_bridge.feishu.media import guess_file_type
    except ImportError:
        guess_file_type = None

    async def send_one(file_path: str) -> str:
        """Send a single file. Raises on error so gather() can collect it."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE:
            raise ValueError(f"{file_path} exceeds 30MB limit")

        with open(file_path, "rb") as f:
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
    """Handle cc-feishu-bridge memory <scope> <action> [args]."""
    from cc_feishu_bridge.claude.memory_manager import get_memory_manager

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

    # Try to send results to Feishu if we're in a bridge session
    feishu_client = None
    feishu_chat_id = None
    try:
        cfg_path, data_dir = resolve_config_path()
        config = load_config(cfg_path)
        from cc_feishu_bridge.feishu.client import FeishuClient
        from cc_feishu_bridge.claude.session_manager import SessionManager
        feishu_client = FeishuClient(
            app_id=config.feishu.app_id,
            app_secret=config.feishu.app_secret,
        )
        sm = SessionManager(db_path=os.path.join(data_dir, "sessions.db"))
        session = sm.get_active_session_by_chat_id()
        feishu_chat_id = session.chat_id if session and session.chat_id else None
    except Exception:
        pass  # Not in a bridge session, skip Feishu push

    async def _send_feishu(text: str):
        if feishu_client and feishu_chat_id:
            await feishu_client.send_text(feishu_chat_id, text)

    def _print(text: str):
        print(text)
        asyncio.run(_send_feishu(text))

    def _parse_args(args_str: str) -> list[str]:
        """Split by pipe to get title/content/keywords or id/title/content/keywords."""
        return [p.strip() for p in args_str.split("|")]

    # ── user ────────────────────────────────────────────────────────────────
    if scope == "user":
        if action == "add":
            parts = _parse_args(raw_args)
            if len(parts) < 3:
                _print("用法: cc-feishu-bridge memory user add <title>|<content>|<keywords>")
                return
            title, content, keywords = parts[0], parts[1], parts[2]
            p = mm.add_preference(title, content, keywords)
            _print(f"✅ 用户偏好已保存 (id={p.id})")

        elif action == "del":
            if not raw_args.strip():
                _print("用法: cc-feishu-bridge memory user del <id>")
                return
            ok = mm.delete_preference(raw_args)
            if ok:
                _print(f"🗑️ 用户偏好 {raw_args} 已删除。")
            else:
                _print(f"未找到 id={raw_args} 的用户偏好")

        elif action == "update":
            parts = _parse_args(raw_args)
            if len(parts) < 4:
                _print("用法: cc-feishu-bridge memory user update <id>|<title>|<content>|<keywords>")
                return
            pref_id, title, content, keywords = parts[0], parts[1], parts[2], parts[3]
            ok = mm.update_preference(pref_id, title, content, keywords)
            if ok:
                _print(f"✅ 用户偏好 {pref_id} 已更新")
            else:
                _print(f"未找到 id={pref_id} 的用户偏好")

        elif action == "list":
            prefs = mm.get_all_preferences()
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
                _print("用法: cc-feishu-bridge memory user search <关键词>")
                return
            results = mm.search_preferences(raw_args)
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
        project_path = args.project or ""

        if action == "add":
            parts = _parse_args(raw_args)
            if len(parts) < 3:
                _print("用法: cc-feishu-bridge memory proj add <title>|<content>|<keywords>")
                return
            title, content, keywords = parts[0], parts[1], parts[2]
            m = mm.add_project_memory(project_path, title, content, keywords)
            _print(f"✅ 项目记忆已保存 (id={m.id})")

        elif action == "del":
            if not raw_args.strip():
                _print("用法: cc-feishu-bridge memory proj del <id>")
                return
            ok = mm.delete_project_memory(raw_args)
            if ok:
                _print(f"🗑️ 项目记忆 {raw_args} 已删除。")
            else:
                _print(f"未找到 id={raw_args} 的项目记忆")

        elif action == "update":
            parts = _parse_args(raw_args)
            if len(parts) < 4:
                _print("用法: cc-feishu-bridge memory proj update <id>|<title>|<content>|<keywords>")
                return
            mem_id, title, content, keywords = parts[0], parts[1], parts[2], parts[3]
            ok = mm.update_project_memory(mem_id, title, content, keywords)
            if ok:
                _print(f"✅ 项目记忆 {mem_id} 已更新")
            else:
                _print(f"未找到 id={mem_id} 的项目记忆")

        elif action == "list":
            mems = mm.get_project_memories(project_path)
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
                _print("用法: cc-feishu-bridge memory proj search <关键词>")
                return
            results = mm.search_project_memories(raw_args, project_path)
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


def main(args=None):
    # Read version once — shared by --version flag and startup banner
    try:
        from importlib.metadata import version as _get_version

        _version = _get_version("cc-feishu-bridge")
    except Exception:
        _version = "dev"

    parser = argparse.ArgumentParser(
        description="Claude Code Feishu Bridge — data stored in .cc-feishu-bridge/"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"cc-feishu-bridge {_version}",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # start (default)
    start_parser = subparsers.add_parser("start", help="Start the bridge (default)")

    # list
    list_parser = subparsers.add_parser("list", help="List all running instances")

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop the bridge instance in the current directory")

    restart_parser = subparsers.add_parser("restart", help="Restart current bridge instance")
    update_parser = subparsers.add_parser("update", help="Check for updates and restart if needed")

    # send
    send_parser = subparsers.add_parser("send", help="Send a file or image to the active Feishu chat")
    send_parser.add_argument("files", nargs="+", help="Path(s) to the file(s) to send")
    send_parser.add_argument("--config", required=True, help="Path to config.yaml for this bridge instance")

    switch_parser = subparsers.add_parser(
        "switch",
        help="Switch to another project's bridge instance",
    )
    switch_parser.add_argument(
        "target",
        help="Target project directory (absolute or relative path)",
    )

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

    args = parser.parse_args(args)

    # Print banner before any logging setup
    print_banner(_version)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # Python < 3.7
    _stdout_handler = _SafeStreamHandler(sys.stdout)
    _stdout_handler.setLevel(args.log_level)
    _stdout_handler.setFormatter(ColoredFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.root.addHandler(_stdout_handler)
    logging.root.setLevel(args.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qrcode").setLevel(logging.WARNING)

    command = args.command

    if command == "list":
        list_bridges()
        return

    if command == "restart":
        from cc_feishu_bridge.restarter import run_restart_cli, RestartError as RestartErr
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
        from cc_feishu_bridge.restarter import run_update_cli, RestartError as UpdateErr
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
        # Read PID from current directory's .cc-feishu-bridge/ directory
        try:
            _, data_dir = resolve_config_path()
        except Exception:
            print("当前目录未初始化，无法停止。")
            return
        pid_file = os.path.join(data_dir, "cc-feishu-bridge.pid")
        if not os.path.exists(pid_file):
            print("当前目录无运行中的 bridge 实例。")
            return
        try:
            pid = int(Path(pid_file).read_text().strip())
        except (ValueError, OSError):
            print("PID 文件损坏，无法停止。")
            return
        stop_bridge(pid)
        return

    if command == "send":
        from cc_feishu_bridge.main import run_send_command
        run_send_command(args.files, args.config)
        return

    if command == "switch":
        from cc_feishu_bridge.switcher import SwitchError, run_switch_cli
        target = os.path.abspath(args.target)

        # Try to load current project's config + Feishu client for notifications
        feishu = None
        chat_id = None
        try:
            cfg_path, data_dir = resolve_config_path()
            config = load_config(cfg_path)
            db_path = os.path.join(data_dir, "sessions.db")

            from cc_feishu_bridge.feishu.client import FeishuClient
            from cc_feishu_bridge.claude.session_manager import SessionManager
            feishu = FeishuClient(
                app_id=config.feishu.app_id,
                app_secret=config.feishu.app_secret,
            )
            sm = SessionManager(db_path=db_path)
            session = sm.get_active_session_by_chat_id()
            chat_id = session.chat_id if session and session.chat_id else None
        except Exception:
            pass  # Feishu not available, proceed without notifications

        try:
            for step in run_switch_cli(target, feishu=feishu, chat_id=chat_id):
                bar = "━" * (step.step - 1) + "▓" + "░" * (step.total - step.step)
                if step.status == "final":
                    print(f"\r[{bar}] ✓ {step.label} {step.detail}")
                else:
                    print(f"\r[{bar}] {step.label}...")
            print()
        except SwitchError as e:
            print(f"\n❌ 切换失败: {e}")
            sys.exit(1)
        return

    if command == "memory":
        _run_memory_command(args)
        return

    # Default: start
    is_installed = detect_config()
    if not is_installed:
        logger.info("No config found, running install flow...")
        cfg_path, data_dir = asyncio.run(interactive_install())
    else:
        cfg_path, data_dir = resolve_config_path()

    # Risk warning must be acknowledged before starting (skip if already accepted)
    if is_installed:
        from cc_feishu_bridge.config import load_config
        config = load_config(cfg_path)
        if config.bypass_accepted:
            logger.info("Bypass warning already accepted, skipping.")
        else:
            if not confirm_risk_warning(cfg_path):
                return
    else:
        if not confirm_risk_warning(cfg_path):
            return

    # Set up logging to file
    log_file = os.path.join(data_dir, "cc-feishu-bridge.log")
    Path(data_dir).mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    write_log_banner(log_file, _version)
    if is_installed:
        logger.info(f"Config found, starting bridge...")
    else:
        logger.info("Install complete, starting bridge...")
    start_bridge(cfg_path, data_dir)


if __name__ == "__main__":
    main()
