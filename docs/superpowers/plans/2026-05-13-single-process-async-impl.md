# 单进程 Async 架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 main.py 的单进程 async 重构，plugin 崩溃自动重启，有序退出，CronScheduler 改为 async task

**Architecture:** core + plugin 共存单 asyncio loop，通过 `asyncio.create_task()` 管理各组件。Plugin 崩溃后在主 loop 内自动重启，无需子进程。日志天然统一到同一 root logger。

**Tech Stack:** Python asyncio, websockets

---

## 当前状态

`main.py` 已部分重构：
- `start_bridge` 已是 `async def`，用 `asyncio.run()` 调用
- Core WsServer 已移至主 loop
- Plugin 用 `asyncio.create_task(run_plugin())` 启动
- 已有 graceful shutdown 信号处理框架

**未完成的部分：**
1. Plugin 崩溃不重启（task 死了就死了）
2. Cleanup 无 0.5s 延迟
3. CronScheduler 仍在独立线程
4. 死代码未清理（`_spawn_plugin_process` 等）

---

## 文件清单

| 文件 | 状态 | 改动 |
|------|------|------|
| `supercc/main.py` | 已部分重构，待完成 | plugin 重启循环、cleanup 延迟、CronScheduler 改 async、清理死代码 |
| `supercc/cron_scheduler.py` | 待改动 | `start()` 去掉线程创建，暴露 `_run()` 供主 loop 调用 |
| `supercc/adapter/feishu/__main__.py` | 已完成 | 无需改动 |
| `supercc/plugin/wecom/__main__.py` | 已完成 | 无需改动 |

---

## Task 1: CronScheduler 改为 async task

**文件:**
- Modify: `supercc/cron_scheduler.py:896-951`

CronScheduler 当前 `start()` 创建一个独立线程 + 独立 event loop。改为：直接暴露 `_run()` 给主 loop 调用，`start()` / `stop()` 作为 thin wrapper。

- [ ] **Step 1: 阅读当前 CronScheduler 实现**

确认 `_run()`、`_tick()`、`stop()` 的当前实现。

- [ ] **Step 2: 修改 CronScheduler.start()**

```python
# supercc/cron_scheduler.py

class CronScheduler:
    def start(self):
        # 不再创建线程，直接标记为已启动
        # 主 loop 会通过 asyncio.create_task(scheduler._run()) 调度
        if self._thread is not None:
            return
        self._stop.clear()
        logger.info("CronScheduler started")

    def stop(self):
        """Safe to call from main loop or signal handler."""
        if self._thread is None:
            return
        self._stop.set()
        # 如果在独立线程中运行，等待线程结束
        if threading.current_thread() != self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._task = None
        logger.info("CronScheduler stopped")
```

- [ ] **Step 3: 修改 main.py 中调用方式**

在 `start_bridge()` 中，把 `cron_scheduler.start()` 改为：

```python
# Phase 4: Cron scheduler as async task
cron_scheduler = CronScheduler(config, data_dir)
set_cron_scheduler(cron_scheduler, config)
cron_task = asyncio.create_task(cron_scheduler._run(), name="cron-scheduler")
set_cron_scheduler(cron_scheduler, config)
```

- [ ] **Step 4: 验证启动**

确认无线程创建错误，CronScheduler tick 正常运行。

- [ ] **Step 5: Commit**

```bash
git add supercc/cron_scheduler.py supercc/main.py
git commit -m "refactor(cron): CronScheduler 改为 async task，去掉独立线程"
```

---

## Task 2: Plugin 重启循环（崩溃自动重启）

**文件:**
- Modify: `supercc/main.py:460-486`（当前 plugin task 创建部分）

当前 plugin task 只创建一次，崩溃后不重启。需要把每个 plugin task 包装在 `while True` 循环中，捕获异常后等待 5s 再重启。

- [ ] **Step 1: 阅读当前 plugin task 创建代码**

找到当前 `asyncio.create_task(run_feishu_plugin(...))` 和 `asyncio.create_task(run_wecom_plugin(...))` 的位置。

- [ ] **Step 2: 定义 plugin 重启辅助函数**

在 `start_bridge()` 中添加：

```python
async def _run_plugin_with_restart(name: str, config, data_dir, delay: float = 5.0):
    """运行一个 plugin task，崩溃后自动重启。"""
    while True:
        try:
            coro = _get_plugin_coro(name, config, data_dir)
            await coro
        except asyncio.CancelledError:
            raise  # 有序关闭时会被外层 cancel，不继续重启
        except Exception:
            logger.error(f"[{name}] plugin crashed, restarting in {delay}s\n%s",
                         traceback.format_exc())
            await asyncio.sleep(delay)
```

其中 `_get_plugin_coro` 根据 name 返回对应的 run_plugin 协程。

- [ ] **Step 3: 修改 plugin task 创建**

把直接 `create_task(run_plugin(...))` 改为：

```python
if _feishu_cfg and getattr(_feishu_cfg, "enabled", False) and getattr(_feishu_cfg, "app_id", ""):
    from supercc.adapter.feishu.__main__ import run_plugin as run_feishu_plugin
    task = asyncio.create_task(
        _run_plugin_with_restart("feishu", config, data_dir),
        name="feishu-plugin"
    )
    plugin_tasks.append(task)
    logger.info("[Bridge] Feishu plugin started (async task with auto-restart)")
```

同样处理 WeCom。

- [ ] **Step 4: 验证**

暂时让 plugin 抛异常，确认 5s 后自动拉起。

- [ ] **Step 5: Commit**

```bash
git add supercc/main.py
git commit -m "feat(bridge): plugin 崩溃自动重启，5s 间隔"
```

---

## Task 3: 有序退出（0.5s 延迟）

**文件:**
- Modify: `supercc/main.py:503-533`（当前 graceful shutdown finally 块）

当前 finally 块直接 cancel 所有 task，无延迟。需要改为：plugin → 0.5s → cron → 0.5s → core。

- [ ] **Step 1: 阅读当前 finally 块**

找到当前 `finally:` 块的内容。

- [ ] **Step 2: 修改 finally 块**

```python
finally:
    # 1. 取消 plugin tasks
    for task in plugin_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
    # 等待 0.5s 让 plugin 优雅关闭
    await asyncio.sleep(0.5)

    # 2. 取消 cron task
    if 'cron_task' in locals() and not cron_task.done():
        cron_task.cancel()
        try:
            await cron_task
        except asyncio.CancelledError:
            pass
    await asyncio.sleep(0.5)

    # 3. 关闭 core server
    if core_server:
        await core_server.stop()

    remove_pid(pid_file)
    lock.release()
    logger.info("SuperCC stopped gracefully")
```

- [ ] **Step 4: Commit**

```bash
git add supercc/main.py
git commit -m "feat(bridge): 有序退出，plugin→cron→core 各 0.5s 延迟"
```

---

## Task 4: 清理死代码

**文件:**
- Modify: `supercc/main.py:356-401`

删除不再使用的 `_plugin_pid_file`、`_kill_plugin_by_pid_file`、`_spawn_plugin_process` 三个函数。

- [ ] **Step 1: 确认三个函数无调用**

```bash
grep -n "_spawn_plugin_process\|_plugin_pid_file\|_kill_plugin_by_pid_file" supercc/main.py
```

预期只有函数定义，无调用。

- [ ] **Step 2: 删除三个函数**

删除 lines 356-401 的三个函数定义。

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "refactor(main): 删除废弃的 subprocess/PID 文件管理代码"
```

---

## Task 5: end-to-end 验证

- [ ] **Step 1: 正常启动**

运行 `python -m supercc`，确认所有组件启动：
- Core WsServer on port
- Feishu plugin connected to core
- WeCom plugin connected to core
- CronScheduler started

- [ ] **Step 2: 日志统一验证**

检查 supercc.log，确认所有组件日志格式一致、时间交织。

- [ ] **Step 3: Plugin 崩溃重启验证**

手动让 Feishu plugin 抛异常，确认 5s 后自动拉起，其他组件不受影响。

- [ ] **Step 4: 有序退出验证**

`kill -TERM <pid>`，确认日志中看到 plugin → cron → core 的有序关闭顺序。

---

## 验证检查清单

| 检查项 | 预期 |
|--------|------|
| 所有 task 正常启动 | 无异常，日志显示 connected to core |
| supercc.log 格式统一 | 所有组件日志格式一致 |
| Plugin 崩溃 | 5s 后自动拉起，日志有 crash + restart 记录 |
| 有序退出 | SIGTERM 后 plugin→cron→core 按序关闭 |
| 独立部署兼容 | `python -m supercc.adapter.feishu` 仍可用 |
