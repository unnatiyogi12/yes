
"""AURA AI - Terminal Desktop Assistant.

Voice is disabled for now.
Use natural-language text commands directly in the terminal.
"""

from __future__ import annotations

import os
import re
import sys
import time
import webbrowser
import urllib.parse
import subprocess
from pathlib import Path
from typing import Any

import requests

from app.laptop_control import (
    LaptopActionError,
    perform_laptop_action,
    parse_laptop_command,
)
from app.github_control import (
    GitHubActionError,
    github_auth_status,
    validate_project,
    init_git_repo,
    get_repo_name_suggestion,
    normalize_repo_name,
    git_add,
    git_commit,
    create_github_repo,
    git_push,
    open_repo,
)

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:3b"

WEBSITES = {
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "leetcode": "https://leetcode.com",
    "whatsapp web": "https://web.whatsapp.com",
    "youtube": "https://youtube.com",
    "linkedin": "https://linkedin.com",
    "chatgpt": "https://chatgpt.com",
    "google": "https://google.com",
    "stackoverflow": "https://stackoverflow.com",
    "instagram": "https://instagram.com",
    "discord": "https://discord.com/app",
    "spotify": "https://open.spotify.com",
}

# Common locations used when searching by folder/file name.
SEARCH_ROOTS = [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "OneDrive",
    Path.home() / "OneDrive" / "Desktop",
    Path.home() / "OneDrive" / "Documents",
    Path.home() / "OneDrive" / "Downloads",
]

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "AppData", "ProgramData", "$Recycle.Bin", "System Volume Information",
}

EXIT_WORDS = {"exit", "quit", "close aura", "stop aura", "bye"}

PENDING_WHATSAPP: dict[str, Any] | None = None


def say(msg: str) -> None:
    print(f"\nAURA > {msg}")


def ask_yes_no(question: str) -> bool:
    answer = input(f"\nAURA > {question} [yes/no]: ").strip().casefold()
    return answer in {"yes", "y", "haan", "ha", "ok", "okay", "do it", "proceed"}


def open_url(url: str) -> None:
    webbrowser.open(url)


def open_website(name: str) -> None:
    key = name.casefold().strip()
    url = WEBSITES[key]
    open_url(url)
    say(f"Opened {name}.")


def open_whatsapp_desktop() -> bool:
    """Open the installed WhatsApp Desktop app, never WhatsApp Web."""
    try:
        os.startfile("whatsapp:")
        return True
    except Exception:
        try:
            subprocess.Popen(["explorer.exe", "whatsapp:"], shell=False)
            return True
        except Exception as exc:
            say(f"Could not open WhatsApp Desktop: {exc}")
            return False


def _search_named_path(name: str, want: str = "any") -> list[Path]:
    """Search common user folders for a file/folder by name."""
    target = name.strip().strip('"').strip("'")
    if not target:
        return []

    # Direct relative path from current directory first.
    direct = Path(target).expanduser()
    if direct.exists():
        if want == "folder" and direct.is_dir():
            return [direct.resolve()]
        if want == "file" and direct.is_file():
            return [direct.resolve()]
        if want == "any":
            return [direct.resolve()]

    # If the user supplied a full path, do not perform a huge search.
    if Path(target).drive or target.startswith("\\"):
        return []

    wanted = target.casefold()
    results: list[Path] = []
    seen: set[str] = set()

    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        try:
            for current, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

                if want in {"folder", "any"}:
                    for d in dirs:
                        if d.casefold() == wanted:
                            p = (Path(current) / d).resolve()
                            key = str(p).casefold()
                            if key not in seen:
                                seen.add(key)
                                results.append(p)

                if want in {"file", "any"}:
                    for f in files:
                        if f.casefold() == wanted:
                            p = (Path(current) / f).resolve()
                            key = str(p).casefold()
                            if key not in seen:
                                seen.add(key)
                                results.append(p)

                if len(results) >= 20:
                    return results
        except (PermissionError, OSError):
            continue

    return results


def choose_path(results: list[Path], description: str) -> Path | None:
    if not results:
        say(f"I could not find {description} in your common folders.")
        return None

    if len(results) == 1:
        return results[0]

    say(f"I found {len(results)} matches for {description}:")
    for i, path in enumerate(results[:10], 1):
        print(f"  {i}. {path}")

    while True:
        choice = input("AURA > Choose a number (or 'cancel') > ").strip().casefold()
        if choice == "cancel":
            return None
        if choice.isdigit() and 1 <= int(choice) <= min(len(results), 10):
            return results[int(choice) - 1]
        say("Please enter one of the listed numbers.")


def open_named_path(command: str) -> bool:
    """Open a file/folder by name without requiring its full path."""
    text = command.strip()
    low = text.casefold()

    if not re.search(r"^(?:open|launch|show)\b", low):
        return False

    if " in vs code" in low or " in vscode" in low or " in visual studio code" in low:
        return False

    m = re.match(r"^(?:open|launch|show)\s+(?:the\s+)?(?:folder\s+)?(.+?)\s*$", text, re.I)
    if not m:
        return False

    target = m.group(1).strip().strip('"').strip("'")
    if target.casefold() in WEBSITES:
        return False
    if target.casefold() in {"whatsapp", "whatsapp web"}:
        return False

    # Full path / explicit PDF/file path.
    expanded = os.path.expandvars(target)
    if os.path.isfile(expanded):
        os.startfile(expanded)
        say(f"Opened file: {expanded}")
        return True
    if os.path.isdir(expanded):
        os.startfile(expanded)
        say(f"Opened folder: {expanded}")
        return True

    # Exact name search: folder first, then file.
    results = _search_named_path(target, "folder")
    if not results:
        results = _search_named_path(target, "file")

    path = choose_path(results, target)
    if not path:
        return True

    os.startfile(str(path))
    say(f"Opened: {path}")
    return True


def open_named_folder_in_vscode(command: str) -> bool:
    """Find a folder by name and open it in VS Code."""
    text = command.strip()
    m = re.match(
        r"^(?:open|launch)\s+(?:the\s+)?(.+?)\s+(?:folder\s+)?in\s+(?:vs\s*code|vscode|visual studio code)\s*$",
        text,
        re.I,
    )
    if not m:
        return False

    target = m.group(1).strip().strip('"').strip("'")
    expanded = os.path.expandvars(target)

    if os.path.isdir(expanded):
        path = Path(expanded).resolve()
    else:
        results = _search_named_path(target, "folder")
        path = choose_path(results, target)
        if not path:
            return True

    try:
        subprocess.Popen(["code", str(path)], shell=False)
        say(f"Opened in VS Code: {path}")
    except FileNotFoundError:
        say("VS Code 'code' command is not in PATH. Run 'code --version' to check it.")
    except Exception as exc:
        say(f"Could not open VS Code: {exc}")
    return True


def find_project_from_text(text: str) -> str | None:
    """
    Accept:
      push C:\\path\\project to github
      push "C:\\path\\my project" to github
    """
    quoted = re.search(r'"([A-Za-z]:\\[^"]+)"', text)
    if quoted and os.path.isdir(quoted.group(1)):
        return os.path.abspath(quoted.group(1))

    path_match = re.search(r'([A-Za-z]:\\.+?)(?=\s+(?:to|on|into|for)\s+github\b|$)', text, re.I)
    if path_match:
        candidate = path_match.group(1).rstrip(" .")
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)

    # "this project" / "my project" -> current terminal directory if it looks like a project.
    if re.search(r"\b(this|current|my)\s+project\b", text, re.I):
        cwd = os.getcwd()
        if os.path.isdir(os.path.join(cwd, ".git")) or any(
            os.path.exists(os.path.join(cwd, name))
            for name in ("package.json", "requirements.txt", "pyproject.toml", "src", "app")
        ):
            return cwd

    # Otherwise ask the user for the path instead of guessing.
    return None


def github_push_workflow(command: str) -> None:
    path = find_project_from_text(command)

    if not path:
        # Try to extract a project name from commands like
        # "push CivicFix to github" or "push my Aura AI project to github".
        m = re.search(r"\bpush\s+(?:my\s+)?(.+?)\s+to\s+github\b", command, re.I)
        if m:
            project_name = re.sub(r"\b(?:project|folder|repo|repository)\b", "", m.group(1), flags=re.I).strip()
            matches = _search_named_path(project_name, "folder")
            path_obj = choose_path(matches, project_name) if matches else None
            if path_obj:
                path = str(path_obj)

    if not path:
        say("Which project folder should I push?")
        path = input("Project path or project name > ").strip().strip('"')
        if not path:
            say("Cancelled.")
            return
        if not os.path.isdir(path):
            matches = _search_named_path(path, "folder")
            path_obj = choose_path(matches, path)
            if path_obj:
                path = str(path_obj)

    if not os.path.isdir(path):
        say(f"Project folder does not exist: {path}")
        return

    auth = github_auth_status()
    if not auth.get("installed"):
        say("GitHub CLI (gh) is not installed. Install GitHub CLI first.")
        return

    if not auth.get("authenticated"):
        say("GitHub is not authenticated.")
        say("Run: gh auth login")
        return

    try:
        info = validate_project(path)
    except GitHubActionError as exc:
        say(str(exc))
        return

    project_path = info.get("git_root") or info.get("path") or path

    if not info.get("is_git_repo"):
        if not ask_yes_no(f"Initialize Git in {project_path}?"):
            say("Cancelled.")
            return

        try:
            init_git_repo(project_path)
        except GitHubActionError as exc:
            say(str(exc))
            return

        info = validate_project(project_path)

    suggestion = get_repo_name_suggestion(project_path)
    repo_name = input(
        f"\nAURA > Repository name [{suggestion}] > "
    ).strip()

    if not repo_name:
        repo_name = suggestion

    try:
        repo_name = normalize_repo_name(repo_name)
    except Exception as exc:
        say(f"Invalid repository name: {exc}")
        return

    visibility = input(
        "AURA > Visibility [private/public] (default private) > "
    ).strip().casefold() or "private"

    if visibility not in {"private", "public"}:
        say("Please choose private or public.")
        return

    # Show a safety summary before staging.
    say(
        f"GitHub push plan:\n"
        f"  Project: {project_path}\n"
        f"  Repository: {repo_name}\n"
        f"  Visibility: {visibility}"
    )

    if not ask_yes_no("Proceed with git add?"):
        say("Cancelled before staging.")
        return

    try:
        add_result = git_add(project_path)
    except GitHubActionError as exc:
        say(f"git add blocked/failed: {exc}")
        return

    say("Files staged successfully.")

    commit_message = input(
        "\nAURA > Commit message [Initial commit] > "
    ).strip() or "Initial commit"

    if not ask_yes_no(f"Create commit '{commit_message}'?"):
        say("Cancelled before commit.")
        return

    try:
        git_commit(project_path, commit_message)
    except GitHubActionError as exc:
        say(f"Commit failed: {exc}")
        return

    if not ask_yes_no(
        f"Create GitHub repository '{repo_name}' as {visibility}?"
    ):
        say("Cancelled before repository creation.")
        return

    try:
        create_github_repo(project_path, repo_name, visibility)
    except GitHubActionError as exc:
        say(f"GitHub repository creation failed: {exc}")
        return

    if not ask_yes_no("Push the committed code to GitHub now?"):
        say("Repository was created, but code was not pushed.")
        return

    try:
        git_push(project_path)
    except GitHubActionError as exc:
        say(f"Push failed: {exc}")
        return

    say("Code pushed successfully.")

    if ask_yes_no("Open the GitHub repository in Chrome?"):
        try:
            open_repo(project_path)
        except Exception:
            repo_url = f"https://github.com/{repo_name}"
            open_url(repo_url)

    say("GitHub workflow completed.")


def parse_gmail_command(command: str) -> dict[str, str] | None:
    """
    Examples:
      compose mail to abc@gmail.com subject Hello body How are you
      compose email to abc@gmail.com
    """
    m = re.search(
        r"\b(?:compose|write|draft)\s+(?:a\s+)?(?:mail|email)\s+"
        r"(?:to\s+)?(?P<to>\S+)"
        r"(?:\s+subject\s+(?P<subject>.*?))?"
        r"(?:\s+body\s+(?P<body>.*))?$",
        command,
        re.I,
    )
    if not m:
        return None

    return {
        "to": m.group("to") or "",
        "subject": (m.group("subject") or "").strip(),
        "body": (m.group("body") or "").strip(),
    }


def compose_gmail(data: dict[str, str]) -> None:
    params = {
        "view": "cm",
        "fs": "1",
        "to": data["to"],
        "su": data["subject"],
        "body": data["body"],
    }
    url = "https://mail.google.com/mail/u/0/?" + urllib.parse.urlencode(params)
    open_url(url)

    say(
        f"Gmail compose opened.\n"
        f"To: {data['to']}\n"
        f"Subject: {data['subject'] or '(empty)'}\n"
        f"Body: {data['body'] or '(empty)'}\n\n"
        "I will NOT click Send automatically."
    )


def parse_whatsapp_command(command: str) -> dict[str, str] | None:
    """Parse WhatsApp commands into a contact name and message."""
    command = command.strip()

    patterns = [
        # send whatsapp message to Rahul saying Hello
        r"^\s*(?:send|message|msg)\s+(?:a\s+)?whatsapp\s+(?:message\s+)?(?:to\s+)?"
        r"(?P<contact>.+?)\s+(?:saying|that\s+says|with\s+message)\s+(?P<message>.+?)\s*$",

        # send whatsapp message to Rahul on whatsapp, Hello
        r"^\s*(?:send|message|msg)\s+(?:a\s+)?whatsapp\s+(?:message\s+)?(?:to\s+)?"
        r"(?P<contact>.+?)\s+on\s+whatsapp\s*[,\:\-]\s*(?P<message>.+?)\s*$",

        # send whatsapp message to Rahul, Hello
        r"^\s*(?:send|message|msg)\s+(?:a\s+)?whatsapp\s+(?:message\s+)?(?:to\s+)?"
        r"(?P<contact>.+?)\s*[,\:\-]\s*(?P<message>.+?)\s*$",

        # whatsapp message to Rahul: Hello
        r"^\s*whatsapp\s+(?:message\s+)?(?:to\s+)?"
        r"(?P<contact>.+?)\s*[,\:\-]\s*(?P<message>.+?)\s*$",

        # message Rahul on whatsapp saying Hello
        r"^\s*(?:message|msg)\s+(?P<contact>.+?)\s+on\s+whatsapp\s+"
        r"(?:saying|that\s+says|with\s+message)\s+(?P<message>.+?)\s*$",
    ]

    for pattern in patterns:
        match = re.search(pattern, command, re.I)
        if match:
            contact = re.sub(r"\s+on\s+whatsapp\s*$", "", match.group("contact"), flags=re.I).strip()
            message = match.group("message").strip()
            if contact and message:
                return {"contact": contact, "message": message}

    return None


def _get_whatsapp_window():
    """Return the native WhatsApp window handle, if available."""
    import ctypes

    user32 = ctypes.windll.user32
    for title in ("WhatsApp", "WhatsApp Beta"):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return user32, hwnd
    return user32, None


def _focus_whatsapp() -> bool:
    """Bring WhatsApp Desktop to the foreground after terminal confirmation."""
    import ctypes

    try:
        user32, hwnd = _get_whatsapp_window()
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.8)
            return True

        import pyautogui
        pyautogui.hotkey("alt", "tab")
        time.sleep(0.8)
        return True
    except Exception:
        return False


def _whatsapp_window_rect():
    """Return (left, top, right, bottom) for WhatsApp Desktop."""
    user32, hwnd = _get_whatsapp_window()
    if not hwnd:
        return None

    import ctypes
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def _find_whatsapp_send_button(pyautogui):
    """Find the green Send button, preferring the lower-right part of WhatsApp."""
    from collections import deque

    image = pyautogui.screenshot().convert("RGB")
    width, height = image.size

    rect = _whatsapp_window_rect()
    if rect:
        left, top, right, bottom = rect
        x0 = max(0, int(left + (right - left) * 0.70))
        x1 = min(width, int(left + (right - left) * 0.99))
        y0 = max(0, int(top + (bottom - top) * 0.78))
        y1 = min(height, int(top + (bottom - top) * 0.99))
    else:
        x0 = int(width * 0.70)
        x1 = int(width * 0.995)
        y0 = int(height * 0.70)
        y1 = int(height * 0.98)

    step = 2
    green = set()
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            r, g, b = image.getpixel((x, y))
            if g >= 120 and g > r * 1.35 and g > b * 1.15:
                green.add((x, y))

    if not green:
        return None

    remaining = green.copy()
    components = []
    while remaining:
        seed = remaining.pop()
        queue = deque([seed])
        points = [seed]
        while queue:
            x, y = queue.popleft()
            for nx, ny in ((x + step, y), (x - step, y), (x, y + step), (x, y - step)):
                if (nx, ny) in remaining:
                    remaining.remove((nx, ny))
                    queue.append((nx, ny))
                    points.append((nx, ny))

        if len(points) < 20:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        bw, bh = max_x - min_x, max_y - min_y
        if 18 <= bw <= 120 and 18 <= bh <= 120:
            aspect = bw / max(1, bh)
            if 0.65 <= aspect <= 1.5:
                cx, cy = (min_x + max_x) // 2, (min_y + max_y) // 2
                components.append((cx, cy, len(points)))

    if not components:
        return None

    # The Send button is the candidate closest to the bottom-right corner.
    return max(components, key=lambda p: (p[1], p[0]))[:2]


def _click_whatsapp_composer(pyautogui) -> bool:
    """Click the message composer using the WhatsApp window geometry."""
    rect = _whatsapp_window_rect()
    if not rect:
        return False

    left, top, right, bottom = rect
    width = right - left
    height = bottom - top

    # Message composer is near the bottom of a normal WhatsApp Desktop window.
    x = int(left + width * 0.52)
    y = int(top + height * 0.90)
    pyautogui.click(x, y)
    time.sleep(0.25)
    return True


def _send_whatsapp_message() -> bool:
    """Actually send the prepared WhatsApp message and verify the composer clears."""
    try:
        import pyautogui
    except Exception as exc:
        raise RuntimeError("pyautogui is required for WhatsApp sending.") from exc

    if not _focus_whatsapp():
        return False

    # The terminal has focus after the yes/no prompt. Explicitly click the
    # WhatsApp message composer before sending so Enter cannot go to PowerShell.
    _click_whatsapp_composer(pyautogui)
    time.sleep(0.3)

    # Capture the Send button before pressing Enter.
    before_button = _find_whatsapp_send_button(pyautogui)

    # WhatsApp Desktop uses Enter to send the message by default.
    pyautogui.press("enter")
    time.sleep(1.2)

    # If the green Send button disappeared, the composer was cleared and the
    # message was sent. If it remains, try the actual visible button once.
    after_button = _find_whatsapp_send_button(pyautogui)
    if before_button is not None and after_button is None:
        return True

    if after_button is not None:
        pyautogui.click(*after_button)
        time.sleep(1.2)
        final_button = _find_whatsapp_send_button(pyautogui)
        if final_button is None:
            return True

    return False


def whatsapp_draft(data: dict[str, str]) -> None:
    """Open WhatsApp Desktop, find the contact, type the message, then confirm send."""
    global PENDING_WHATSAPP

    contact = data["contact"]
    message = data["message"]

    if not open_whatsapp_desktop():
        return

    time.sleep(3)

    try:
        # Official WhatsApp Windows shortcut: Ctrl+Alt+N opens New Chat.
        perform_laptop_action(action="hotkey", key="ctrl+alt+n")
        time.sleep(1.5)

        # Some versions can leave the shortcut's last key in the search field.
        # Ctrl+A ensures ONLY the contact name is searched.
        perform_laptop_action(action="hotkey", key="ctrl+a")
        time.sleep(0.2)
        perform_laptop_action(action="type_text", text=contact)
        time.sleep(2.0)
        perform_laptop_action(action="press_key", key="enter")
        time.sleep(2.0)

        # Click the composer explicitly, then type only the message.
        import pyautogui
        _click_whatsapp_composer(pyautogui)
        perform_laptop_action(action="type_text", text=message)
        time.sleep(0.8)
    except Exception as exc:
        say(f"Could not prepare the WhatsApp message automatically: {exc}")
        say("WhatsApp is open. You can finish the message manually.")
        return

    PENDING_WHATSAPP = {"contact": contact, "message": message}

    say(
        f"Message prepared for {contact}:\n"
        f"  {message}\n\n"
        "It has NOT been sent yet."
    )

    if ask_yes_no("Send this WhatsApp message now?"):
        try:
            if _send_whatsapp_message():
                PENDING_WHATSAPP = None
                say("WhatsApp message sent successfully.")
            else:
                say("The message was NOT sent. WhatsApp still appears to have text in the composer.")
        except Exception as exc:
            say(f"Could not send the WhatsApp message: {exc}")
    else:
        PENDING_WHATSAPP = None
        say("Message was not sent.")


def open_leetcode_problem(command: str) -> bool:
    # Strip common natural-language wrappers.
    m = re.search(
        r"\b(?:open|show|find|go\s+to)\s+(?:the\s+)?(?:leetcode\s+)?"
        r"(?:problem\s+)?(.+?)\s*$",
        command,
        re.I,
    )
    if not m:
        return False

    query = m.group(1).strip()
    if not query:
        return False

    # "open my leetcode" should go to the homepage, not search.
    if query.casefold() in {"my leetcode", "leetcode", "my leetcode account"}:
        open_url("https://leetcode.com")
        say("Opened LeetCode.")
        return True

    # Use LeetCode's search page so the user can choose the exact problem.
    search_url = "https://leetcode.com/problemset/?search=" + urllib.parse.quote(query)
    open_url(search_url)
    say(f"Opened LeetCode search for: {query}")
    return True


def open_pdf(command: str) -> bool:
    m = re.search(
        r"\b(?:open|launch|show)\s+(?:pdf\s+)?[\"']?(.+?\.pdf)[\"']?\s*$",
        command,
        re.I,
    )
    if not m:
        return False

    path = os.path.expandvars(m.group(1).strip().strip('"').strip("'"))
    if not os.path.isabs(path):
        path = os.path.abspath(path)

    if not os.path.isfile(path):
        say(f"PDF not found: {path}")
        return True

    os.startfile(path)
    say(f"Opened PDF: {path}")
    return True


def search_web(command: str) -> bool:
    m = re.search(
        r"^(?:search|google|search\s+for)\s+(.+)$",
        command.strip(),
        re.I,
    )
    if not m:
        return False

    query = m.group(1).strip()
    open_url("https://www.google.com/search?q=" + urllib.parse.quote(query))
    say(f"Searched Google for: {query}")
    return True



def open_folder_in_vscode(command: str) -> bool:
    """Open a folder in VS Code; accepts either a full path or folder name."""
    if open_named_folder_in_vscode(command):
        return True

    patterns = [
        r'^(?:open|launch)\s+(.+?)\s+(?:folder\s+)?in\s+(?:vs\s*code|vscode|visual studio code)$',
        r'^(?:open|launch)\s+(?:vs\s*code|vscode|visual studio code)\s+(?:folder\s+)?(.+)$',
    ]

    folder = None
    for pattern in patterns:
        match = re.search(pattern, command.strip(), re.I)
        if match:
            folder = match.group(1).strip().strip('"').strip("'")
            break

    if not folder:
        return False

    folder = os.path.expandvars(folder)
    if not os.path.isabs(folder):
        folder = os.path.abspath(folder)

    if not os.path.isdir(folder):
        say(f"Folder not found: {folder}")
        return True

    try:
        subprocess.Popen(["code", folder], shell=False)
        say(f"Opened folder in VS Code: {folder}")
    except FileNotFoundError:
        say("VS Code 'code' command is not available in PATH. Run 'code --version' in PowerShell to check.")
    except Exception as exc:
        say(f"Could not open VS Code folder: {exc}")
    return True


def parse_simple_task(command: str) -> bool:
    low = command.casefold().strip()

    # Screenshot.
    if any(
        phrase in low
        for phrase in (
            "take screenshot",
            "take a screenshot",
            "screenshot lelo",
            "screenshot lo",
            "capture screen",
            "screen capture",
        )
    ):
        result = perform_laptop_action(action="screenshot")
        say(result.get("message", "Screenshot captured."))
        return True

    # Direct PDF.
    if ".pdf" in low and re.search(r"\b(open|launch|show)\b", low):
        return open_pdf(command)

    # Web search.
    if search_web(command):
        return True

    # Find a folder by name and open it in VS Code.
    if open_named_folder_in_vscode(command):
        return True

    # Installed WhatsApp desktop app.
    # This must run BEFORE the generic website handler so
    # "open whatsapp" opens the installed Windows app, not WhatsApp Web.
    if re.search(
        r"\b(?:open|launch|start|run)\s+(?:whatsapp|whatsapp app)\b",
        low,
    ):
        action = parse_laptop_command("open whatsapp")

        if action is None:
            say("WhatsApp app command could not be recognized.")
            return True

        result = perform_laptop_action(**action)
        say(result.get("message", "WhatsApp opened."))
        return True

    # Open a project/folder directly in VS Code.
    if open_folder_in_vscode(command):
        return True

    # Open known website.
    for name in sorted(WEBSITES, key=len, reverse=True):
        if re.search(
            rf"\b(?:open|launch|visit|go\s+to|kholo|khol)\s+{re.escape(name)}\b",
            low,
        ) or re.search(
            rf"\b{re.escape(name)}\s+(?:open|kholo|khol)\b",
            low,
        ):
            open_website(name)
            return True

    # LeetCode problem search.
    if "leetcode" in low and any(
        word in low for word in ("problem", "question", "que", "solve", "open", "show", "find")
    ):
        return open_leetcode_problem(command)

    # Gmail compose.
    gmail = parse_gmail_command(command)
    if gmail:
        compose_gmail(gmail)
        return True

    # WhatsApp.
    whatsapp = parse_whatsapp_command(command)
    if whatsapp:
        whatsapp_draft(whatsapp)
        return True

    # GitHub.
    if re.search(
        r"\b(?:push|upload|publish|deploy)\b.*\bgithub\b|\bgithub\b.*\b(?:push|upload|publish)\b",
        low,
    ):
        github_push_workflow(command)
        return True

    # Open any local file/folder by name without requiring its full path.
    if open_named_path(command):
        return True

    # Existing deterministic laptop parser handles apps, typing, keyboard, mouse, scroll.
    action = parse_laptop_command(command)
    if action is not None:
        result = perform_laptop_action(**action)
        say(result.get("message", "Done."))
        return True

    return False


def ollama_fallback(command: str) -> str | None:
    """
    We do not let the LLM execute arbitrary commands.
    It is used only to explain/normalize unknown requests.
    """
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are AURA. The user is controlling a Windows PC. "
                            "If you cannot map the request to a supported action, "
                            "briefly explain that the command is not implemented yet. "
                            "Never claim an action was executed."
                        ),
                    },
                    {"role": "user", "content": command},
                ],
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["message"]["content"].strip()
    except Exception:
        return None


def handle_command(command: str) -> bool:
    command = command.strip()
    if not command:
        return True

    if command.casefold() in EXIT_WORDS:
        say("AURA stopped.")
        return False

    try:
        handled = parse_simple_task(command)
        if handled:
            return True

        answer = ollama_fallback(command)
        if answer:
            say(answer)
        else:
            say(
                "I could not map that command to an implemented desktop action yet."
            )

    except LaptopActionError as exc:
        say(f"Desktop action failed: {exc}")
    except GitHubActionError as exc:
        say(f"GitHub action failed: {exc}")
    except Exception as exc:
        say(f"{type(exc).__name__}: {exc}")

    return True


def main() -> None:
    print("=" * 64)
    print("                 AURA AI - TERMINAL MODE")
    print("=" * 64)
    print("Voice is OFF. Type commands directly.")
    print("Type 'help' for examples or 'exit' to stop.")
    print()

    while True:
        try:
            command = input("YOU > ")
        except (KeyboardInterrupt, EOFError):
            print("\nAURA stopped.")
            break

        if command.casefold().strip() == "help":
            print(
                """
Examples:

  open calculator
  open camera
  open chrome
  open whatsapp
  open whatsapp web
  send whatsapp message to Rahul saying Hello, I will call you later
  open github
  open gmail
  open my leetcode
  open leetcode two sum
  open CivicFix
  open CivicFix in VS Code
  take screenshot
  open pdf C:\\Users\\sajal\\Downloads\\notes.pdf

  compose mail to test@gmail.com subject Meeting body Hello, see you tomorrow

  push this project to github
  push C:\\Users\\sajal\\Desktop\\CivicFix to github

  type Hello AURA in notepad
  press ctrl+s
  scroll down

  search for Siemens interview questions

Sensitive operations always ask for confirmation.
"""
            )
            continue

        if not handle_command(command):
            break


if __name__ == "__main__":
    main()
