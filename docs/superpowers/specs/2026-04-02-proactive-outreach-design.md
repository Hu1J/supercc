# 主动推送功能设计

## 背景

cc-feishu-bridge 目前是纯被动触发的：用户发消息，bridge 响应。为了让 bridge 更主动，增加主动推送功能——在用户沉默超过一定时间后，bridge 主动发消息找用户。

## 目标

- 用户超过 N 分钟没发消息，且当前在时间窗口内，bridge 主动发消息
- 内容由 Claude Code 自己分析项目状况生成，永远以项目为导向
- 不打断 bridge 主流程，不影响用户正常对话

## 核心流程

```
定时器触发（每 N 分钟）
    → 遍历所有有记录的用户
    → 检查：时间窗口内？沉默超阈值？今天已发过？
    → 满足条件
        → 调用 Claude Code，prompt：分析项目状况，告知下一步该做啥
        → 把 Claude 回复主动发到飞书
```

本质上就是一次模拟用户发消息的调用——触发源从 WS 事件变成定时器，链路完全复用。

## 主动消息 prompt

```
分析 {approved_directory} 项目：
- 当前状况和进展（git log / 文件变更）
- 下一步应该做什么

给用户一段简短汇报（200字以内），让他知道项目在哪、下一步往哪走。
语气自然，像同事之间的日常交流。
```

## 配置项

在 `config.yaml` 中新增：

```yaml
proactive:
  enabled: true
  time_window_start: "08:00"
  time_window_end: "22:00"
  silence_threshold_minutes: 60
  check_interval_minutes: 5
  max_per_day: 3        # 每天最多推送次数，0 或 false 表示不限次数
```

## 数据变更

`sessions.db` 的 `sessions` 表新增字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `last_message_at` | TIMESTAMP | 用户最后发消息的时间 |
| `proactive_sent_today` | DATE | 最后一次主动推送的日期（每天清零） |
| `proactive_today_count` | INT | 当天已主动推送次数 |

## 新增文件

| 文件 | 职责 |
|------|------|
| `cc_feishu_bridge/proactive_scheduler.py` | 定时调度器，检查条件并触发主动推送 |

## 改动文件

| 文件 | 改动 |
|------|------|
| `cc_feishu_bridge/config.py` | 新增 `ProactiveConfig` 数据类 |
| `cc_feishu_bridge/session_manager.py` | 新增字段，更新 `update_session` |
| `cc_feishu_bridge/main.py` | WS 连接前启动 Scheduler |
| `cc_feishu_bridge/feishu/client.py` | 新增 `send_proactive_message()` 方法（不带 reply threading） |

## 关键决策

1. **不走 worker 队列**：主动推送是独立触发的，直接调 Feishu API 发消息，不经过消息队列，不会打断用户正在进行的对话
2. **复用现有链路**：Claude 调用走现有的 `ClaudeIntegration`，只不过不经过 `MessageHandler`
3. **每天次数上限**：用 `proactive_today_count` 字段控制，默认每天最多 3 次；`max_per_day: 0` 表示不限次数，时间窗口内沉默超阈值就发
4. **错误静默**：Claude 调用失败或发飞书失败时静默跳过，不影响 bridge 主流程

## 主动消息格式

主动发到飞书的消息不走 Reply 线程（因为不是回复任何消息），直接发一条普通文本消息。格式：

```
📋 项目进展提醒

{Claude 返回的分析内容}
```

## 测试要点

- 时间窗口外不触发
- 沉默阈值内不触发
- 主动推送后当天计数 +1，超限后不再触发
- 第二天重新触发（计数清零）
- max_per_day: 0 时不限次数，每次沉默超阈值都触发
- 用户发消息后重置计时
- bridge 重启后状态不丢失
