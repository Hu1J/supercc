# 单进程 Async 架构设计

**日期**: 2026-05-13
**状态**: 设计完成，待实现

## 背景

SuperCC 当前采用多进程架构：主进程通过 subprocess 启动 Feishu/WeCom 插件子进程，各进程独立运行、日志分散。

**核心痛点**：子进程日志难以统一，排查问题时日志分散、格式不统一、难以关联。

**设计决策**：保持 core + plugin 的模块化设计理念，但将 plugin 从独立 OS 进程改为同一进程内的 asyncio task（Hermes 风格）。

## 目标架构

```
main.py (单进程，单 asyncio loop)
├── Core WsServer (await ws_server.start())
├── CronScheduler (asyncio.create_task)
├── Feishu Plugin (asyncio.create_task) ← 崩溃自动重启，5s 间隔
└── WeCom Plugin (asyncio.create_task) ← 崩溃自动重启，5s 间隔
    ↓ plugin 通过 ws://127.0.0.1:port 连接 Core（thin-client 模式不变）
```

## 核心特性

### 日志统一
- 同一进程 + 同一 root logger = 日志自动统一
- 所有组件共享 `logging.root`，格式完全一致
- 日志写到 `supercc.log`，按时间顺序交织
- **无需改造日志格式**，是迁移到单进程的天然收益

### Plugin 崩溃自动重启
- 捕获 plugin task 的未捕获异常
- 记录日志
- 等待 5 秒后重新创建 task（固定延迟策略）
- 核心服务不受影响，其他 plugin 继续运行

### 有序退出
- SIGINT/SIGTERM 信号触发有序关闭
- 顺序：plugin → cron → core，各间隔 0.5s
- 允许各组件优雅关闭 WS 连接和清理资源

### CronScheduler 改为 Async Task
- `_run_job()` / `_tick()` 已是 `async def`，无需改动
- 去掉独立线程，直接在主 loop 创建 task
- `start()` / `stop()` 接口保持不变

## 关键约束

1. **Plugin __main__.py 保留** — 兼容独立进程部署（`python -m supercc.adapter.feishu`）
2. **WS thin-client 协议不变** — plugin 仍通过 `ws://127.0.0.1:port` 连接 Core
3. **PID 文件废弃** — 单进程不需要
4. **lark SDK 嵌套 loop** — 使用 `asyncio.to_thread(ws_client.start)` 隔离

## 改动范围

### main.py
- `start_bridge()`: 去掉 daemon thread + subprocess 逻辑，改为 `asyncio.run()`
- 插件: `asyncio.create_task(run_plugin(config, data_dir))`，复用现有协程
- Cleanup: 有序 cancel + 0.5s 延迟
- Plugin 重启: `while True` 循环，捕获异常 + 5s 延迟后重新 create_task

### cron_scheduler.py
- `CronScheduler`: 去掉 `start()` 中的独立线程，直接暴露 `_run()` 在主 loop

### adapter/feishu/__main__.py
- 无改动（`run_plugin()` 协程已存在）

### plugin/wecom/__main__.py
- 无改动（`run_plugin()` 协程已存在）

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| Plugin 崩溃 | 捕获异常 → 记录日志 → 5s 后重启 |
| Core WsServer 崩溃 | 整体进程退出（无法恢复） |
| CronScheduler 异常 | 记录日志，继续运行（不影响其他组件） |
| Plugin 重启循环过快 | 固定 5s 延迟，避免抖动 |

## 测试验证

1. **正常启动**: 所有 task 按顺序启动，WS 连接建立
2. **日志统一**: supercc.log 包含所有组件日志，格式一致
3. **Plugin 崩溃重启**: kill 掉某个 plugin task，验证自动拉起
4. **有序退出**: SIGTERM 触发，检查各组件关闭顺序和时间
5. **独立部署兼容**: `python -m supercc.adapter.feishu` 仍能正常运行
