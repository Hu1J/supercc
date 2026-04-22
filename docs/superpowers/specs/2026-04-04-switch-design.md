# Project Switch 设计文档

## 背景与目标

当前 `cc-feishu-bridge` 以工作目录为隔离边界，每个项目目录下独立运行一个 bridge 实例。用户在飞书里只能与当前目录下运行的那个 bridge 对话，无法切换到其他项目继续讨论。

本文档定义 `/switch` 命令，允许用户在不中断对话记录的情况下，切换到另一个项目的 bridge 实例继续工作。

## 设计原则

- **数据隔离不变**：每个 bridge 实例的 config.yaml 和 sessions.db 仍然完全隔离，不因切换而混淆。
- **流程透明**：切换过程中每一步都有日志/通知，用户清楚发生了什么。
- **最小侵入**：不改动 bridge 核心业务逻辑，新增模块独立实现。

---

## 核心概念

### 初始化判定

`config.yaml` 存在且包含有效的 `feishu.app_id` + `feishu.app_secret` 时，视为该项目已完成初始化。未初始化的项目无法作为切换目标。

### 切换目标

`/switch <路径>` 支持绝对路径和相对路径（相对于当前工作目录）。

---

## 实现方案

### 1. Python 模块：`switcher.py`

独立模块，负责切换的核心逻辑，供 CLI 和 skill 代码共同调用。

#### 公开函数

```python
def switch_to(target_path: str) -> SwitchResult:
    """执行完整的项目切换流程。返回切换结果和消息。"""
```

#### 执行流程（顺序执行，失败则中止并报告）

**Step 1：目标路径初始化检查**
- 检查 `<target_path>/.cc-feishu-bridge/config.yaml` 是否存在且凭证有效
- 若未初始化：抛出 `NotInitializedError`，提示用户先运行 `cc-feishu-bridge install`

**Step 2：停止目标路径 bridge（若在运行）**
- 读取 `<target_path>/.cc-feishu-bridge/cc-feishu-bridge.pid`
- 若进程存在则发送 SIGTERM，验证进程退出
- 若超时未退出，发送 SIGKILL

**Step 3：拷贝并修正 config.yaml**
- 读取当前 `config.yaml`
- 将 `storage.db_path` 替换为目标路径下的绝对路径：
  - 原：`/当前项目/.cc-feishu-bridge/sessions.db`
  - 新：`/目标项目/.cc-feishu-bridge/sessions.db`
- 其余字段（feishu 凭证、auth、claude 等）原样拷贝
- 写入 `<target_path>/.cc-feishu-bridge/config.yaml`

**Step 4：在目标路径启动 bridge**
- `subprocess.Popen` 启动新 bridge 子进程，工作目录切到 target_path
- 等待 3 秒，验证 pid 文件已创建且进程存活

**Step 5：停止当前 bridge**
- 读取当前 `<cwd>/.cc-feishu-bridge/cc-feishu-bridge.pid`
- 发送 SIGTERM，验证进程退出

**Step 6：报告结果**
- 若所有步骤成功：返回成功状态和新 bridge 的 PID
- 若中间步骤失败：返回失败状态 + 失败步骤描述，已执行的部分不做回滚

#### 错误类型

| 错误类型 | 触发条件 |
|---|---|
| `NotInitializedError` | 目标路径未初始化 |
| `TargetAlreadyRunningError` | 目标 bridge 无法被停止（持续运行） |
| `StartupTimeoutError` | 目标 bridge 启动后未能存活 |
| `CurrentBridgeStopError` | 当前 bridge 无法被停止 |

---

### 2. 飞书内置命令处理

`/switch` 作为内置命令在消息解析层处理，与现有 `/git` 命令同一机制，无需新增 skill 文件。

用户在飞书发送 `/switch <目标路径>` → 消息解析拦截 → 调用 `switcher.switch_to()` → 发送结果卡片。

---

### 3. CLI 子命令

`cc-feishu-bridge switch <目标路径>` 底层调用 `switcher.switch_to()`，将标准输出用于进度展示。

```
$ cc-feishu-bridge switch ../my-project
[1/5] 正在检查目标项目是否已初始化...          ✓
[2/5] 正在停止目标项目的 bridge（若在运行）...  ✓
[3/5] 正在拷贝并修正配置文件...                 ✓
[4/5] 正在启动目标项目 bridge...                 ✓
[5/5] 正在停止当前 bridge...                    ✓
切换完成。当前 bridge 已停止，请前往目标项目继续。
```

---

### 4. 飞书消息卡片

切换成功后，当前 bridge 向用户发送一张飞书卡片（Markdown）：

```
## ✅ 已切换到新项目

- **目标项目**：`/path/to/target`
- **新 Bridge PID**：`12345`
- **数据库**：`/path/to/target/.cc-feishu-bridge/sessions.db`

> 飞书消息流已切换，请在新项目下继续对话。
> 返回时执行 `/switch /path/to/current` 即可。
```

切换失败时，发送错误详情卡片，不影响当前 bridge 运行状态。

---

## 文件变更

| 操作 | 路径 |
|---|---|
| 新增 | `cc_feishu_bridge/switcher.py` — 切换核心逻辑 |
| 修改 | `cc_feishu_bridge/main.py` — 注册 `switch` 子命令和飞书消息解析拦截 |

---

## 测试策略

1. **单元测试**：Mock 文件系统和 subprocess，验证各步骤调用顺序正确
2. **集成测试**（手动）：
   - 场景 A：目标未初始化 → 应报 `NotInitializedError`
   - 场景 B：目标在跑 → 应成功停掉再启动
   - 场景 C：全流程正常切换 → 两个 bridge 均正常停止/启动
   - 场景 D：当前 bridge 无法停止 → 应回滚失败报告，不影响目标 bridge
