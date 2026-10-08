"""Safe Windows desktop-control tools for AURA AI.

This module intentionally does not run arbitrary shell commands.
Only allowlisted applications and explicit desktop actions are supported.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


class LaptopActionError(Exception):
    """A requested desktop action could not be safely performed."""


APP_ALIASES = {
    # Browsers
    "chrome": "chrome",
    "google chrome": "chrome",
    "browser": "browser",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",

    # Windows built-in apps
    "notepad": "notepad",
    "calculator": "calculator",
    "calc": "calculator",
    "paint": "paint",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "camera": "camera",
    "windows camera": "camera",
    "photos": "photos",
    "clock": "clock",
    "calendar": "calendar",
    "mail": "mail",
    "settings": "settings",
    "task manager": "task_manager",
    "snipping tool": "snipping_tool",
    "media player": "media_player",
    "windows media player": "media_player",

    # Developer tools
    "vscode": "vscode",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "visual studio": "visual_studio",
    "terminal": "terminal",
    "windows terminal": "terminal",
    "powershell": "powershell",
    "command prompt": "cmd",
    "cmd": "cmd",
    "postman": "postman",
    "github desktop": "github_desktop",

    # Microsoft Office
    "word": "word",
    "microsoft word": "word",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpoint",
    "microsoft powerpoint": "powerpoint",
    "outlook": "outlook",

    # Common desktop apps
    "teams": "teams",
    "microsoft teams": "teams",
    "whatsapp": "whatsapp",
    "spotify": "spotify",
    "discord": "discord",
    "zoom": "zoom",
    "telegram": "telegram",
    "obs": "obs",
    "obs studio": "obs",
}

KEY_ALIASES = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "esc",
    "esc": "esc",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}


def _pyautogui():
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.15
        return pyautogui
    except Exception as exc:
        raise LaptopActionError(
            "Desktop control dependency missing. Install with: "
            "python -m pip install pyautogui pillow"
        ) from exc


def _window_tools():
    try:
        import pygetwindow as gw
        return gw
    except Exception as exc:
        raise LaptopActionError(
            "Window targeting needs PyGetWindow. Install with: "
            "python -m pip install pygetwindow"
        ) from exc


def _window_title(win: Any) -> str:
    try:
        return (win.title or "").strip()
    except Exception:
        return ""


def _find_window(app: str):
    """Find a visible window whose title contains the requested app name."""
    gw = _window_tools()
    requested = (app or "").strip().casefold()

    if not requested:
        raise LaptopActionError("Provide an app name, e.g. Notepad.")

    # Common Windows title variations.
    aliases = {
        "notepad": ["notepad"],
        "chrome": ["chrome"],
        "google chrome": ["chrome"],
        "edge": ["edge"],
        "microsoft edge": ["edge"],
        "vscode": ["visual studio code", "vs code"],
        "vs code": ["visual studio code", "vs code"],
        "visual studio code": ["visual studio code", "vs code"],
        "paint": ["paint"],
        "calculator": ["calculator"],
        "calc": ["calculator"],
        "explorer": ["file explorer"],
        "file explorer": ["file explorer"],
        "files": ["file explorer"],
    }

    wanted = aliases.get(requested, [requested])

    windows = []
    try:
        windows = gw.getAllWindows()
    except Exception:
        pass

    candidates = []
    for win in windows:
        title = _window_title(win)
        if not title:
            continue

        title_low = title.casefold()
        if any(term in title_low for term in wanted):
            candidates.append(win)

    if not candidates:
        raise LaptopActionError(
            f"No open window found matching '{app}'. "
            "Open the application first."
        )

    # Prefer a non-minimized window.
    for win in candidates:
        try:
            if not win.isMinimized:
                return win
        except Exception:
            pass

    return candidates[0]


def _activate_window(win: Any, app: str) -> str:
    """Restore and activate a Windows window, then return its title."""
    pg = _pyautogui()

    try:
        if getattr(win, "isMinimized", False):
            win.restore()
            time.sleep(0.4)

        win.activate()
        time.sleep(0.8)

        # Windows can sometimes leave another application focused for a
        # moment. Clicking a safe point inside the target window makes the
        # target receive keyboard focus.
        left = int(win.left)
        top = int(win.top)
        width = max(200, int(win.width))
        height = max(150, int(win.height))

        # For Notepad/VS Code/browser, this point is normally inside the
        # document/content area rather than the title bar.
        click_x = left + max(40, min(width // 2, width - 40))
        click_y = top + max(120, min(int(height * 0.45), height - 40))

        screen_w, screen_h = pg.size()
        if 0 <= click_x < screen_w and 0 <= click_y < screen_h:
            pg.click(click_x, click_y)

        time.sleep(0.3)

        title = _window_title(win)
        if not title:
            title = app

        return title

    except Exception as exc:
        raise LaptopActionError(
            f"Could not activate '{app}': {exc}"
        ) from exc


def get_active_window_title() -> str:
    """Return the currently active Windows window title when available."""
    try:
        gw = _window_tools()
        win = gw.getActiveWindow()
        return _window_title(win) if win else ""
    except Exception:
        return ""


def get_laptop_status() -> dict[str, Any]:
    pg = _pyautogui()
    width, height = pg.size()

    return {
        "status": "ready",
        "screen": {"width": width, "height": height},
        "active_window": get_active_window_title(),
        "supported_apps": sorted(APP_ALIASES.keys()),
        "supported_actions": [
            "open_app",
            "open_url",
            "screenshot",
            "click",
            "double_click",
            "move_mouse",
            "type_text",
            "press_key",
            "hotkey",
            "scroll",
        ],
        "safety": (
            "Allowlisted app launching; no arbitrary shell commands; "
            "PyAutoGUI failsafe enabled."
        ),
    }


def _launch_app(app: str) -> str:
    """Launch a safe, explicitly allowlisted Windows application."""
    key = (app or "").strip().casefold()

    if key not in APP_ALIASES:
        allowed = ", ".join(sorted(APP_ALIASES))
        raise LaptopActionError(
            f"App '{app}' is not allowlisted yet. "
            f"Supported apps: {allowed}."
        )

    target = APP_ALIASES[key]

    try:
        if target == "browser":
            webbrowser.open("https://www.google.com")

        elif target == "chrome":
            subprocess.Popen(["cmd", "/c", "start", "", "chrome"], shell=False)

        elif target == "edge":
            subprocess.Popen(["cmd", "/c", "start", "", "msedge"], shell=False)

        elif target == "firefox":
            subprocess.Popen(["cmd", "/c", "start", "", "firefox"], shell=False)

        # Windows built-in applications
        elif target == "notepad":
            subprocess.Popen(["notepad.exe"], shell=False)

        elif target == "paint":
            subprocess.Popen(["mspaint.exe"], shell=False)

        elif target == "explorer":
            subprocess.Popen(["explorer.exe"], shell=False)

        elif target == "calculator":
            os.startfile("calculator:")

        elif target == "camera":
            os.startfile("microsoft.windows.camera:")

        elif target == "photos":
            os.startfile("ms-photos:")

        elif target == "clock":
            os.startfile("ms-clock:")

        elif target == "calendar":
            os.startfile("outlookcal:")

        elif target == "mail":
            os.startfile("mailto:")

        elif target == "settings":
            os.startfile("ms-settings:")

        elif target == "snipping_tool":
            subprocess.Popen(["SnippingTool.exe"], shell=False)

        elif target == "media_player":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "mswindowsmusic:"],
                shell=False,
            )

        elif target == "task_manager":
            subprocess.Popen(["taskmgr.exe"], shell=False)

        # Developer applications
        elif target == "vscode":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "code"],
                shell=False,
            )

        elif target == "visual_studio":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "devenv"],
                shell=False,
            )

        elif target == "terminal":
            subprocess.Popen(["wt.exe"], shell=False)

        elif target == "powershell":
            subprocess.Popen(["powershell.exe"], shell=False)

        elif target == "cmd":
            subprocess.Popen(["cmd.exe"], shell=False)

        elif target == "postman":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "postman"],
                shell=False,
            )

        elif target == "github_desktop":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "github"],
                shell=False,
            )

        # Microsoft Office
        elif target == "word":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "winword"],
                shell=False,
            )

        elif target == "excel":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "excel"],
                shell=False,
            )

        elif target == "powerpoint":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "powerpnt"],
                shell=False,
            )

        elif target == "outlook":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "outlook"],
                shell=False,
            )

        # Common apps
        elif target == "teams":
            os.startfile("ms-teams:")

        elif target == "whatsapp":
            os.startfile("whatsapp:")

        elif target == "spotify":
            os.startfile("spotify:")

        elif target == "discord":
            os.startfile("discord:")

        elif target == "zoom":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "zoom"],
                shell=False,
            )

        elif target == "telegram":
            os.startfile("tg:")

        elif target == "obs":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "obs64"],
                shell=False,
            )

        return f"Launch request sent for {app}."

    except Exception as exc:
        raise LaptopActionError(
            f"Could not open {app}. Make sure it is installed."
        ) from exc


def _parse_keys(key: str) -> list[str]:
    raw_parts = [
        part.strip().casefold()
        for part in re.split(r"\s*\+\s*", key.strip())
        if part.strip()
    ]

    if not raw_parts:
        raise LaptopActionError(
            "Tell me which key or key combination to press, e.g. Ctrl+S."
        )

    result = []

    for part in raw_parts:
        mapped = KEY_ALIASES.get(part, part)

        if mapped not in KEY_ALIASES.values() and not (
            len(mapped) == 1 and mapped.isalnum()
        ):
            raise LaptopActionError(f"Unsupported key: {part}")

        result.append(mapped)

    return result


def perform_laptop_action(
    action: str,
    text: str | None = None,
    app: str | None = None,
    x: int | None = None,
    y: int | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
) -> dict[str, Any]:

    action = (
        action.strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )

    if action == "open_app":
        if not app:
            raise LaptopActionError(
                "Provide an app name, e.g. Chrome or Notepad."
            )

        message = _launch_app(app)
        time.sleep(1.2)

        return {
            "ok": True,
            "action": action,
            "app": app,
            "message": message,
            "active_window": get_active_window_title(),
        }

    if action == "open_url":
        url = (text or "").strip()

        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise LaptopActionError(
                "URL must start with http:// or https://"
            )

        webbrowser.open(url)

        return {
            "ok": True,
            "action": action,
            "url": url,
            "message": f"Opened URL: {url}",
        }

    if action == "screenshot":
        pg = _pyautogui()

        folder = Path.home() / "Pictures" / "AURA_Screenshots"
        folder.mkdir(parents=True, exist_ok=True)

        filename = f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
        target = folder / filename

        try:
            pg.screenshot().save(target)
        except Exception as exc:
            raise LaptopActionError(
                "Could not capture the screen. Check Windows permissions."
            ) from exc

        return {
            "ok": True,
            "action": action,
            "path": str(target),
            "message": "Screenshot saved.",
        }

    pg = _pyautogui()
    width, height = pg.size()

    if action in {"click", "double_click", "move_mouse"}:
        if x is None or y is None:
            raise LaptopActionError(
                "Provide screen coordinates x and y, e.g. x=500, y=300."
            )

        if not (0 <= x < width and 0 <= y < height):
            raise LaptopActionError(
                f"Coordinates must be inside your screen "
                f"(0..{width - 1}, 0..{height - 1})."
            )

        if action == "click":
            pg.click(x=x, y=y)
        elif action == "double_click":
            pg.doubleClick(x=x, y=y)
        else:
            pg.moveTo(x=x, y=y, duration=0.2)

        return {
            "ok": True,
            "action": action,
            "x": x,
            "y": y,
            "message": f"{action.replace('_', ' ').title()} completed.",
        }

    if action == "type_text":
        if text is None:
            raise LaptopActionError("Provide the exact text to type.")

        if not text:
            raise LaptopActionError("Text cannot be empty.")

        target_title = get_active_window_title()

        # If an app is provided, activate it or launch it if it is not open.
        if app:
            try:
                win = _find_window(app)
            except LaptopActionError as exc:
                if not str(exc).startswith("No open window found matching"):
                    raise

                _launch_app(app)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        win = _find_window(app)
                        break
                    except LaptopActionError as window_error:
                        if not str(window_error).startswith(
                            "No open window found matching"
                        ):
                            raise
                        if time.monotonic() >= deadline:
                            raise LaptopActionError(
                                f"Opened {app}, but its window did not become available."
                            ) from window_error
                        time.sleep(0.25)

            target_title = _activate_window(win, app)

        # Give Windows a short moment to transfer keyboard focus.
        time.sleep(0.25)

        pg.write(text, interval=0.015)

        return {
            "ok": True,
            "action": action,
            "target_app": app or None,
            "target_window": target_title or "unknown",
            "characters_typed": len(text),
            "message": (
                f"Typed {len(text)} characters into "
                f"'{target_title or 'the active window'}'."
            ),
        }

    if action == "press_key":
        if not key:
            raise LaptopActionError(
                "Provide a key name, e.g. Enter or Escape."
            )

        keys = _parse_keys(key)

        if len(keys) != 1:
            raise LaptopActionError(
                "Use press_key for one key; use hotkey for combinations like Ctrl+S."
            )

        pg.press(keys[0])

        return {
            "ok": True,
            "action": action,
            "key": keys[0],
            "active_window": get_active_window_title(),
            "message": f"Pressed {keys[0]}",
        }

    if action == "hotkey":
        if not key:
            raise LaptopActionError(
                "Provide a key combination, e.g. Ctrl+S."
            )

        keys = _parse_keys(key)

        if len(keys) < 2:
            raise LaptopActionError(
                "Hotkey needs at least two keys, e.g. Ctrl+S."
            )

        pg.hotkey(*keys)

        return {
            "ok": True,
            "action": action,
            "keys": keys,
            "active_window": get_active_window_title(),
            "message": f"Pressed {'+'.join(keys)}",
        }

    if action == "scroll":
        scroll_direction = (direction or "").casefold()

        if scroll_direction not in {"up", "down"}:
            raise LaptopActionError(
                "Scroll direction must be 'up' or 'down'."
            )

        ticks = max(1, min(int(amount or 3), 10))
        pg.scroll(ticks if scroll_direction == "up" else -ticks)

        return {
            "ok": True,
            "action": action,
            "direction": scroll_direction,
            "amount": ticks,
            "active_window": get_active_window_title(),
            "message": f"Scrolled {scroll_direction}.",
        }

    raise LaptopActionError(
        "Unsupported action. Use open_app, open_url, screenshot, click, "
        "double_click, move_mouse, type_text, press_key, hotkey, or scroll."
    )


def parse_laptop_command(message: str) -> dict[str, Any] | None:
    """Parse only clear, explicitly phrased desktop commands."""

    raw = message.strip()
    low = raw.casefold()

    # Screenshot
    if any(
        p in low
        for p in [
            "take screenshot",
            "take a screenshot",
            "screenshot lo",
            "screen shot",
            "capture screen",
            "screen capture",
        ]
    ):
        return {"action": "screenshot"}

    # Open a known app
    app_pattern = (
        r"chrome|google chrome|browser|edge|microsoft edge|firefox|"
        r"notepad|calculator|calc|paint|explorer|file explorer|files|"
        r"camera|windows camera|photos|clock|calendar|mail|settings|"
        r"task manager|snipping tool|media player|windows media player|"
        r"vscode|vs code|visual studio code|visual studio|"
        r"terminal|windows terminal|powershell|command prompt|cmd|"
        r"postman|github desktop|"
        r"word|microsoft word|excel|microsoft excel|"
        r"powerpoint|microsoft powerpoint|outlook|"
        r"teams|microsoft teams|whatsapp|spotify|discord|zoom|"
        r"telegram|obs|obs studio"
    )

    open_match = re.search(
        rf"\b(?:open|launch|start|kholo|khol|chalao)\s+({app_pattern})\b",
        low,
    )

    if not open_match:
        open_match = re.search(
            rf"\b({app_pattern})\s+(?:open|kholo|khol|chalao)\b",
            low,
        )

    if open_match:
        candidate = open_match.group(1)

        if candidate not in APP_ALIASES:
            candidate = next(
                (
                    alias
                    for alias in APP_ALIASES
                    if re.search(rf"\b{re.escape(alias)}\b", low)
                ),
                candidate,
            )

        return {"action": "open_app", "app": candidate}

    # Explicit coordinate mouse commands
    coord = re.search(
        r"\b(click|double\s+click|move\s+mouse|mouse\s+ko\s+le\s+jao)"
        r"\s+(?:at\s+)?(?:x\s*[:=]?\s*)?(\d{1,5})"
        r"\s*[, ]\s*(?:y\s*[:=]?\s*)?(\d{1,5})\b",
        low,
    )

    if coord:
        verb = coord.group(1)
        action = (
            "double_click"
            if "double" in verb
            else "move_mouse"
            if "move" in verb
            else "click"
        )

        return {
            "action": action,
            "x": int(coord.group(2)),
            "y": int(coord.group(3)),
        }

    # Text directed to a known app, e.g. "type hello in notepad".
    typed_into_app = re.search(
        r"^\s*(?:type\s+karo|type|write|likho)\s+(.+?)\s+(?:in|into)\s+(.+?)\s*$",
        raw,
        re.IGNORECASE,
    )
    if typed_into_app:
        app = typed_into_app.group(2).strip().casefold()
        if app in APP_ALIASES:
            return {
                "action": "type_text",
                "text": typed_into_app.group(1).strip(),
                "app": app,
            }

    # Explicit text command
    type_match = re.search(
        r'^\s*(?:type\s+karo|type|write|likho)\s*[:"]\s*(.+?)["]?\s*$',
        raw,
        re.IGNORECASE,
    )

    if type_match:
        return {
            "action": "type_text",
            "text": type_match.group(1).strip().strip('"'),
        }

    # Key / hotkey
    key_match = re.search(
        r"\b(?:press|dabao|key press karo)\s+"
        r"(ctrl\s*\+\s*[a-z0-9]+|control\s*\+\s*[a-z0-9]+|"
        r"alt\s*\+\s*[a-z0-9]+|shift\s*\+\s*[a-z0-9]+|"
        r"enter|escape|esc|tab|space|backspace|delete|up|down|"
        r"left|right|f(?:[1-9]|1[0-2]))\b",
        low,
    )

    if key_match:
        key = re.sub(r"\s*\+\s*", "+", key_match.group(1))

        if "+" in key:
            return {"action": "hotkey", "key": key}

        return {"action": "press_key", "key": key}

    if re.search(r"\bscroll\s+up\b|\bpage upar\b|\bscroll upar\b", low):
        return {"action": "scroll", "direction": "up", "amount": 3}

    if re.search(
        r"\bscroll\s+down\b|\bpage neeche\b|\bscroll neeche\b",
        low,
    ):
        return {"action": "scroll", "direction": "down", "amount": 3}

    url_match = re.search(
        r"\b(?:open|visit|kholo)\s+(https?://\S+)",
        raw,
        re.IGNORECASE,
    )

    if url_match:
        return {
            "action": "open_url",
            "text": url_match.group(1).rstrip(".,)"),
        }

    return None
