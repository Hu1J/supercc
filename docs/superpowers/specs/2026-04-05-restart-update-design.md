# restart / update 功能设计

## 背景

当前 `cc-feishu-bridge` 支持 `start` / `stop` / `switch` 等子命令，但缺少 restart（重启当前 bridge）和 update（检查并更新到最新版本）功能。同时需要支持飞书消息指令 `/restart` 和 `/update`。

## restart

### 行为

重启当前目录下的 bridge 实例。所有通知卡片在旧进程断开飞书连接前全部发完。

### 步骤（generator: `restart_to()`）

| Step | Label | 说明 |
|------|-------|------|
| 1 | 准备重启 | 发卡片 |
| 2 | 启动新 bridge | fork 新进程，新进程拿到 FileLock，正常启动 |
| 3 | 等待新进程就绪 | 确认新进程 pid file 存在 |
| 4 | 重启完成 | 发完成卡片 |
| 5 | 旧进程退出 | `os._exit(0)` 直接退出，不触发 cleanup handler |

**关键实现细节：**

1. **FileLock 处理**：在启动新进程前，主动调用 `lock.release()`，让新进程能够获取锁。新进程正常启动后会创建自己的 FileLock 实例。
2. **PID 文件隔离**：不走 cleanup handler，直接 `os._exit(0)` 退出，不读写 pid 文件。新进程启动时会覆盖 pid 文件，旧的 pid 文件内容对新进程无影响。
3. **卡片发送时机**：所有卡片通过旧进程的 Feishu WS 连接发送，在 `os._exit(0)` 之前完成。

### 飞书消息指令 `/restart`

- 调用 `run_restart()`（与 `run_switch()` 结构一致）
- 传入 `feishu`、`chat_id`、`reply_to_message_id`
- 每步发送一张进度卡片，最后发完成卡片

### CLI 命令 `cc-feishu-bridge restart`

- 调用 `run_restart_cli()`（与 `run_switch_cli()` 结构一致）
- 传入 `feishu`、`chat_id`（可为空）
- CLI 端打印进度条，飞书连接正常时同步发送卡片

## update

### 行为

检查 PyPI 最新版本，如有更新则执行升级并自动调用 restart 重启。

### 步骤（`do_update()`）

| Step | Label | 说明 |
|------|-------|------|
| 1 | 📋 检查更新 | 调用 `check_version()` 获取 PyPI 最新版本，对比当前版本 |
| 2a | ⬇️ 下载新版本 | 当前版本 < PyPI 版本，执行 pip install -U |
| 2b | ✅ 已是最新 | 当前版本 >= PyPI 版本，发提示卡，流程结束 |
| 3 | ✅ 下载完成 | pip install 成功 |
| 4 | 🔄 准备重启 | 复用 restart 步骤 1 |
| 5 | 🚀 启动新 bridge | 复用 restart 步骤 2-3 |
| 6 | ✅ 重启完成 | 复用 restart 步骤 4 |
| 7 | 旧进程退出 | `os._exit(0)` |

### 版本检查（`check_version()`）

- 使用 `pip index versions cc-feishu-bridge` 或解析 PyPI JSON API 获取最新版本
- 使用 `packaging.version` 或字符串比较判断是否需要更新
- 异常时降级为静默不更新

### 飞书消息指令 `/update`

- 调用 `run_update()`（与 `run_restart()` 结构一致）
- 传入 `feishu`、`chat_id`、`reply_to_message_id`
- 已在最新时发提示卡；有更新时按步骤发卡片

### CLI 命令 `cc-feishu-bridge update`

- 调用 `run_update_cli()`（与 `run_restart_cli()` 结构一致）
- CLI 端打印进度文本，飞书连接正常时同步发送卡片

## 代码组织

```
cc_feishu_bridge/
  restarter.py          # 新建，与 switcher.py 完全对称
    RestartStep          # dataclass，同 SwitchStep 结构
    RestartResult        # dataclass
    RestartError         # 异常基类
    StartupTimeoutError  # 异常
    CurrentStopError     # 异常

    _pid_file_path()
    _is_process_alive()
    _kill_process()
    _stop_bridge()
    _start_bridge()
    _restart_to()              # generator，yield RestartStep
    run_restart()             # 飞书消息用（async）
    run_restart_cli()         # CLI 用（sync generator）

    check_version()           # 返回 (current_ver, latest_ver)
    _do_update()              # generator，yield UpdateStep
    run_update()              # 飞书消息用（async）
    run_update_cli()          # CLI 用（sync generator）

  main.py               # 新增 restart / update 子命令
  feishu/message_handler.py  # 新增 /restart / /update 指令处理
```

## 飞书卡片格式

与现有 `run_switch()` 完全一致的格式风格：

```markdown
## 🔄 正在重启

**当前目录**: `/path/to/project`

▓░░░ `1/4` 🛑 准备重启

⏳ 即将重启 bridge，请稍候...
```

```markdown
## ✅ 重启完成

**当前目录**: `/path/to/project`
**新进程 PID**: `12345`

🎉 Bridge 已重启，可以在飞书中继续对话了。
```

## 错误处理

- **新进程启动超时**：`StartupTimeoutError` → 发错误卡片，进程不退出
- **pip install 失败**：捕获异常 → 发错误卡片，进程不退出
- **FileLock 释放失败**：理论上不会发生，一旦发生 → 发错误卡片，进程不退出
- **PyPI 请求失败**（网络问题）：静默跳过 update，进程正常继续

## 向后兼容

- 普通 `cc-feishu-bridge stop` / SIGINT / SIGTERM：保持原有 pid 文件删除逻辑，不受 restart 影响
- restart 中 `os._exit(0)` 直接终止进程，不执行 cleanup handler 中的 `remove_pid()`
