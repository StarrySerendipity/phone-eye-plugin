"""phone_eye 插件核心逻辑单测（不依赖 N.E.K.O 运行环境）。

验证：
1. multipart/form-data 解析（photo + text 字段）
2. 鉴权（X-Neko-Token）
3. POST /message 纯文字端点
4. ctx.images.upload() + push_message() 投递链路的参数正确性
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

from aiohttp import multipart, web
from aiohttp.test_utils import TestClient, TestServer

from plugin.plugins.phone_eye import PhoneEyePlugin


class FakeContext:
    """模拟插件宿主 ctx：只记录 images.upload / push_message 调用。"""

    def __init__(self):
        self.uploaded: list[bytes] = []
        self.pushed: list[dict[str, Any]] = []
        self.logger = mock.MagicMock()

    async def upload(self, data: bytes, *, mime: str | None = None, timeout: float = 3.0):
        self.uploaded.append(data)
        return {"type": "image", "url": "http://fake.local/tmp.jpg", "mime": "image/jpeg"}

    def push_message(self, **kwargs) -> dict[str, Any]:
        self.pushed.append(kwargs)
        return {"submitted": True}

    @property
    def images(self):
        return self


def make_plugin(token: str = "") -> PhoneEyePlugin:
    plugin = PhoneEyePlugin.__new__(PhoneEyePlugin)  # 绕过真实 __init__
    plugin.logger = mock.MagicMock()
    plugin._port = 48930
    plugin._token = token
    plugin.ctx = FakeContext()
    # 新增的双向通信状态
    plugin._ws_clients = set()
    plugin._recent_replies = []
    plugin._last_seen_seq = -1
    plugin._poll_task = None
    return plugin


def make_client(plugin: PhoneEyePlugin) -> TestClient:
    app = web.Application()
    app.router.add_post("/upload", plugin._handle_upload)
    app.router.add_post("/message", plugin._handle_message)
    app.router.add_get("/health", plugin._handle_health)
    app.router.add_get("/history", plugin._handle_history)
    return TestClient(TestServer(app))


async def test_health_ok():
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        resp = await client.get("/health")
        body = await resp.json()
        assert resp.status == 200
        assert body["ok"] is True
        assert body["ws_clients"] == 0
    finally:
        await client.close()


async def test_upload_without_auth_token_succeeds():
    """未设置 token 时，上传应成功并触发投递。"""
    plugin = make_plugin(token="")
    client = make_client(plugin)
    await client.start_server()
    try:
        photo = b"\xff\xd8\xff\xe0" + b"\x00" * 2048  # 伪 JPEG 字节
        writer = multipart.MultipartWriter("form-data")
        writer.append(photo, headers={
            "Content-Disposition": 'form-data; name="photo"; filename="photo.jpg"',
            "Content-Type": "image/jpeg",
        })
        writer.append("看看这个", headers={
            "Content-Disposition": 'form-data; name="text"',
        })
        headers = {"Content-Type": f"multipart/form-data; boundary={writer.boundary}"}
        resp = await client.post("/upload", data=writer, headers=headers)
        body = await resp.json()
        assert resp.status == 200
        assert body["ok"] is True
        assert len(plugin.ctx.uploaded) == 1
        assert plugin.ctx.uploaded[0] == photo
        assert len(plugin.ctx.pushed) == 1
        pushed = plugin.ctx.pushed[0]
        assert pushed["ai_behavior"] == "respond"
        assert pushed["source"] == "phone_eye_photo"
        parts = pushed["parts"]
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "看看这个"
        assert parts[1]["type"] == "image"
        assert parts[1]["url"] == "http://fake.local/tmp.jpg"
    finally:
        await client.close()


async def test_upload_with_token_requires_auth():
    """设置了 token 时，缺 token 应 401，带正确 token 应成功。"""
    plugin = make_plugin(token="sekret")
    client = make_client(plugin)
    await client.start_server()
    try:
        photo = b"\xff\xd8\xff\xe0" + b"\x00" * 1024
        writer = multipart.MultipartWriter("form-data")
        writer.append(photo, headers={
            "Content-Disposition": 'form-data; name="photo"; filename="photo.jpg"',
            "Content-Type": "image/jpeg",
        })
        headers = {"Content-Type": f"multipart/form-data; boundary={writer.boundary}"}

        resp = await client.post("/upload", data=writer, headers=headers)
        assert resp.status == 401
        assert plugin.ctx.pushed == []

        resp = await client.post("/upload", data=writer, headers={**headers, "X-Neko-Token": "wrong"})
        assert resp.status == 401

        resp = await client.post("/upload", data=writer, headers={**headers, "X-Neko-Token": "sekret"})
        body = await resp.json()
        assert resp.status == 200
        assert body["ok"] is True
        assert len(plugin.ctx.pushed) == 1
    finally:
        await client.close()


async def test_upload_without_photo_rejected():
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        writer = multipart.MultipartWriter("form-data")
        writer.append("只有文字", headers={"Content-Disposition": 'form-data; name="text"'})
        headers = {"Content-Type": f"multipart/form-data; boundary={writer.boundary}"}
        resp = await client.post("/upload", data=writer, headers=headers)
        body = await resp.json()
        assert resp.status == 400
        assert body["ok"] is False
    finally:
        await client.close()


async def test_default_text_uses_placeholder():
    """无文字时默认提示语应含 {MASTER_NAME} 占位符。"""
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        photo = b"\xff\xd8\xff\xe0" + b"\x00" * 512
        writer = multipart.MultipartWriter("form-data")
        writer.append(photo, headers={
            "Content-Disposition": 'form-data; name="photo"; filename="photo.jpg"',
            "Content-Type": "image/jpeg",
        })
        headers = {"Content-Type": f"multipart/form-data; boundary={writer.boundary}"}
        resp = await client.post("/upload", data=writer, headers=headers)
        body = await resp.json()
        assert resp.status == 200
        assert "{MASTER_NAME}" in plugin.ctx.pushed[0]["parts"][0]["text"]
    finally:
        await client.close()


async def test_post_message_text_only():
    """POST /message 纯文字消息应直接 push_message。"""
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        resp = await client.post("/message", json={"text": "我到公园了，樱花开了"})
        body = await resp.json()
        assert resp.status == 200
        assert body["ok"] is True
        assert len(plugin.ctx.pushed) == 1
        pushed = plugin.ctx.pushed[0]
        assert pushed["source"] == "phone_eye_text"
        assert "我到公园了" in pushed["parts"][0]["text"]
    finally:
        await client.close()


async def test_post_message_empty_rejected():
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        resp = await client.post("/message", json={"text": "   "})
        assert resp.status == 400
    finally:
        await client.close()


async def test_history_empty_by_default():
    plugin = make_plugin()
    client = make_client(plugin)
    await client.start_server()
    try:
        resp = await client.get("/history")
        body = await resp.json()
        assert resp.status == 200
        assert body["messages"] == []
    finally:
        await client.close()


async def main() -> int:
    tests = [
        test_health_ok,
        test_upload_without_auth_token_succeeds,
        test_upload_with_token_requires_auth,
        test_upload_without_photo_rejected,
        test_default_text_uses_placeholder,
        test_post_message_text_only,
        test_post_message_empty_rejected,
        test_history_empty_by_default,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
            print(f"[PASS] {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"[FAIL] {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
