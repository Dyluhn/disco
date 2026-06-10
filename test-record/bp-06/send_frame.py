"""Send one WS control frame to a live conversation (harness-side stand-in for the
UI click): `approve_plan`, `confirm`, `reject`, `resume`, `cancel`.

Usage: .venv/bin/python test-record/bp-06/send_frame.py <cid> <frame-type>
"""

import asyncio
import json
import sys

import websockets


async def main(cid: str, frame_type: str) -> None:
    url = f"ws://127.0.0.1:8000/ws/conversations/{cid}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": frame_type}))
        # Drain a few frames so the server processes ours before we hang up.
        try:
            for _ in range(3):
                await asyncio.wait_for(ws.recv(), timeout=2)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
    print(f"sent {frame_type} -> {cid}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
