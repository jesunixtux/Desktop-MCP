# AGENTS.md

Single-file MCP server (`desktop_mcp.py`) that lets LLM models control Windows: mouse, keyboard, screenshots, and window management (PowerShell), via the `mcp` 2.x SDK + `pyautogui`.

## Run

```sh
.venv\Scripts\python desktop_mcp.py
```

- Always use `.venv\Scripts\python`.
- Deps are pinned in `requirements.txt` (`mcp==2.0.0`, `pyautogui==0.9.54`, `Pillow==12.3.0`, `uvicorn==0.52.3`). If you add a dep: `.venv\Scripts\pip install ...` and pin it in `requirements.txt`.
- Serves streamable-http on `0.0.0.0:3001`; MCP clients connect to `http://localhost:3001/mcp` (default path, not the root).
- Env vars (all optional): `MCP_HOST` (default 0.0.0.0), `MCP_PORT` (3001), `MCP_PATH` (/mcp), `MCP_AUTH_TOKEN` — if set, clients MUST send `Authorization: Bearer <token>` (enforced by a pure-ASGI middleware; 401 otherwise). The server no longer uses `mcp.run()` — it builds the app via `mcp.streamable_http_app()` and serves it with `uvicorn.run()` directly.
- No tests, no lint/typecheck config, no git repo. Verification: start the server and hit it with an MCP client, or `import desktop_mcp` from the venv python and inspect `asyncio.run(mcp.list_tools())`. `test_client.py` runs a basic smoke suite (list tools, permissions, read-only tools, one mouse move, screenshot) and appends JSON results to `test.txt` — use it for manual verification. Live testing of the desktop tools happens on request.
- Only one server can bind the port: if 3001 is taken (e.g. the user's instance is running), start your own on `MCP_PORT=3101` and point the client at `http://127.0.0.1:3101/mcp`.

## Tools (18)

- Mouse: `move_mouse`, `move_mouse_relative`, `click`, `double_click`, `click_at`, `drag`, `scroll`, `mouse_position`, `get_screen_size`
- Keyboard: `type_text`, `press_key`, `hotkey(keys: list[str])` → `pyautogui.hotkey(*keys)` (whole combo in one call)
- Windows (Windows/PowerShell via `powershell.exe`): `list_windows`, `focus_window`, `move_window`, `resize_window` — `app` is the process name as shown by `list_windows` (e.g. "notepad"); window ops act on the front window unless a `title` is passed
- Diagnostics: `screenshot` (returns PNG `Image`), `check_permissions`

## Gotchas

- Windows: pyautogui works out of the box without special permissions, as long as the server runs as a regular desktop user (not sandboxed/UWP). `check_permissions()` always returns true for both.
- `pyautogui.FAILSAFE = True` (hardcoded): yanking the mouse to a screen corner raises `FailSafeException` and aborts the call — it is not a bug in your code. The `@tool_safe` decorator (below `@mcp.tool()`) maps it to an actionable message; `inspect.signature` follows `__wrapped__` so schemas are unaffected. Keep the decorator stack order.
- Tool behavior is deliberately clamped server-side: `move_mouse`/`drag`/`move_mouse_relative` durations are clamped to 0.15–3.0s and coordinates validated with `pyautogui.onScreen` (raises `ValueError` off-screen); `scroll` is clamped to ±10; `click`/`double_click`/`click_at`/`drag` accept only `left`/`right`/`middle`.
- Don't "fix" the clamps, the failsafe, or the auth middleware — they are intentional safety guards.
- `type_text` with non-ASCII (accents, emoji) cannot be typed by pyautogui: the text is piped through `clip` (Windows clipboard) and pasted with Ctrl+V (returns a different message so the caller knows). ASCII is typed char-by-char with `interval`.
- All PowerShell calls go through `_powershell()` (20s timeout) and `_ps_quote()` (quote-escaping). `list_windows` uses `Get-Process` + Win32 `GetWindowRect` — it is fast (under 1s). The client must still tolerate slow tools; `httpx2.AsyncClient()` with no explicit timeout defaults to a 5s read and will kill slow calls with "SSE stream ended without a response".
- `httpx2` (mcp client SDK) caps SSE events at 1 MiB: a full-screen PNG in base64 can exceed it and the client aborts the call. `screenshot` re-encodes via `_encode_screenshot()` (PNG → JPEG q85/70/55 → downscale) to keep base64 under `_MAX_SSE_IMAGE_BYTES` (700KB). This limit is per-event, client-side, and not configurable through the SDK — don't remove the cap.
- Keep the module importable without side effects: preflight permission warnings and uvicorn startup live in `main()`, not at module level.