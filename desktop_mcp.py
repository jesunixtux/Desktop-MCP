"""Desktop MCP server: lets LLM models control Windows via pyautogui + PowerShell.

Run:
    .venv\\Scripts\\python desktop_mcp.py

Env vars (all optional):
    MCP_HOST      bind address, default 0.0.0.0
    MCP_PORT      port, default 3001
    MCP_PATH      MCP endpoint path, default /mcp
    MCP_AUTH_TOKEN  if set, clients must send `Authorization: Bearer <token>`

Windows: pyautogui works out of the box. No special permissions are needed
(assume the server runs as a regular desktop user, not a UWP/sandboxed app).
"""

from __future__ import annotations

import functools
import hmac
import logging
import os
import subprocess
from io import BytesIO

import pyautogui
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_mcp")

mcp = MCPServer("Desktop MCP Prototype")

# Safety guards (intentional, do not remove).
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.15

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "3001"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN") or None

_BUTTONS = ("left", "right", "middle")
_MIN_DURATION, _MAX_DURATION = 0.15, 3.0

# httpx2 (used by the mcp 2.x client SDK) caps SSE events at 1 MiB; a full-screen
# PNG base64 easily exceeds that and kills the response. Keep encoded images well
# under the cap so every client can receive them.
_MAX_SSE_IMAGE_BYTES = 700_000


def _encode_screenshot(image) -> tuple[bytes, str]:
    """Encode a screenshot, shrinking it until it fits client SSE limits.

    Returns (data, format) with format in {"png", "jpeg"}. Tries PNG first, then
    JPEG at decreasing quality, then downscales as a last resort.
    """
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    if len(data) * 4 / 3 <= _MAX_SSE_IMAGE_BYTES:
        return data, "png"

    current = image
    if current.mode == "RGBA":
        current = current.convert("RGB")
    for quality in (85, 70, 55):
        buffer = BytesIO()
        current.save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if len(data) * 4 / 3 <= _MAX_SSE_IMAGE_BYTES:
            return data, "jpeg"

    while True:
        width, height = current.size
        current = current.resize((int(width * 0.75), int(height * 0.75)))
        buffer = BytesIO()
        current.save(buffer, format="JPEG", quality=70)
        data = buffer.getvalue()
        if len(data) * 4 / 3 <= _MAX_SSE_IMAGE_BYTES or min(current.size) < 200:
            return data, "jpeg"


def _clamp_duration(duration: float) -> float:
    return max(_MIN_DURATION, min(duration, _MAX_DURATION))


def _validate_xy(x: int, y: int) -> None:
    if not pyautogui.onScreen(x, y):
        width, height = pyautogui.size()
        raise ValueError(f"Coordinates ({x}, {y}) are outside the screen (screen is {width}x{height}).")


def tool_safe(func):
    """Wrap a tool: log the call and turn FailSafe into an actionable error.

    Applied BELOW @mcp.tool() so the SDK registers this wrapper; inspect.signature
    follows __wrapped__ so tool schemas are unaffected.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info("tool call: %s", func.__name__)
        try:
            return func(*args, **kwargs)
        except pyautogui.FailSafeException:
            raise ValueError(
                "FailSafe triggered: the mouse hit a corner of the screen, which aborts the "
                "call (pyautogui.FAILSAFE is enabled by design). This is a safety guard, not a "
                "bug - retry the operation."
            ) from None

    return wrapper


def _powershell(script: str) -> str:
    """Run a PowerShell script; raises actionable errors on failure."""
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        raise RuntimeError(f"PowerShell failed: {err}")
    return proc.stdout


def _ps_quote(s: str) -> str:
    """Escape a string for safe embedding in a PowerShell single-quoted string."""
    return s.replace("'", "''")


def _accessibility_granted() -> bool:
    """On Windows, pyautogui works without special accessibility permissions."""
    return True


def _screen_recording_granted() -> bool:
    """On Windows, pyautogui.screenshot() works without special permissions."""
    return True


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_safe
def screenshot() -> Image:
    """Take a screenshot of the desktop. Returns a PNG (or JPEG if large).

    Screenshots are capped to fit MCP client SSE limits (1 MiB event): large
    captures are re-encoded as JPEG and downscaled if still too big.
    """

    image = pyautogui.screenshot()

    data, fmt = _encode_screenshot(image)

    return Image(
        data=data,
        format=fmt
    )


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_safe
def mouse_position() -> dict:
    """Get current mouse position as {x, y}."""

    x, y = pyautogui.position()

    return {"x": x, "y": y}


@mcp.tool()
@tool_safe
def get_screen_size() -> dict:
    """Get the screen size as {width, height}."""

    width, height = pyautogui.size()

    return {"width": width, "height": height}


@mcp.tool()
@tool_safe
def move_mouse(
    x: int,
    y: int,
    duration: float = 0.5
) -> str:
    """Move the mouse visibly to an absolute screen coordinate. Duration is clamped to 0.15-3.0s."""

    _validate_xy(x, y)

    pyautogui.moveTo(x, y, duration=_clamp_duration(duration))

    return f"Mouse moved to ({x}, {y})"


@mcp.tool()
@tool_safe
def move_mouse_relative(
    dx: int,
    dy: int,
    duration: float = 0.5
) -> str:
    """Move the mouse by a relative offset (dx, dy) from its current position."""

    x, y = pyautogui.position()
    _validate_xy(x + dx, y + dy)

    pyautogui.moveRel(dx, dy, duration=_clamp_duration(duration))

    return f"Mouse moved by ({dx}, {dy}) to ({x + dx}, {y + dy})"


@mcp.tool()
@tool_safe
def click(
    button: str = "left"
) -> str:
    """Click at the current mouse position. Button: left, right or middle."""

    if button not in _BUTTONS:
        raise ValueError(f"Invalid mouse button '{button}'. Use one of: {', '.join(_BUTTONS)}.")

    pyautogui.click(button=button)

    return f"{button} click"


@mcp.tool()
@tool_safe
def double_click(
    button: str = "left"
) -> str:
    """Double-click at the current mouse position. Button: left, right or middle."""

    if button not in _BUTTONS:
        raise ValueError(f"Invalid mouse button '{button}'. Use one of: {', '.join(_BUTTONS)}.")

    pyautogui.doubleClick(button=button)

    return f"{button} double-click"


@mcp.tool()
@tool_safe
def click_at(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1
) -> str:
    """Move to an absolute coordinate and click there. Button: left, right or middle."""

    if button not in _BUTTONS:
        raise ValueError(f"Invalid mouse button '{button}'. Use one of: {', '.join(_BUTTONS)}.")
    if clicks < 1:
        raise ValueError("clicks must be >= 1.")

    _validate_xy(x, y)

    pyautogui.click(x, y, clicks=clicks, interval=0.05, button=button)

    return f"{clicks}x {button} click at ({x}, {y})"


@mcp.tool()
@tool_safe
def drag(
    x: int,
    y: int,
    duration: float = 0.5,
    button: str = "left"
) -> str:
    """Drag from the current mouse position to an absolute coordinate (press, move, release)."""

    if button not in _BUTTONS:
        raise ValueError(f"Invalid mouse button '{button}'. Use one of: {', '.join(_BUTTONS)}.")

    _validate_xy(x, y)

    pyautogui.dragTo(x, y, duration=_clamp_duration(duration), button=button)

    return f"Dragged to ({x}, {y}) with {button} button"


@mcp.tool()
@tool_safe
def scroll(amount: int) -> str:
    """Scroll vertically. Positive scrolls up, negative scrolls down. Amount is clamped to +/-10."""

    amount = max(-10, min(amount, 10))

    pyautogui.scroll(amount)

    return f"Scrolled {amount}"


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_safe
def type_text(
    text: str,
    interval: float = 0.03
) -> str:
    """Type text using the keyboard.

    ASCII text is typed character by character. Non-ASCII text (accents, symbols, emoji)
    cannot be typed by pyautogui, so it is copied to the clipboard and pasted with Ctrl+V.
    """

    if text.isascii():
        pyautogui.write(text, interval=interval)
        return f"Typed {len(text)} characters"

    proc = subprocess.run(
        ["clip"],
        input=text.encode("utf-16le"),
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError("Failed to copy text to the clipboard (clip).")

    pyautogui.hotkey("ctrl", "v")

    return f"Pasted {len(text)} characters via clipboard (non-ASCII text)"


@mcp.tool()
@tool_safe
def press_key(key: str) -> str:
    """Press one keyboard key (e.g. 'enter', 'tab', 'escape', 'a', 'F5')."""

    pyautogui.press(key)

    return f"Pressed {key}"


@mcp.tool()
@tool_safe
def hotkey(keys: list[str]) -> str:
    """Press a keyboard shortcut (e.g. ['ctrl', 'c'] for copy, ['alt', 'tab'] to switch windows)."""

    pyautogui.hotkey(*keys)

    return f"Pressed {' + '.join(keys)}"


# ---------------------------------------------------------------------------
# Window management (Windows / PowerShell)
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_safe
def list_windows() -> list[dict]:
    """List open app windows: app name, title, position (x, y), size (width, height)."""

    script = """
    Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    public class WinAPI {
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
        [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    }
    "@
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -ne '' -and $_.MainWindowHandle -ne [IntPtr]::Zero }
    foreach ($p in $procs) {
        $rect = New-Object WinAPI+RECT
        [WinAPI]::GetWindowRect($p.MainWindowHandle, [ref]$rect) | Out-Null
        $w = $rect.Right - $rect.Left
        $h = $rect.Bottom - $rect.Top
        Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $p.ProcessName, $p.MainWindowTitle, $rect.Left, $rect.Top, $w, $h)
    }
    """

    windows = []
    for line in _powershell(script).splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        app, title, x, y, width, height = fields
        windows.append({
            "app": app,
            "title": title,
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
        })

    return windows


@mcp.tool()
@tool_safe
def focus_window(
    app: str,
    title: str | None = None
) -> str:
    """Bring an app to the front; optionally raise a specific window of that app by title.

    `app` must be the process name as reported by list_windows (e.g. 'notepad').
    """

    if title:
        script = (
            f"$p = Get-Process -Name '{_ps_quote(app)}' | Where-Object {{ $_.MainWindowTitle -eq '{_ps_quote(title)}' }};"
            "if ($p) { $p | ForEach-Object { $_.MainWindowHandle } | ForEach-Object { "
            "Add-Type -Name W -N 'U' -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);'; "
            " [U]::SetForegroundWindow($_) } }"
        )
    else:
        script = (
            f"$p = Get-Process -Name '{_ps_quote(app)}' -ErrorAction Stop;"
            "$h = ($p | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1).MainWindowHandle;"
            "if ($h -ne [IntPtr]::Zero) { "
            "Add-Type -Name W -N 'U' -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);'; "
            " [U]::SetForegroundWindow($h) }"
        )

    _powershell(script)

    return f"Focused {app}" + (f" (window: {title})" if title else "")


@mcp.tool()
@tool_safe
def move_window(
    app: str,
    x: int,
    y: int,
    title: str | None = None
) -> str:
    """Move a window of an app to absolute screen coordinates (x, y).

    Moves the front window unless `title` is given (must match list_windows output).
    """

    where = (
        f"Where-Object {{ $_.MainWindowTitle -eq '{_ps_quote(title)}' }}"
        if title
        else "Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1"
    )

    script = (
        "$code = @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WP {\n"
        "  [DllImport(\"user32.dll\")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int h2, bool r);\n"
        "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
        "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L; public int T; public int R; public int B; }\n"
        "}\n"
        "'@\n"
        "Add-Type $code\n"
        f"$p = Get-Process -Name '{_ps_quote(app)}' -ErrorAction Stop | {where};\n"
        "if ($p) {\n"
        "  $h = $p.MainWindowHandle\n"
        "  $rect = New-Object WP+RECT\n"
        "  [WP]::GetWindowRect($h, [ref]$rect) | Out-Null\n"
        "  $w = $rect.R - $rect.L\n"
        "  $ht = $rect.B - $rect.T\n"
        "  [WP]::MoveWindow($h, $x, $y, $w, $ht, $true) | Out-Null\n"
        "}\n"
    )

    _powershell(script)

    return f"Moved {app} window to ({x}, {y})"


@mcp.tool()
@tool_safe
def resize_window(
    app: str,
    width: int,
    height: int,
    title: str | None = None
) -> str:
    """Resize a window of an app to (width, height) in pixels.

    Resizes the front window unless `title` is given (must match list_windows output).
    """

    if width < 1 or height < 1:
        raise ValueError("width and height must be >= 1.")

    where = (
        f"Where-Object {{ $_.MainWindowTitle -eq '{_ps_quote(title)}' }}"
        if title
        else "Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1"
    )

    script = (
        "$code = @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WR {\n"
        "  [DllImport(\"user32.dll\")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int h2, bool r);\n"
        "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
        "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L; public int T; public int R; public int B; }\n"
        "}\n"
        "'@\n"
        "Add-Type $code\n"
        f"$p = Get-Process -Name '{_ps_quote(app)}' -ErrorAction Stop | {where};\n"
        "if ($p) {\n"
        "  $h = $p.MainWindowHandle\n"
        "  $rect = New-Object WR+RECT\n"
        "  [WR]::GetWindowRect($h, [ref]$rect) | Out-Null\n"
        "  [WR]::MoveWindow($h, $rect.L, $rect.T, $width, $height, $true) | Out-Null\n"
        "}\n"
    )

    _powershell(script)

    return f"Resized {app} window to {width}x{height}"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@mcp.tool()
@tool_safe
def check_permissions() -> dict:
    """Check that this server can run on Windows.

    On Windows, pyautogui works without special permissions as long as the
    server runs as a regular desktop user (not sandboxed/UWP).
    """

    return {
        "accessibility": True,
        "screen_recording": True,
        "note": "Windows: no special permissions needed. Server must run as a regular desktop user.",
    }


# ---------------------------------------------------------------------------
# Auth middleware + entrypoint
# ---------------------------------------------------------------------------


class _AuthMiddleware:
    """Pure-ASGI middleware: reject requests without `Authorization: Bearer <MCP_AUTH_TOKEN>`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and AUTH_TOKEN:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            expected = b"Bearer " + AUTH_TOKEN.encode()
            if not hmac.compare_digest(auth, expected):
                body = b"401 Unauthorized"
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main() -> None:
    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=HOST)
    app.add_middleware(_AuthMiddleware)

    logger.info(
        "Desktop MCP listening on http://%s:%s%s%s",
        HOST,
        PORT,
        MCP_PATH,
        " (auth: Bearer token required)" if AUTH_TOKEN else " (WARNING: no auth token - set MCP_AUTH_TOKEN)",
    )

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()