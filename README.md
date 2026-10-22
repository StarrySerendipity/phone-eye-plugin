# phone-eye-plugin

**N.E.K.O 插件：手机 ↔ 猫娘 视觉/聊天桥接**

让猫娘"看见"你在外面的世界。手机 APP 拍照或发文字，POST 到本插件的 HTTP 端口，插件把消息推送给 N.E.K.O 主系统；猫娘的回复通过 WebSocket 实时推回手机端，形成双向聊天。

```
手机 NekoCam APP
    │  POST /upload   (multipart: photo + text)
    │  POST /message  (JSON: {"text": "..."})
    │  GET  /ws       (WebSocket: 接收猫娘回复)
    ▼
本插件 HTTP Server (默认 0.0.0.0:48930)
    │  ctx.images.upload() -> 规范化图片
    ▼
ctx.push_message(ai_behavior="respond", parts=[...])
    ▼
猫娘（LLM）看到消息并生成回复
    │  轮询 bus.messages.get() 捕获最新 assistant 消息
    ▼
WebSocket 广播给已连接的手机端
```

## 安装

把整个目录放到 N.E.K.O 的 `plugin/plugins/phone_eye/` 下即可。N.E.K.O 启动时会自动加载。

## 配置

编辑 `plugin.toml` 或在 N.E.K.O 的插件设置里：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `port` | `48930` | HTTP + WebSocket 监听端口 |
| `token` | `""` | 鉴权令牌（留空不校验）。手机请求头需带 `X-Neko-Token: <token>` |

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回 `{"ok": true, "ws_clients": N}` |
| POST | `/upload` | 上传照片（multipart/form-data: `photo` JPEG + 可选 `text`） |
| POST | `/message` | 纯文字消息（JSON `{"text": "..."}`） |
| GET | `/ws` | WebSocket，连接后实时接收猫娘回复 `{"type":"neko_reply","text":"...","ts":...}` |
| GET | `/history` | 返回最近缓存的猫娘回复列表 |

## 手机端

配套 APP：**NekoCam**（见同仓库 release 或自行构建）。

- 设置里填服务器地址 `http://<电脑IP>:48930` 和 token
- 拍照后自动上传，跳进聊天页等猫娘回复
- 聊天页支持文字对话，WebSocket 实时接收回复

## 测试

```bash
cd N.E.K.O
.venv/bin/python -m plugin.plugins.phone_eye.tests.test_core
```
