"""
手机眼睛插件 (Phone Eye) v0.2

让猫娘看见你的世界：手机 APP 拍下照片或发文字，POST 到本插件的 HTTP 端口，
插件把消息推送给 N.E.K.O 主系统，猫娘立即回应；猫娘的回复通过 WebSocket
实时推回手机端，形成双向聊天。

链路：
    手机 NekoCam APP
        │  POST /upload   (multipart: photo + text)
        │  POST /message  (JSON: {"text": "..."})
        │  GET  /ws       (WebSocket: 接收猫娘回复)
        ▼
    本插件 HTTP Server (默认 0.0.0.0:48930)
        │  ctx.images.upload() -> 规范化图片
        ▼
    ctx.push_message(ai_behavior="respond", parts=[...])
        │
        ▼
    猫娘（LLM）看到消息并生成回复
        │
        │  轮询 bus.messages.get() 捕获最新 assistant 消息
        ▼
    WebSocket 广播给已连接的手机端

不动 N.E.K.O 主项目任何源码，纯插件扩展。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp import web

from plugin.sdk.plugin import (
    NekoPluginBase,
    neko_plugin,
    lifecycle,
    Ok,
)


# 轮询间隔：猫娘回复通常在 2~15 秒内生成，2 秒轮询足够实时
POLL_INTERVAL_SEC = 2.0
# 缓存最近 N 条猫娘回复，新手机连上时补发
RECENT_HISTORY_LIMIT = 20
# 每条消息的最大文本长度（防止长文本刷屏）
MAX_TEXT_LEN = 2000


@neko_plugin
class PhoneEyePlugin(NekoPluginBase):
    """接收手机照片/文字并推送给猫娘，同时把猫娘回复通过 WebSocket 推回手机。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int = 48930
        self._token: str = ""
        # WebSocket 连接集合
        self._ws_clients: set[web.WebSocketResponse] = set()
        # 最近消息历史（猫娘回复缓存，新连接时补发）
        self._recent_replies: list[dict[str, Any]] = []
        # 上次轮询看到的最大消息序号，避免重复推送
        self._last_seen_seq: int = -1
        # 轮询任务
        self._poll_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self._port = int(await self.config.get("port", default=48930))
        self._token = str(await self.config.get("token", default="")) or ""

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/upload", self._handle_upload)
        app.router.add_post("/message", self._handle_message)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/history", self._handle_history)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()

        # 启动轮询协程
        self._poll_task = asyncio.create_task(self._poll_assistant_replies())

        self.logger.info(
            "PhoneEye HTTP+WS server listening on 0.0.0.0:%s (auth=%s)",
            self._port,
            "on" if self._token else "off",
        )
        return Ok({"status": "ready", "port": self._port})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        # 关闭所有 WebSocket 连接
        for ws in list(self._ws_clients):
            try:
                await ws.close(code=1001, message=b"server shutdown")
            except Exception:
                pass
        self._ws_clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.logger.info("PhoneEye HTTP+WS server stopped")
        return Ok({"status": "stopped"})

    # ------------------------------------------------------------------
    # HTTP 端点
    # ------------------------------------------------------------------
    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "service": "phone_eye",
            "ws_clients": len(self._ws_clients),
        })

    async def _handle_upload(self, request: web.Request) -> web.Response:
        """手机拍照后 POST 图片（multipart/form-data: photo + 可选 text）。"""
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            photo: bytes | None = None
            text: str = ""
            reader = await request.multipart()
            async for part in reader:
                if part.name == "photo":
                    photo = await part.read()
                elif part.name == "text":
                    raw = await part.read()
                    text = raw.decode("utf-8", errors="ignore").strip()
            if not photo:
                return web.json_response(
                    {"ok": False, "error": "missing field 'photo'"}, status=400
                )
            await self._deliver_photo(photo, text)
            return web.json_response({"ok": True, "queued": True})
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("phone_eye upload handler failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _handle_message(self, request: web.Request) -> web.Response:
        """纯文字聊天消息：POST JSON {"text": "你好"}。"""
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON"}, status=400
            )
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response(
                {"ok": False, "error": "empty text"}, status=400
            )
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN]
        try:
            await self._deliver_text(text)
            return web.json_response({"ok": True, "queued": True})
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("phone_eye message handler failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket 端点：手机端连接后实时接收猫娘回复。"""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # 鉴权
        if not self._check_auth(request):
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        self._ws_clients.add(ws)
        client_ip = request.remote
        self.logger.info("phone_eye WS client connected: %s (total=%d)",
                         client_ip, len(self._ws_clients))
        # 连上后立即补发最近历史
        for msg in self._recent_replies:
            try:
                await ws.send_json(msg)
            except Exception:
                break
        try:
            async for raw in ws:
                # 手机端发过来的消息（主要是 ping/心跳，忽略内容）
                if raw.type == web.WSMsgType.TEXT:
                    # 收到 ping 就回 pong，简单保活
                    if raw.data == "ping":
                        await ws.send_str("pong")
        finally:
            self._ws_clients.discard(ws)
            self.logger.info("phone_eye WS client disconnected: %s (total=%d)",
                             client_ip, len(self._ws_clients))
        return ws

    async def _handle_history(self, request: web.Request) -> web.Response:
        """GET /history 返回最近缓存的猫娘回复。"""
        if not self._check_auth(request):
            return web.json_response({"ok": False}, status=401)
        return web.json_response({
            "ok": True,
            "messages": list(self._recent_replies),
        })

    # ------------------------------------------------------------------
    # 内部实现：投递消息给猫娘
    # ------------------------------------------------------------------
    def _check_auth(self, request) -> bool:
        if not self._token:
            return True
        return request.headers.get("X-Neko-Token", "") == self._token

    async def _deliver_photo(self, photo: bytes, text: str) -> None:
        """上传图片并推送给猫娘（立即 respond）。"""
        image_part = await self.ctx.images.upload(photo, mime="image/jpeg")

        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "text": text})
        else:
            parts.append({
                "type": "text",
                "text": (
                    "{MASTER_NAME} 刚刚用手机拍了一张照片，拍的是我身边的现实环境。"
                    "看看这张照片，告诉我你看到了什么、周围是什么情况。"
                ),
            })
        parts.append(image_part)

        await self._push_to_neko(parts, source="phone_eye_photo")
        self.logger.info("photo delivered to N.E.K.O (bytes=%d, text=%r)",
                         len(photo), text)

    async def _deliver_text(self, text: str) -> None:
        """纯文字消息推送给猫娘。"""
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": f"[来自手机] {text}"},
        ]
        await self._push_to_neko(parts, source="phone_eye_text")
        self.logger.info("text delivered to N.E.K.O: %r", text)

    async def _push_to_neko(self, parts: list[dict[str, Any]], *, source: str) -> None:
        """统一的 push_message 调用，统一错误处理。"""
        result = self.ctx.push_message(
            visibility=[],
            ai_behavior="respond",
            parts=parts,
            source=source,
        )
        if not result.get("submitted"):
            reason = result.get("reason", "unknown")
            self.logger.warning("push_message not submitted: %s", reason)
            raise RuntimeError(f"push_message rejected: {reason}")

    # ------------------------------------------------------------------
    # 轮询：捕获猫娘回复并广播给手机端
    # ------------------------------------------------------------------
    async def _poll_assistant_replies(self) -> None:
        """定期从 bus.messages 拉取最新 assistant 消息，广播给 WS 客户端。"""
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.debug("poll assistant replies failed", exc_info=True)

    async def _poll_once(self) -> None:
        # bus.messages.get 是 async 的（见 SDK 协议）
        bus = getattr(self.ctx, "bus", None)
        if bus is None or bus.messages is None:
            return
        msgs = await bus.messages.get(max_count=10)
        # msgs 可能是列表或有 .items() 的对象，兼容处理
        items = self._extract_message_items(msgs)
        if not items:
            return
        # 按 seq 排序，只处理比 last_seen_seq 更新的
        new_items: list[dict[str, Any]] = []
        max_seq = self._last_seen_seq
        for it in items:
            seq = self._extract_seq(it)
            if seq > max_seq:
                max_seq = seq
                new_items.append(it)
        if max_seq > self._last_seen_seq:
            self._last_seen_seq = max_seq
        # 过滤出 assistant 角色的文本消息
        for it in new_items:
            role = self._extract_role(it)
            if role != "assistant":
                continue
            text = self._extract_text(it)
            if not text:
                continue
            await self._broadcast_reply(text)

    def _extract_message_items(self, msgs: Any) -> list[dict[str, Any]]:
        """兼容多种返回格式：list / .items() / .rows / 直接是 list[dict]。"""
        if msgs is None:
            return []
        if isinstance(msgs, list):
            return [m for m in msgs if isinstance(m, dict)]
        if hasattr(msgs, "items"):
            try:
                raw = msgs.items()
                return [m for m in raw if isinstance(m, dict)]
            except Exception:
                pass
        if hasattr(msgs, "rows"):
            try:
                return [m for m in msgs.rows if isinstance(m, dict)]
            except Exception:
                pass
        return []

    def _extract_seq(self, item: dict[str, Any]) -> int:
        for key in ("seq", "id", "timestamp", "ts"):
            v = item.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return int(v)
        return -1

    def _extract_role(self, item: dict[str, Any]) -> str:
        return str(item.get("role", "")).lower()

    def _extract_text(self, item: dict[str, Any]) -> str:
        # content 可能是 str 或 list[part]
        content = item.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(str(part.get("text", "")))
            return "\n".join(chunks).strip()
        return ""

    async def _broadcast_reply(self, text: str) -> None:
        """把猫娘回复加入历史并广播给所有 WS 客户端。"""
        msg = {
            "type": "neko_reply",
            "text": text,
            "ts": time.time(),
        }
        self._recent_replies.append(msg)
        if len(self._recent_replies) > RECENT_HISTORY_LIMIT:
            self._recent_replies = self._recent_replies[-RECENT_HISTORY_LIMIT:]
        # 广播
        dead: list[web.WebSocketResponse] = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)
        self.logger.info("neko reply broadcast to %d client(s): %.80s",
                         len(self._ws_clients), text)
