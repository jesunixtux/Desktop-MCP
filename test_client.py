"""Test client for the Desktop MCP server (streamable-http).

Usage:
    .venv\Scripts\python test_client.py [--url http://127.0.0.1:3001/mcp] [--token TOKEN]

Runs a basic smoke suite: connect, list tools, permissions check, read-only
tools, and one visible input event (move_mouse). Results are printed as JSON
lines and appended to test.txt (the testing logbook).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import pathlib
import sys

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

LOGBOOK = pathlib.Path(__file__).parent / "test.txt"

BASIC_TOOLS = [
    "screenshot",
    "mouse_position",
    "get_screen_size",
    "move_mouse",
    "move_mouse_relative",
    "click",
    "double_click",
    "click_at",
    "drag",
    "scroll",
    "type_text",
    "press_key",
    "hotkey",
    "list_windows",
    "focus_window",
    "move_window",
    "resize_window",
    "check_permissions",
]


def log_entry(entry: dict) -> None:
    with LOGBOOK.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False))


async def call(session: ClientSession, name: str, args: dict | None = None) -> dict:
    result = await session.call_tool(name, args or {})
    return {"tool": name, "ok": True, "result": result}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3001/mcp")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    timeout = httpx2.Timeout(connect=10.0, write=10.0, read=120.0, pool=10.0)
    client = httpx2.AsyncClient(headers=headers, timeout=timeout)

    log_entry({
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": "run_start",
        "url": args.url,
        "auth": bool(args.token),
    })

    async with streamable_http_client(args.url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log_entry({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "event": "initialize_ok"})

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            missing = [n for n in BASIC_TOOLS if n not in names]
            log_entry({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "event": "list_tools",
                "count": len(names),
                "missing": missing,
            })

            # Read-only / diagnostics first.
            for tool, tool_args in [
                ("check_permissions", {}),
                ("get_screen_size", {}),
                ("mouse_position", {}),
                ("list_windows", {}),
            ]:
                try:
                    result = await call(session, tool, tool_args)
                    log_entry({
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                        **result,
                        "result": _simplify(result["result"]),
                    })
                except Exception as exc:
                    log_entry({
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                        "tool": tool,
                        "ok": False,
                        "error": str(exc),
                    })

            # One visible input event: move the mouse near the top-left (not a corner).
            try:
                result = await session.call_tool("move_mouse", {"x": 100, "y": 100, "duration": 0.3})
                log_entry({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "tool": "move_mouse",
                    "ok": True,
                    "result": _simplify(result),
                    "note": "visible input event - cursor should have moved",
                })
            except Exception as exc:
                log_entry({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "tool": "move_mouse",
                    "ok": False,
                    "error": str(exc),
                })

            # Screenshot: save PNG so it can be inspected (blank/black => missing permission).
            try:
                shot = await session.call_tool("screenshot", {})
                png = None
                for block in shot.content:
                    if getattr(block, "type", None) == "image":
                        png = base64.b64decode(block.data)
                        break
                if png is None:
                    raise RuntimeError(f"no image content block in response: {shot.content!r}")
                out = pathlib.Path(__file__).parent / "test_screenshot.png"
                out.write_bytes(png)
                log_entry({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "tool": "screenshot",
                    "ok": True,
                    "bytes": len(png),
                    "saved": str(out),
                })
            except Exception as exc:
                log_entry({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "tool": "screenshot",
                    "ok": False,
                    "error": str(exc),
                })

    log_entry({
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": "run_end",
    })
    return 0


def _simplify(result) -> str:
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content:
        parts = []
        for block in content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(str(block))
        return " | ".join(parts)
    text = getattr(result, "text", None)
    if text:
        return str(text)
    return str(result)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))