import os
import re
import subprocess
import webbrowser
from pathlib import Path
from typing import Any


class GitHubActionError(Exception):
    """Safe, user-facing error for Git/GitHub operations."""


def _run(command: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except FileNotFoundError as exc:
        raise GitHubActionError(
            f"Command not found: {command[0]}. Please install GitHub CLI/Git first."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubActionError("The Git/GitHub command took too long and was stopped.") from exc
    except OSError as exc:
        raise GitHubActionError(f"Could not run the Git/GitHub command: {exc}") from exc


def gh_available() -> bool:
    try:
        code, _, _ = _run(["gh", "--version"], timeout=10)
        return code == 0
    except GitHubActionError:
        return False


def git_available() -> bool:
    try:
        code, _, _ = _run(["git", "--version"], timeout=10)
        return code == 0
    except GitHubActionError:
        return False


def github_auth_status() -> dict[str, Any]:
    if not gh_available():
        return {
            "installed": False,
            "authenticated": False,
            "message": "GitHub CLI (gh) is not installed.",
        }

    code, stdout, stderr = _run(["gh", "auth", "status"], timeout=20)
    combined = "\n".join(x for x in (stdout, stderr) if x).strip()
    if code == 0:
        # gh auth status usually prints the account in stderr on Windows.
        account = None
        match = re.search(r"(?:Logged in to github\.com account|account)\s+([A-Za-z0-9_.-]+)", combined, re.I)
        if match:
            account = match.group(1)
        return {
            "installed": True,
            "authenticated": True,
            "account": account,
            "message": combined or "GitHub CLI is authenticated.",
        }

    return {
        "installed": True,
        "authenticated": False,
        "account": None,
        "message": combined or "GitHub CLI is not authenticated.",
    }


def validate_project(path: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise GitHubActionError(f"Project folder does not exist: {target}")
    if not target.is_dir():
        raise GitHubActionError("The selected project path is not a folder.")
    if not git_available():
        raise GitHubActionError("Git is not installed or is not available in PATH.")

    code, top, err = _run(["git", "rev-parse", "--show-toplevel"], cwd=str(target), timeout=15)
    if code != 0:
        return {
            "path": str(target),
            "is_git_repo": False,
            "git_root": None,
            "message": "This folder is not a Git repository yet.",
        }

    root = Path(top).resolve()
    code, branch, _ = _run(["git", "branch", "--show-current"], cwd=str(root), timeout=15)
    code2, status, _ = _run(["git", "status", "--short"], cwd=str(root), timeout=15)
    return {
        "path": str(target),
        "is_git_repo": True,
        "git_root": str(root),
        "branch": branch or "(detached HEAD)",
        "changed_files": len(status.splitlines()) if code2 == 0 and status else 0,
        "status_preview": status.splitlines()[:20] if status else [],
    }


def init_git_repo(path: str) -> dict[str, Any]:
    code, stdout, stderr = _run(["git", "init"], cwd=path, timeout=30)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "git init failed.")
    return {"message": "Git repository initialized.", "output": stdout or stderr}


def get_repo_name_suggestion(path: str) -> str:
    return Path(path).resolve().name.replace(" ", "-")


def normalize_repo_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-.")
    if not name:
        raise GitHubActionError("Repository name cannot be empty.")
    if len(name) > 100:
        raise GitHubActionError("Repository name is too long.")
    return name


def get_remote(path: str) -> str | None:
    code, stdout, _ = _run(["git", "remote", "get-url", "origin"], cwd=path, timeout=15)
    return stdout if code == 0 and stdout else None


def _sensitive_file(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    blocked_names = {
        ".env", ".env.local", ".env.production", ".env.development",
        "credentials.json", "service-account.json",
    }
    blocked_suffixes = (".pem", ".key", ".p12", ".pfx")
    return name in blocked_names or name.endswith(blocked_suffixes)


def git_add(path: str) -> dict[str, Any]:
    # Safety preflight: do not stage common secrets accidentally.
    code, before_status, stderr = _run(["git", "status", "--short"], cwd=path, timeout=30)
    if code != 0:
        raise GitHubActionError(stderr or "Could not inspect Git status before staging.")
    risky = []
    for line in before_status.splitlines():
        if len(line) >= 4 and _sensitive_file(line[3:].strip()):
            risky.append(line[3:].strip())
    if risky:
        raise GitHubActionError(
            "I stopped before `git add .` because these possible secret files were found: "
            + ", ".join(risky[:10])
            + ". Add them to .gitignore or remove the secrets, then try again."
        )

    code, stdout, stderr = _run(["git", "add", "."], cwd=path, timeout=120)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "git add failed.")
    code, status, stderr = _run(["git", "status", "--short"], cwd=path, timeout=30)
    if code != 0:
        raise GitHubActionError(stderr or "Could not read Git status.")
    return {
        "message": "Files were staged with git add .",
        "staged_files": len(status.splitlines()),
        "status_preview": status.splitlines()[:30],
    }


def git_commit(path: str, message: str) -> dict[str, Any]:
    message = message.strip() or "Initial commit"
    code, stdout, stderr = _run(["git", "commit", "-m", message], cwd=path, timeout=120)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "git commit failed.")
    return {"message": message, "output": stdout or stderr}


def create_github_repo(path: str, repo_name: str, visibility: str) -> dict[str, Any]:
    repo_name = normalize_repo_name(repo_name)
    visibility_flag = "--private" if visibility == "private" else "--public"

    remote = get_remote(path)
    if remote:
        return {
            "created": False,
            "remote": remote,
            "message": f"This project already has an origin remote: {remote}",
        }

    code, stdout, stderr = _run(
        ["gh", "repo", "create", repo_name, visibility_flag, "--source", path, "--remote", "origin"],
        cwd=path,
        timeout=120,
    )
    if code != 0:
        raise GitHubActionError(stderr or stdout or "GitHub repository creation failed.")

    remote = get_remote(path)
    return {
        "created": True,
        "repo_name": repo_name,
        "visibility": visibility,
        "remote": remote,
        "output": stdout or stderr,
    }


def git_push(path: str) -> dict[str, Any]:
    # Push the currently checked-out branch and set upstream safely.
    code, branch, stderr = _run(["git", "branch", "--show-current"], cwd=path, timeout=15)
    if code != 0 or not branch:
        raise GitHubActionError("Could not determine the current branch. Checkout a branch first.")

    code, stdout, stderr = _run(
        ["git", "push", "-u", "origin", branch],
        cwd=path,
        timeout=180,
    )
    if code != 0:
        raise GitHubActionError(stderr or stdout or "git push failed.")
    return {"branch": branch, "output": stdout or stderr, "message": "Code pushed successfully."}


def get_repo_web_url(path: str) -> str | None:
    remote = get_remote(path)
    if not remote:
        return None
    remote = remote.strip()
    if remote.startswith("git@github.com:"):
        slug = remote[len("git@github.com:"):]
    elif "github.com/" in remote:
        slug = remote.split("github.com/", 1)[1]
    else:
        return None
    slug = slug.rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    return f"https://github.com/{slug}"


def open_repo(path: str) -> dict[str, Any]:
    url = get_repo_web_url(path)
    if not url:
        raise GitHubActionError("Could not determine the GitHub repository URL.")
    try:
        webbrowser.open(url)
    except Exception as exc:
        raise GitHubActionError(f"Repository was pushed, but the browser could not be opened: {exc}") from exc
    return {"url": url, "message": f"GitHub repository opened: {url}"}
