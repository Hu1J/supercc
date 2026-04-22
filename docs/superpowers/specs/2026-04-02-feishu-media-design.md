# 飞书图片/文件双向传输设计

## 概述

**项目:** `cc-feishu-bridge`
**功能:** 双向图片/文件传输
**本期范围:** 图片 (`image`) + 文件 (`file`)
**下期范围:** 音频 (`audio`)

---

## 目录结构

CLI 执行目录下的 `.cc-feishu-bridge/` 目录（由程序初始化时自动创建）：

```
执行目录/
└── .cc-feishu-bridge/
    ├── config.yaml          # 现有配置
    ├── sessions.db          # 现有会话
    ├── received_images/     # 收到的图片
    └── received_files/     # 收到的文件
```

---

## 一、Inbound（接收飞书图片/文件）

### 消息类型判断

```
msg_type == "image"
  → download_media() 下载二进制
  → 保存到 .cc-feishu-bridge/received_images/img_{时间戳}_{message_id[:8]}.{ext}
  → prompt 拼接: "[图片: .cc-feishu-bridge/received_images/img_20260402_143052_abc12345.png]"

msg_type == "file"
  → download_media() 下载二进制
  → 保存到 .cc-feishu-bridge/reived_files/file_{时间戳}_{message_id[:8]}_{原文件名}.{ext}
  → prompt 拼接: "[文件: .cc-feishu-bridge/received_files/file_20260402_143052_abc12345_document.pdf]"

其他 (audio 等)
  → 跳过，log 提示"暂不支持该消息类型"
```

### 文件名命名

- **图片**: `img_{YYYYMMDD}_{HHMMSS}_{message_id[:8]}.{ext}`
  - 例: `img_20260402_143052_abc12345.png`
- **文件**: `file_{YYYYMMDD}_{HHMMSS}_{message_id[:8]}_{原文件名}.{ext}`
  - 例: `file_20260402_143052_abc12345_document.pdf`

文件名特殊字符（空格、`/`、`\` 等）替换为 `_`，扩展名从 MIME 类型推导。

### 处理流程（message_handler.py）

```
handle() 收到消息
  → 检测 msg_type
  → image/file: 调用 feishu.download_media()
  → 保存到本地路径
  → 将路径拼入 prompt
  → 原有逻辑不变，继续调用 Claude
```

---

## 二、Outbound（Claude 图片 → 发回飞书）

### 流程

```
Claude 流式输出
  → _parse_message() 检测 ImageBlock → 收集 {base64, mimeType} 列表
  → response 发完后，依次调用 upload_image()
  → upload_image() → image.create() → 拿 image_key
  → send_image(chat_id, image_key)
```

- 多图全部发送，无顺序保证
- 纯图片，无文字说明
- 无需写临时文件，内存直接传

---

## 三、FeishuClient 新增 API（feishu/client.py）

### `download_media(message_id: str, file_key: str) -> bytes`

调用 `lark_oapi.message_resource.get()` 下载媒体二进制。

### `upload_image(image_bytes: bytes) -> str`

调用 `lark_oapi.image.create()` 上传图片，返回 `image_key`。

### `send_image(chat_id: str, image_key: str) -> str`

调用 `lark_oapi.message.create()` 发送图片消息，返回 `message_id`。

---

## 四、ClaudeIntegration 改动（claude/integration.py）

### `_parse_message()` 新增 ImageBlock 检测

```python
elif block_type == "ImageBlock":
    image_data = getattr(block, "data", "")       # base64
    mime_type  = getattr(block, "mimeType", "")  # "image/png"
    return ClaudeMessage(content="", is_final=False, image_data=image_data, mime_type=mime_type)
```

### `ClaudeMessage` 新增字段

```python
@dataclass
class ClaudeMessage:
    content: str
    is_final: bool = False
    tool_name: str | None = None
    tool_input: str | None = None
    image_data: str | None = None   # 新增: base64 图片数据
    mime_type: str | None = None   # 新增: 图片 MIME 类型
```

### MessageHandler 改动（feishu/message_handler.py）

- `handle()` 在调用 `claude.query()` 前，将图片/文件路径拼入 prompt
- stream_callback 收集所有 ImageBlock 的 `{base64, mimeType}`
- query 返回后，依次调用 `feishu.send_image()` 发送图片

---

## 五、错误处理

| 场景 | 处理 |
|------|------|
| `download_media` 失败 | 跳过媒体，继续处理文字，log warning |
| `upload_image` 失败 | 重试 1 次，再失败则发文字提示"图片发送失败" |
| 图片超过 30MB | 发文字提示"图片超过 30MB，无法发送" |
| 非图片/文件类型 | 跳过，log 提示暂不支持 |
| 存储目录创建失败 | 回退到 `/tmp/cc-feishu-bridge/` |

---

## 六、依赖变更

无新增依赖，`lark-oapi` 已覆盖所有媒体 API。
