
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
    github_list_repos,
    github_repo_info,
    github_list_issues,
    github_list_prs,
    github_list_runs,
    github_list_commits,
    github_search_repos,
    github_create_repo_action,
    github_clone_repo,
)
from personal_reports import (
    collect_progress,
    generate_docx_report,
    get_leetcode_username,
    set_leetcode_username,
)

try:
    from app.voice_control import listen_and_transcribe, speak_text
except Exception:
    listen_and_transcribe = None
    speak_text = None


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
    Path(__file__).resolve().parent.parent,
    Path.cwd(),
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
CONFIRM_HANDLER = None


def say(msg: str) -> None:
    print(f"\nAURA > {msg}")


def ask_yes_no(question: str) -> bool:
    if CONFIRM_HANDLER is not None:
        try:
            return bool(CONFIRM_HANDLER(question))
        except Exception:
            return False
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

    # Let the dedicated laptop/app parser handle installed applications.
    # Otherwise a command like "open calculator" would be consumed by the
    # file/folder search and incorrectly report "not found".
    known_apps = {
        "chrome", "edge", "firefox", "notepad", "calculator", "paint",
        "explorer", "camera", "photos", "clock", "calendar", "mail",
        "settings", "task manager", "snipping tool", "media player",
        "vs code", "vscode", "visual studio", "terminal", "powershell",
        "cmd", "postman", "github desktop", "word", "excel", "powerpoint",
        "outlook", "teams", "whatsapp", "spotify", "discord", "zoom",
        "telegram", "obs studio",
    }
    if target.casefold() in known_apps:
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


def _git_remote_url(path: str) -> str | None:
    """Return the origin URL, if this project already has one."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        value = (result.stdout or "").strip()
        return value or None
    except Exception:
        return None


def _git_has_staged_changes(path: str) -> bool:
    """True when there is something staged that can be committed."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=path,
            timeout=15,
            check=False,
        )
        return result.returncode != 0
    except Exception:
        return False


def _git_current_branch(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return (result.stdout or "").strip() or "main"
    except Exception:
        return "main"


def _remote_matches_repo(remote: str | None, repo_name: str, account: str | None) -> bool:
    if not remote:
        return False
    normalized = remote.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    expected = f"https://github.com/{account}/{repo_name}" if account else None
    ssh_expected = f"git@github.com:{account}/{repo_name}" if account else None
    return normalized in {x for x in (expected, ssh_expected) if x}


def _ensure_github_remote(path: str, repo_name: str, visibility: str, account: str | None) -> str | None:
    """Create/select the requested GitHub repo and make it origin."""
    remote = _git_remote_url(path)

    if remote and _remote_matches_repo(remote, repo_name, account):
        return remote

    if remote:
        say(f"This project already has origin: {remote}")
        if not ask_yes_no(f"Use this existing GitHub repository instead of '{repo_name}'?"):
            if not account:
                say("Could not determine the GitHub account needed to create the requested repository.")
                return None

            full_name = f"{account}/{repo_name}"
            code = subprocess.run(
                ["gh", "repo", "view", full_name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).returncode

            if code != 0:
                visibility_flag = "--private" if visibility == "private" else "--public"
                create_result = subprocess.run(
                    ["gh", "repo", "create", full_name, visibility_flag],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if create_result.returncode != 0:
                    detail = (create_result.stderr or create_result.stdout or "").strip()
                    say(f"Could not create {full_name}: {detail}")
                    return None

            new_remote = f"https://github.com/{full_name}.git"
            set_result = subprocess.run(
                ["git", "remote", "set-url", "origin", new_remote],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if set_result.returncode != 0:
                say(f"Could not update origin: {(set_result.stderr or '').strip()}")
                return None
            say(f"Origin updated to {new_remote}")
            return new_remote

        return remote

    if not account:
        say("Could not determine the authenticated GitHub account.")
        return None

    try:
        create_github_repo(path, repo_name, visibility)
    except GitHubActionError as exc:
        say(f"GitHub repository creation failed: {exc}")
        return None

    return _git_remote_url(path)


def _robust_git_push(path: str) -> bool:
    """Push with a retry and verify remote state before reporting failure."""
    branch = _git_current_branch(path)

    commands = [
        ["git", "push", "-u", "origin", branch],
        ["git", "-c", "http.version=HTTP/1.1", "push", "-u", "origin", branch],
    ]

    last_detail = "git push failed."
    for index, cmd in enumerate(commands, 1):
        result = subprocess.run(
            cmd,
            cwd=path,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        combined = "\n".join(x for x in (result.stdout, result.stderr) if x).strip()

        if result.returncode == 0:
            return True

        # Git can return a connection error even after the remote has already
        # received the commit. Verify before saying the push failed.
        verify = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        remote_line = (verify.stdout or "").strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        local_sha = (head.stdout or "").strip()
        remote_sha = remote_line.split()[0] if remote_line else ""
        if local_sha and remote_sha and local_sha == remote_sha:
            return True

        if "Everything up-to-date" in combined:
            return True

        last_detail = combined or last_detail
        if index < len(commands):
            say("Git push connection failed. Retrying with HTTP/1.1...")

    say(f"Push failed: {last_detail}")
    return False


def github_push_workflow(command: str) -> None:
    """Push a project to GitHub with safe, resumable handling."""
    path = find_project_from_text(command)

    if not path:
        m = re.search(r"\bpush\s+(?:my\s+)?(.+?)\s+to\s+github\b", command, re.I)
        if m:
            project_name = re.sub(
                r"\b(?:project|folder|repo|repository)\b", "", m.group(1), flags=re.I
            ).strip()
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
    repo_name_input = input(f"\nAURA > Repository name [{suggestion}] > ").strip()

    # Prevent an accidental yes/no confirmation from becoming the repo name.
    if repo_name_input.casefold() in {"yes", "y", "no", "n", "haan", "ha", "ok", "okay"}:
        say(f"'{repo_name_input}' looks like a confirmation, not a repository name. Using '{suggestion}'.")
        repo_name_input = suggestion

    repo_name = repo_name_input or suggestion
    try:
        repo_name = normalize_repo_name(repo_name)
    except Exception as exc:
        say(f"Invalid repository name: {exc}")
        return

    visibility = input(
        "AURA > Visibility [private/public] (default private) > "
    ).strip().casefold() or "private"
    if visibility in {"yes", "y", "haan", "ha", "ok", "okay"}:
        visibility = "private"
        say("I interpreted that confirmation as the default private visibility.")
    if visibility not in {"private", "public"}:
        say("Please choose private or public.")
        return

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
        git_add(project_path)
    except GitHubActionError as exc:
        say(f"git add blocked/failed: {exc}")
        return
    say("Files staged successfully.")

    if _git_has_staged_changes(project_path):
        commit_message = input(
            "\nAURA > Commit message [Initial commit] > "
        ).strip() or "Initial commit"

        if not ask_yes_no(f"Create commit '{commit_message}'?"):
            say("Cancelled before commit.")
            return

        try:
            git_commit(project_path, commit_message)
            say("Commit created successfully.")
        except GitHubActionError as exc:
            say(f"Commit failed: {exc}")
            return
    else:
        say("Nothing new to commit. The working tree is already clean.")

    account = auth.get("account")
    remote = _git_remote_url(project_path)
    if remote:
        say(f"Existing Git remote: {remote}")

    if not ask_yes_no(f"Continue with GitHub repository '{repo_name}' as {visibility}?"):
        say("Cancelled before GitHub repository setup.")
        return

    remote = _ensure_github_remote(project_path, repo_name, visibility, account)
    if not remote:
        return

    say(f"GitHub remote ready: {remote}")

    if not ask_yes_no("Push the committed code to GitHub now?"):
        say("Code was committed, but it was not pushed.")
        return

    if not _robust_git_push(project_path):
        return

    say("Code pushed successfully.")

    if ask_yes_no("Open the GitHub repository in Chrome?"):
        try:
            open_repo(project_path)
        except Exception:
            open_url(remote.replace(".git", ""))

    say("GitHub workflow completed.")



def _format_github_repo_list(repos: list[dict[str, Any]]) -> None:
    if not repos:
        say("No GitHub repositories were found.")
        return
    say(f"I found {len(repos)} GitHub repositories:")
    for i, repo in enumerate(repos, 1):
        visibility = repo.get("visibility", "unknown")
        stars = repo.get("stars")
        description = (repo.get("description") or "").strip()
        line = f"{i}. {repo.get('name', 'unknown')} [{visibility}]"
        if stars is not None:
            line += f" ⭐ {stars}"
        if description:
            line += f" — {description[:90]}"
        print(line)


def _github_repo_target(command: str) -> str | None:
    patterns = [
        r"\b(?:repo|repository)\s+(?:named\s+)?[\"']?([^\"']+?)[\"']?\s*$",
        r"\b(?:github\s+)?(?:repo|repository)\s*[:=-]\s*([^,]+)",
        r"\b(?:for|of)\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, command, re.I)
        if m:
            value = m.group(1).strip().strip('"\' ')
            if value:
                return value
    # Natural forms such as: "show Aura-AI on github"
    m = re.search(r"\b(?:show|open|details?\s+for|about)\s+([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)\s+(?:on\s+)?github\b", command, re.I)
    if m:
        return m.group(1).strip()
    return None


def github_account_name() -> str | None:
    auth = github_auth_status()
    return auth.get("account") if auth.get("authenticated") else None


def github_action_workflow(command: str) -> bool:
    """Handle read/action GitHub commands through the authenticated gh CLI."""
    low = command.casefold().strip()

    # Do not steal the existing project-push workflow.
    if re.search(r"\b(?:push|upload|publish|deploy)\b.*\bgithub\b", low):
        return False

    auth = github_auth_status()
    if not auth.get("authenticated"):
        if "github" in low and any(x in low for x in ("repo", "repository", "issue", "pull request", "pr", "workflow", "run")):
            say("GitHub is not authenticated. Run `gh auth login` first.")
            return True
        return False

    account = auth.get("account")

    # List repositories.
    if re.search(r"\b(?:list|show|display|get|fetch)\b.*\b(?:my\s+)?(?:github\s+)?(?:repos|repositories)\b", low) or re.search(r"\b(?:my|all)\s+(?:github\s+)?(?:repos|repositories)\b", low):
        try:
            repos = github_list_repos(limit=100)
            _format_github_repo_list(repos)
        except GitHubActionError as exc:
            say(f"GitHub repository listing failed: {exc}")
        return True

    # Search repositories on GitHub.
    if re.search(r"\b(?:search|find)\b.*\bgithub\s+(?:repos|repositories)\b", low):
        m = re.search(r"\b(?:for|named|called)\s+(.+?)\s*$", command, re.I)
        query = (m.group(1).strip() if m else "").strip('"\'')
        if not query:
            query = input("AURA > What should I search for on GitHub? ").strip()
        if not query:
            say("Cancelled.")
            return True
        try:
            repos = github_search_repos(query, limit=20)
            _format_github_repo_list(repos)
        except GitHubActionError as exc:
            say(f"GitHub search failed: {exc}")
        return True

    # Repository details.
    if re.search(r"\b(?:show|view|details?|info|information|about)\b.*\b(?:github\s+)?(?:repo|repository)\b", low):
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository name (e.g. Aura-AI) > ").strip()
        if not target:
            say("Cancelled.")
            return True
        try:
            info = github_repo_info(target, account=account)
            say(
                f"Repository: {info.get('name')}\n"
                f"Owner: {info.get('owner')}\n"
                f"Visibility: {info.get('visibility')}\n"
                f"Default branch: {info.get('default_branch')}\n"
                f"Stars: {info.get('stars')} | Forks: {info.get('forks')} | Open issues: {info.get('open_issues')}\n"
                f"URL: {info.get('url')}\n"
                f"Description: {info.get('description') or '(none)'}"
            )
        except GitHubActionError as exc:
            say(f"Could not read repository: {exc}")
        return True

    # Open a repository in the browser.
    if re.search(r"\b(?:open|visit|go\s+to)\b.*\bgithub\b", low) and re.search(r"\b(?:repo|repository)\b|\b[a-z0-9_.-]+/[a-z0-9_.-]+\b", low):
        target = _github_repo_target(command)
        if target:
            try:
                info = github_repo_info(target, account=account)
                open_url(info["url"])
                say(f"Opened GitHub repository: {info['url']}")
            except GitHubActionError as exc:
                say(f"Could not open repository: {exc}")
            return True

    # Issues.
    if re.search(r"\b(?:list|show|display|get|fetch)\b.*\bissues?\b", low) and "github" in low:
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository name for issues > ").strip()
        if not target:
            say("Cancelled.")
            return True
        try:
            issues = github_list_issues(target, account=account, limit=30)
            if not issues:
                say(f"No open issues found in {target}.")
            else:
                say(f"Open issues in {target}:")
                for i, item in enumerate(issues, 1):
                    print(f"{i}. #{item['number']} {item['title']} — {item.get('author', 'unknown')}")
        except GitHubActionError as exc:
            say(f"Could not list issues: {exc}")
        return True

    # Pull requests.
    if re.search(r"\b(?:list|show|display|get|fetch)\b.*\b(?:pull\s+requests?|prs?)\b", low) and "github" in low:
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository name for pull requests > ").strip()
        if not target:
            say("Cancelled.")
            return True
        try:
            prs = github_list_prs(target, account=account, limit=30)
            if not prs:
                say(f"No open pull requests found in {target}.")
            else:
                say(f"Open pull requests in {target}:")
                for i, item in enumerate(prs, 1):
                    print(f"{i}. #{item['number']} {item['title']} — {item.get('author', 'unknown')}")
        except GitHubActionError as exc:
            say(f"Could not list pull requests: {exc}")
        return True

    # Workflow runs / CI status.
    if re.search(r"\b(?:list|show|check|get|fetch)\b.*\b(?:workflow|workflows|runs?|actions?)\b", low) and "github" in low:
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository name for workflow runs > ").strip()
        if not target:
            say("Cancelled.")
            return True
        try:
            runs = github_list_runs(target, account=account, limit=20)
            if not runs:
                say(f"No workflow runs found in {target}.")
            else:
                say(f"Recent GitHub Actions runs in {target}:")
                for i, item in enumerate(runs, 1):
                    print(f"{i}. {item['workflow']} — {item['status']} / {item['conclusion']} — {item['branch']}")
        except GitHubActionError as exc:
            say(f"Could not list workflow runs: {exc}")
        return True

    # Recent commits.
    if re.search(r"\b(?:list|show|display|get|fetch)\b.*\bcommits?\b", low) and "github" in low:
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository name for commits > ").strip()
        if not target:
            say("Cancelled.")
            return True
        try:
            commits = github_list_commits(target, account=account, limit=20)
            if not commits:
                say(f"No commits found in {target}.")
            else:
                say(f"Recent commits in {target}:")
                for i, item in enumerate(commits, 1):
                    print(f"{i}. {item['sha']} — {item['message']} — {item['author']}")
        except GitHubActionError as exc:
            say(f"Could not list commits: {exc}")
        return True

    # Create repository — always confirmation because it changes account state.
    if re.search(r"\b(?:create|make|new)\b.*\b(?:github\s+)?(?:repo|repository)\b", low):
        m = re.search(r"\b(?:repo|repository)\s+(?:named|called)?\s*[\"']?([A-Za-z0-9._-]+)[\"']?", command, re.I)
        repo_name = m.group(1) if m else input("AURA > New repository name > ").strip()
        if not repo_name:
            say("Cancelled.")
            return True
        visibility = "private"
        if re.search(r"\bpublic\b", low):
            visibility = "public"
        elif re.search(r"\bprivate\b", low):
            visibility = "private"
        if not ask_yes_no(f"Create GitHub repository '{repo_name}' as {visibility}?"):
            say("Cancelled.")
            return True
        try:
            result = github_create_repo_action(repo_name, visibility)
            say(f"Repository created: {result['url']}")
        except GitHubActionError as exc:
            say(f"Could not create repository: {exc}")
        return True

    # Clone repository — changes local files, so confirm.
    if re.search(r"\bclone\b.*\bgithub\b", low):
        target = _github_repo_target(command)
        if not target:
            target = input("AURA > Repository to clone (e.g. owner/repo or repo name) > ").strip()
        destination = None
        m = re.search(r"\bto\s+(.+?)\s*$", command, re.I)
        if m:
            destination = m.group(1).strip().strip('"\'')
        if not target:
            say("Cancelled.")
            return True
        dest_text = f" into '{destination}'" if destination else ""
        if not ask_yes_no(f"Clone GitHub repository '{target}'{dest_text}?" ):
            say("Cancelled.")
            return True
        try:
            result = github_clone_repo(target, destination=destination, account=account)
            say(f"Repository cloned to: {result['path']}")
        except GitHubActionError as exc:
            say(f"Could not clone repository: {exc}")
        return True

    return False


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

    # First try Enter. WhatsApp for Windows can be configured so Enter sends
    # the message; this is the safest method because it does not depend on
    # the exact position or color of the Send button.
    pyautogui.press("enter")
    time.sleep(1.5)

    # Do not rely on the green-button detector to decide whether Enter worked.
    # The detector can miss the button because of theme/UI changes. Instead,
    # look for actual text still present in the composer by selecting it and
    # copying it to the clipboard. If there is no text, the message was sent.
    try:
        import pyperclip
        _click_whatsapp_composer(pyautogui)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.15)
        remaining = pyperclip.paste().strip()
        pyautogui.press("escape")
        if not remaining:
            return True
    except Exception:
        # Clipboard verification is only a secondary check.
        pass

    # If Enter did not send, click the likely Send-button positions. We try a
    # few very small offsets around the bottom-right of the WhatsApp window.
    # This works even when the button is not detected by color scanning.
    rect = _whatsapp_window_rect()
    if rect:
        left, top, right, bottom = rect
        candidates = [
            (right - 38, bottom - 48),
            (right - 48, bottom - 52),
            (right - 58, bottom - 48),
        ]
        for x, y in candidates:
            pyautogui.click(int(x), int(y))
            time.sleep(1.0)

            # If the composer is now empty, the click sent the message.
            try:
                import pyperclip
                _click_whatsapp_composer(pyautogui)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "c")
                time.sleep(0.1)
                remaining = pyperclip.paste().strip()
                pyautogui.press("escape")
                if not remaining:
                    return True
            except Exception:
                pass

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


def _leetcode_slug(text: str) -> str:
    """Convert a natural-language LeetCode title to a usable problem slug."""
    title = re.sub(r"^(?:the\s+)?(?:leetcode\s+)?(?:problem\s+|question\s+)?", "", text.strip(), flags=re.I)
    title = re.sub(r"\s+(?:on|in)\s+leetcode\s*$", "", title, flags=re.I).strip()
    title = re.sub(r"[’'`]", "", title)
    title = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    known = {
        "two-sum": "two-sum",
        "valid-parentheses": "valid-parentheses",
        "merge-two-sorted-lists": "merge-two-sorted-lists",
        "best-time-to-buy-and-sell-stock": "best-time-to-buy-and-sell-stock",
        "valid-palindrome": "valid-palindrome",
        "contains-duplicate": "contains-duplicate",
        "product-of-array-except-self": "product-of-array-except-self",
        "maximum-subarray": "maximum-subarray",
        "climbing-stairs": "climbing-stairs",
        "binary-tree-inorder-traversal": "binary-tree-inorder-traversal",
        "maximum-depth-of-binary-tree": "maximum-depth-of-binary-tree",
        "reverse-linked-list": "reverse-linked-list",
        "linked-list-cycle": "linked-list-cycle",
        "longest-substring-without-repeating-characters":
            "longest-substring-without-repeating-characters",
        "3sum": "3sum",
        "3-sum": "3sum",
        "group-anagrams": "group-anagrams",
        "valid-anagram": "valid-anagram",
        "search-in-rotated-sorted-array": "search-in-rotated-sorted-array",
        "number-of-islands": "number-of-islands",
        "course-schedule": "course-schedule",
        "subsets": "subsets",
        "permutations": "permutations",
        "combination-sum": "combination-sum",
        "trapping-rain-water": "trapping-rain-water",
    }
    return known.get(title, title)


def open_leetcode_problem(command: str) -> bool:
    """Open a specific LeetCode problem directly when possible."""
    low = command.casefold().strip()

    # Homepage/account commands.
    if low in {
        "open leetcode", "open my leetcode", "open my leetcode account",
        "show leetcode", "go to leetcode",
    }:
        open_url("https://leetcode.com")
        say("Opened LeetCode.")
        return True

    # Extract the problem title from common text/voice forms.
    patterns = [
        r"^(?:leetcode)\s+(?:problem\s+|question\s+)?(.+?)\s*$",
        r"^(?:open|show|find|go\s+to)\s+(?:the\s+)?(.+?)\s+(?:on|in)\s+leetcode\s*$",
        r"^(?:open|show|find|go\s+to)\s+(?:leetcode\s+)?(?:problem\s+|question\s+)?(.+?)\s*$",
    ]
    query = None
    for pattern in patterns:
        m = re.match(pattern, command.strip(), re.I)
        if m:
            query = m.group(1).strip()
            break

    if not query:
        return False

    query = re.sub(r"\s+(?:leetcode)$", "", query, flags=re.I).strip()
    if query.casefold() in {"leetcode", "my leetcode", "my leetcode account"}:
        open_url("https://leetcode.com")
        say("Opened LeetCode.")
        return True

    slug = _leetcode_slug(query)
    if not slug:
        return False

    url = f"https://leetcode.com/problems/{slug}/"
    open_url(url)
    say(f"Opening LeetCode problem: {query}")
    return True

def personal_report_workflow(command: str) -> bool:
    """Generate LeetCode/GitHub/Gmail/Calendar progress reports."""
    low = command.casefold().strip()

    m = re.match(r"^set\s+leetcode\s+username\s+(.+?)\s*$", command, re.I)
    if m:
        username = set_leetcode_username(m.group(1))
        say(f"LeetCode username saved: {username}")
        return True

    report_words = ("report", "progress", "activity", "recent")
    source_words = ("leetcode", "github", "gmail", "calendar", "progress", "activity")
    if not ((any(x in low for x in report_words) and any(x in low for x in source_words)) or
            low in {"my progress report", "recent progress", "weekly tech report", "monthly tech report"}):
        return False

    days = 30
    if "week" in low or "7 days" in low:
        days = 7
    elif "90 days" in low or "quarter" in low:
        days = 90

    try:
        say(f"Collecting your recent progress for the last {days} days...")
        data = collect_progress(days)
        for key in ("github", "leetcode", "google"):
            item = data.get(key, {})
            if item.get("ok"):
                label = "Gmail + Calendar" if key == "google" else key.title()
                say(f"{label}: ready")
            else:
                label = "Gmail + Calendar" if key == "google" else key.title()
                say(f"{label}: unavailable — {item.get('error', 'unknown error')}")
        path = generate_docx_report(data, days)
        say(f"Progress report created: {path}")
        try:
            os.startfile(str(path))
        except Exception:
            pass
        return True
    except Exception as exc:
        say(f"Could not generate the progress report: {exc}")
        return True


def open_youtube_search(query: str, play: bool = False) -> bool:
    query = query.strip().strip('"').strip("'")
    if not query:
        return False
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    open_url(url)
    say(f"Opened YouTube search for: {query}")
    if play:
        say("I opened the search results. YouTube playback depends on the browser/player state; choose the video you want to play.")
    return True


def search_youtube(command: str) -> bool:
    text = command.strip()
    # Supports: search X on youtube / search X on YouTube / find X on youtube / play X on youtube
    m = re.match(r"^(?:search|find|look\s+for)\s+(.+?)\s+(?:on\s+)?youtube\s*$", text, re.I)
    if m:
        return open_youtube_search(m.group(1), play=False)
    m = re.match(r"^(?:play|watch|open)\s+(.+?)\s+(?:on\s+)?youtube\s*$", text, re.I)
    if m:
        return open_youtube_search(m.group(1), play=True)
    return False


def search_leetcode(command: str) -> bool:
    text = command.strip()
    low = text.casefold()
    if "leetcode" not in low:
        return False

    patterns = [
        r"^(?:search|find|look\s+for)\s+(.+?)\s+(?:questions?|problems?)\s+(?:on\s+)?leetcode\s*$",
        r"^(?:search|find|look\s+for)\s+(.+?)\s+(?:on\s+)?leetcode\s*$",
    ]
    for pattern in patterns:
        m = re.match(pattern, text, re.I)
        if m:
            query = m.group(1).strip()
            url = "https://leetcode.com/problemset/?search=" + urllib.parse.quote_plus(query)
            open_url(url)
            say(f"Opened LeetCode search for: {query}")
            return True
    return False


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
    if search_youtube(command):
        return True
    if search_leetcode(command):
        return True

    m = re.search(
        r"^(?:search|google|search\s+for)\s+(.+)$",
        command.strip(),
        re.I,
    )
    if not m:
        return False

    query = m.group(1).strip()
    # If the user explicitly says YouTube/LeetCode, the specialized handlers above
    # should have consumed it. Everything else goes to Google.
    open_url("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
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

    # Personal progress reports: LeetCode + GitHub + Gmail + Calendar.
    if personal_report_workflow(command):
        return True

    # GitHub account/repository actions use the authenticated GitHub CLI.
    if "github" in low and github_action_workflow(command):
        return True

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


def _voice_once() -> bool:
    """Listen once, run the same AURA command pipeline, and speak the reply."""
    if listen_and_transcribe is None:
        say("Voice module is unavailable. Install sounddevice and faster-whisper.")
        return True
    try:
        say("Listening...")
        result = listen_and_transcribe(seconds=6, language="auto")
        command = str(result.get("text", "")).strip()
        if not command:
            say("I did not hear a command.")
            return True
        say(f'Heard: "{command}"')
        if command.casefold() in EXIT_WORDS:
            say("AURA stopped.")
            return False
        keep_running = handle_command(command)
        if speak_text:
            try:
                speak_text("Done." if keep_running else "AURA stopped.")
            except Exception:
                pass
        return keep_running
    except Exception as exc:
        say(f"Voice error: {exc}")
        return True


def main() -> None:
    print("=" * 64)
    print("                 AURA AI - TERMINAL MODE")
    print("=" * 64)
    print("Text mode is ON. Type 'voice' to give a voice command.")
    print("Type 'help' for examples or 'exit' to stop.")
    print()

    while True:
        try:
            command = input("YOU > ")
        except (KeyboardInterrupt, EOFError):
            print("\nAURA stopped.")
            break

        if command.casefold().strip() == "voice":
            if not _voice_once():
                break
            continue

        if command.casefold().strip() == "help":
            print(
                """
Examples:

  open calculator
  open chrome
  open whatsapp
  open github
  open gmail
  open drive
  open calendar
  open my leetcode
  leetcode two sum
  open two sum on leetcode
  open valid parentheses on leetcode
  open CivicFix
  open CivicFix in VS Code
  take screenshot

  send whatsapp message to Rahul saying Hello
  compose mail to test@gmail.com subject Meeting body Hello

  push this project to github
  list my github repositories
  show github repository Aura-AI
  list github commits for repo Aura-AI

  set leetcode username YOUR_USERNAME
  generate my recent progress report
  generate my weekly tech report

  voice
  exit

Sensitive operations always ask for confirmation.
"""
            )
            continue

        if not handle_command(command):
            break


if __name__ == "__main__":
    main()
