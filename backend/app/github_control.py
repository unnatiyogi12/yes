import os
import re
import subprocess
import json
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



def _gh_json(command: list[str], timeout: int = 60) -> Any:
    code, stdout, stderr = _run(command, timeout=timeout)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "GitHub CLI command failed.")
    if not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GitHubActionError("GitHub CLI returned invalid JSON.") from exc


def _owner_repo(target: str, account: str | None = None) -> str:
    value = target.strip().strip('"\'')
    if "/" in value:
        return value
    if not account:
        raise GitHubActionError("Use owner/repository or authenticate so I can determine the GitHub account.")
    return f"{account}/{value}"


def github_list_repos(limit: int = 100) -> list[dict[str, Any]]:
    auth = github_auth_status()
    account = auth.get("account")
    if not account:
        raise GitHubActionError("Could not determine the authenticated GitHub account.")
    limit = max(1, min(int(limit), 1000))
    data = _gh_json([
        "gh", "repo", "list", account,
        "--limit", str(limit),
        "--json", "name,visibility,url,description,stargazerCount,forkCount,isArchived,updatedAt",
    ])
    return [
        {
            "name": item.get("name"),
            "visibility": str(item.get("visibility", "")).lower(),
            "url": item.get("url"),
            "description": item.get("description"),
            "stars": item.get("stargazerCount", 0),
            "forks": item.get("forkCount", 0),
            "archived": item.get("isArchived", False),
            "updated_at": item.get("updatedAt"),
        }
        for item in data
        if isinstance(item, dict)
    ]


def github_repo_info(target: str, account: str | None = None) -> dict[str, Any]:
    full_name = _owner_repo(target, account)
    data = _gh_json([
        "gh", "repo", "view", full_name,
        "--json", "name,nameWithOwner,owner,visibility,url,description,defaultBranchRef,stargazerCount,forkCount,issues,updatedAt",
    ])
    if not isinstance(data, dict):
        raise GitHubActionError("Unexpected repository information from GitHub.")
    owner = data.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("login")
    default_ref = data.get("defaultBranchRef")
    if isinstance(default_ref, dict):
        default_branch = default_ref.get("name")
    else:
        default_branch = default_ref
    issues = data.get("issues")
    open_issues = issues.get("totalCount") if isinstance(issues, dict) else None
    return {
        "name": data.get("nameWithOwner") or full_name,
        "owner": owner,
        "visibility": str(data.get("visibility", "")).lower(),
        "url": data.get("url"),
        "description": data.get("description"),
        "default_branch": default_branch or "unknown",
        "stars": data.get("stargazerCount", 0),
        "forks": data.get("forkCount", 0),
        "open_issues": open_issues if open_issues is not None else 0,
        "updated_at": data.get("updatedAt"),
    }


def github_list_issues(target: str, account: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    full_name = _owner_repo(target, account)
    limit = max(1, min(int(limit), 100))
    data = _gh_json([
        "gh", "issue", "list", "--repo", full_name,
        "--state", "open", "--limit", str(limit),
        "--json", "number,title,author,url,labels,updatedAt",
    ])
    result = []
    for item in data:
        author = item.get("author")
        if isinstance(author, dict):
            author = author.get("login")
        result.append({
            "number": item.get("number"),
            "title": item.get("title"),
            "author": author or "unknown",
            "url": item.get("url"),
            "labels": [x.get("name") for x in (item.get("labels") or []) if isinstance(x, dict)],
            "updated_at": item.get("updatedAt"),
        })
    return result


def github_list_prs(target: str, account: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    full_name = _owner_repo(target, account)
    limit = max(1, min(int(limit), 100))
    data = _gh_json([
        "gh", "pr", "list", "--repo", full_name,
        "--state", "open", "--limit", str(limit),
        "--json", "number,title,author,url,headRefName,baseRefName,updatedAt",
    ])
    result = []
    for item in data:
        author = item.get("author")
        if isinstance(author, dict):
            author = author.get("login")
        result.append({
            "number": item.get("number"),
            "title": item.get("title"),
            "author": author or "unknown",
            "url": item.get("url"),
            "head": item.get("headRefName"),
            "base": item.get("baseRefName"),
            "updated_at": item.get("updatedAt"),
        })
    return result


def github_list_runs(target: str, account: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    full_name = _owner_repo(target, account)
    limit = max(1, min(int(limit), 100))
    data = _gh_json([
        "gh", "run", "list", "--repo", full_name,
        "--limit", str(limit),
        "--json", "name,status,conclusion,headBranch,url,workflowName,createdAt",
    ])
    result = []
    for item in data:
        workflow = item.get("workflowName") or item.get("name") or "workflow"
        result.append({
            "workflow": workflow,
            "status": item.get("status") or "unknown",
            "conclusion": item.get("conclusion") or "pending",
            "branch": item.get("headBranch") or "unknown",
            "url": item.get("url"),
            "created_at": item.get("createdAt"),
        })
    return result


def github_list_commits(target: str, account: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    full_name = _owner_repo(target, account)
    limit = max(1, min(int(limit), 100))
    raw = _gh_json([
        "gh", "api", f"repos/{full_name}/commits", "-f", f"per_page={limit}"
    ])
    result = []
    for item in raw:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        message = (commit.get("message") or "").splitlines()[0]
        result.append({
            "sha": str(item.get("sha", ""))[:7],
            "message": message,
            "author": author.get("name") or "unknown",
            "url": item.get("html_url"),
            "date": author.get("date"),
        })
    return result


def github_search_repos(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        raise GitHubActionError("GitHub search query cannot be empty.")
    limit = max(1, min(int(limit), 100))
    data = _gh_json([
        "gh", "search", "repos", query,
        "--limit", str(limit),
        "--json", "name,owner,visibility,url,description,stargazersCount,forksCount",
    ])
    result = []
    for item in data:
        owner = item.get("owner")
        if isinstance(owner, dict):
            owner = owner.get("login")
        result.append({
            "name": f"{owner}/{item.get('name')}" if owner else item.get("name"),
            "visibility": str(item.get("visibility", "")).lower(),
            "url": item.get("url"),
            "description": item.get("description"),
            "stars": item.get("stargazersCount", 0),
            "forks": item.get("forksCount", 0),
        })
    return result


def github_create_repo_action(repo_name: str, visibility: str = "private") -> dict[str, Any]:
    repo_name = normalize_repo_name(repo_name)
    visibility = visibility.casefold()
    if visibility not in {"private", "public"}:
        raise GitHubActionError("Visibility must be private or public.")
    auth = github_auth_status()
    account = auth.get("account")
    if not account:
        raise GitHubActionError("Could not determine the authenticated GitHub account.")
    flag = "--private" if visibility == "private" else "--public"
    code, stdout, stderr = _run(["gh", "repo", "create", f"{account}/{repo_name}", flag], timeout=120)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "GitHub repository creation failed.")
    return {"name": f"{account}/{repo_name}", "visibility": visibility, "url": f"https://github.com/{account}/{repo_name}"}


def github_clone_repo(target: str, destination: str | None = None, account: str | None = None) -> dict[str, Any]:
    full_name = _owner_repo(target, account)
    command = ["gh", "repo", "clone", full_name]
    if destination:
        destination = os.path.expandvars(destination.strip().strip('"\''))
        command.append(destination)
    code, stdout, stderr = _run(command, timeout=300)
    if code != 0:
        raise GitHubActionError(stderr or stdout or "GitHub repository clone failed.")
    if destination:
        path = str(Path(destination).expanduser().resolve())
    else:
        path = str(Path(full_name.split("/", 1)[-1]).resolve())
    return {"repository": full_name, "path": path, "output": stdout or stderr}

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
