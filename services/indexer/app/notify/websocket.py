import json

import redis.asyncio as redis_async
from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings


async def ws_progress_handler(websocket: WebSocket, entry_id: str) -> None:
    await websocket.accept()
    client = redis_async.from_url(settings.redis_url)
    async with client.pubsub() as pubsub:
        channel = f"progress:{entry_id}"
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    text = data.decode()
                else:
                    text = str(data)
                await websocket.send_text(text)
                payload = json.loads(text)
                if payload.get("step") in {"done", "failed"} and payload.get("progress_pct") in {0, 100}:
                    break
        except WebSocketDisconnect:
            return
    await client.aclose()
