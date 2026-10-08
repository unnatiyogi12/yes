import asyncio
import os
import re
import string
import webbrowser
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.laptop_control import perform_laptop_action, parse_laptop_command, LaptopActionError
from app.voice_control import listen_and_transcribe, speak_text
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


app = FastAPI(title="AURA AI", version="0.3.0")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"

# Temporary choices for this local prototype.
# A later dashboard should store choices per user/session instead.
PENDING_FOLDER_CHOICES: list[dict[str, str]] = []

# One local pending GitHub workflow at a time. This keeps the prototype simple.
PENDING_GITHUB: dict[str, Any] = {}

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SKIP_SEARCH_DIRS = {
    "windows",
    "$recycle.bin",
    "system volume information",
    "recovery",
}

SKIP_TREE_EXPANSION = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".next",
    "dist",
    "build",
}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
async def health():
    return {"status": "running", "assistant": "AURA AI"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                OLLAMA_URL,
                json={
                    "model": MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are AURA, a helpful personal AI assistant. "
                                "Be clear and truthful. Never claim to have performed "
                                "an action unless a tool actually performed it."
                            ),
                        },
                        {"role": "user", "content": request.message},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()

        return ChatResponse(reply=data["message"]["content"])

    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Ollama is not running. Start Ollama and retry.",
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="The local AI model request failed.",
        )


@app.get("/tools/project-files")
async def list_project_files():
    """List the top-level files in the AURA AI project, read-only."""
    try:
        files = []
        for path in PROJECT_ROOT.iterdir():
            if path.name.casefold() in {".venv", ".git", "__pycache__"}:
                continue
            files.append({
                "name": path.name,
                "type": "folder" if path.is_dir() else "file",
            })

        files.sort(key=lambda item: (item["type"] != "folder", item["name"].casefold()))
        return {
            "tool": "list_project_files",
            "project": PROJECT_ROOT.name,
            "files": files,
        }
    except OSError:
        raise HTTPException(
            status_code=500,
            detail="Could not read the project folder.",
        )


def is_github_push_intent(message: str) -> bool:
    text = message.casefold()
    keywords = [
        "github par push", "github pe push", "github par upload", "github pe upload",
        "github repository banao", "github repo banao", "create github repo",
        "create repository", "push this project", "push this code", "code ko github",
        "project ko github", "github par bhejo", "github pe bhejo", "git push",
    ]
    return any(k in text for k in keywords)


def extract_project_path(message: str) -> str | None:
    # Windows absolute path, including quoted paths with spaces.
    match = re.search(r'([A-Za-z]:\\[^"\n]+)', message)
    if match:
        return match.group(1).rstrip(' .')
    return None


def github_response(message: str, action: str = "github_workflow", **extra):
    result = {"assistant": "AURA AI", "action": action, "message": message}
    result.update(extra)
    return result


def handle_github_workflow(message: str) -> dict[str, Any]:
    """Handle the confirmation-based GitHub push workflow."""
    global PENDING_GITHUB
    text = message.strip()
    low = text.casefold()

    # Start a fresh workflow.
    if not PENDING_GITHUB:
        auth = github_auth_status()
        if not auth["installed"]:
            return github_response(
                "GitHub CLI (gh) is not installed. Install GitHub CLI first, then run this command again.",
                "github_setup_required", auth=auth,
            )
        if not auth["authenticated"]:
            return github_response(
                "GitHub is not authenticated. Run `gh auth login` in PowerShell first, then try again.",
                "github_auth_required", auth=auth,
            )

        path = extract_project_path(text)
        if path:
            try:
                info = validate_project(path)
            except GitHubActionError as exc:
                return github_response(str(exc), "github_project_error")
            if not info["is_git_repo"]:
                PENDING_GITHUB = {"stage": "init_confirm", "path": info["path"]}
                return github_response(
                    f"I found the project at:\n{info['path']}\n\nIt is not a Git repository yet. Should I run `git init`? (yes/no)",
                    "github_confirmation", stage="init_confirm", project=info,
                )
            PENDING_GITHUB = {"stage": "repo_name", "path": info["git_root"]}
            suggestion = get_repo_name_suggestion(info["git_root"])
            return github_response(
                f"I found the Git project:\n{info['git_root']}\n\nWhat should the GitHub repository name be? Suggested: `{suggestion}`",
                "github_question", stage="repo_name", project=info, suggested_name=suggestion,
            )

        # If the command itself says current/this project, use the backend's current
        # working directory only when it is already a Git repo. Otherwise ask for path.
        cwd = os.getcwd()
        try:
            info = validate_project(cwd)
        except GitHubActionError as exc:
            return github_response(str(exc), "github_project_error")
        if info["is_git_repo"] and any(x in low for x in ["this project", "current project", "ye project", "is project", "current folder"]):
            PENDING_GITHUB = {"stage": "repo_name", "path": info["git_root"]}
            suggestion = get_repo_name_suggestion(info["git_root"])
            return github_response(
                f"I found the current Git project:\n{info['git_root']}\n\nWhat should the GitHub repository name be? Suggested: `{suggestion}`",
                "github_question", stage="repo_name", project=info, suggested_name=suggestion,
            )

        return github_response(
            "Which project should I push? Please give me the full project folder path, for example `C:\\Users\\sajal\\OneDrive\\Desktop\\CivicFix`.",
            "github_project_required",
        )

    stage = PENDING_GITHUB.get("stage")
    path = PENDING_GITHUB.get("path")

    if stage == "init_confirm":
        if low in {"yes", "y", "haan", "ha", "ok", "okay", "do it", "kar do"}:
            try:
                result = init_git_repo(path)
                info = validate_project(path)
                PENDING_GITHUB["stage"] = "repo_name"
                PENDING_GITHUB["path"] = info["git_root"]
                suggestion = get_repo_name_suggestion(info["git_root"])
                return github_response(
                    f"Git initialized successfully. What should the GitHub repository name be? Suggested: `{suggestion}`",
                    "github_question", stage="repo_name", result=result,
                )
            except GitHubActionError as exc:
                PENDING_GITHUB.clear()
                return github_response(str(exc), "github_error")
        if low in {"no", "n", "nahi", "cancel", "cancel karo", "stop"}:
            PENDING_GITHUB.clear()
            return github_response("Okay, I cancelled the GitHub workflow.", "github_cancelled")
        return github_response("Please answer yes or no: should I run `git init`?", "github_confirmation", stage=stage)

    if stage == "repo_name":
        # Treat the whole reply as the repo name unless it is obviously a command.
        if low in {"cancel", "cancel karo", "stop", "no", "nahi"}:
            PENDING_GITHUB.clear()
            return github_response("Okay, I cancelled the GitHub workflow.", "github_cancelled")
        try:
            repo_name = normalize_repo_name(text)
        except GitHubActionError as exc:
            return github_response(str(exc), "github_question", stage=stage)
        PENDING_GITHUB["repo_name"] = repo_name
        PENDING_GITHUB["stage"] = "visibility"
        return github_response(
            f"Repository name: `{repo_name}`. Should it be **public** or **private**?",
            "github_question", stage="visibility", repo_name=repo_name,
        )

    if stage == "visibility":
        if "private" in low:
            visibility = "private"
        elif "public" in low:
            visibility = "public"
        else:
            return github_response("Please choose `public` or `private`.", "github_question", stage=stage)
        PENDING_GITHUB["visibility"] = visibility
        PENDING_GITHUB["stage"] = "add_confirm"
        return github_response(
            f"Project: `{path}`\nRepository: `{PENDING_GITHUB['repo_name']}`\nVisibility: `{visibility}`\n\nShall I run `git add .` now? (yes/no)",
            "github_confirmation", stage="add_confirm",
        )

    if stage == "add_confirm":
        if low in {"yes", "y", "haan", "ha", "ok", "okay", "do it", "kar do", "add karo"}:
            try:
                result = git_add(path)
                PENDING_GITHUB["stage"] = "commit_message"
                return github_response(
                    f"`git add .` completed. {result['staged_files']} changed/staged entries found. What commit message should I use? Suggested: `Initial commit`",
                    "github_question", stage="commit_message", result=result,
                )
            except GitHubActionError as exc:
                return github_response(str(exc), "github_error")
        if low in {"no", "n", "nahi", "cancel", "stop"}:
            PENDING_GITHUB.clear()
            return github_response("Okay, I cancelled before staging files.", "github_cancelled")
        return github_response("Please answer yes or no: should I run `git add .`?", "github_confirmation", stage=stage)

    if stage == "commit_message":
        if low in {"cancel", "cancel karo", "stop", "no", "nahi"}:
            PENDING_GITHUB.clear()
            return github_response("Okay, I cancelled the GitHub workflow.", "github_cancelled")
        commit_message = text
        if low in {"yes", "y", "haan", "ha", "ok", "okay", "initial commit"}:
            commit_message = "Initial commit"
        try:
            result = git_commit(path, commit_message)
            PENDING_GITHUB["stage"] = "create_confirm"
            return github_response(
                f"Commit created: `{commit_message}`. Shall I create the GitHub repository `{PENDING_GITHUB['repo_name']}` as {PENDING_GITHUB['visibility']}? (yes/no)",
                "github_confirmation", stage="create_confirm", result=result,
            )
        except GitHubActionError as exc:
            return github_response(str(exc), "github_error")

    if stage == "create_confirm":
        if low in {"yes", "y", "haan", "ha", "ok", "okay", "do it", "kar do", "create karo"}:
            try:
                result = create_github_repo(path, PENDING_GITHUB["repo_name"], PENDING_GITHUB["visibility"])
                PENDING_GITHUB["stage"] = "push_confirm"
                return github_response(
                    f"GitHub repository is ready. Shall I run `git push` and upload the committed code? (yes/no)",
                    "github_confirmation", stage="push_confirm", repo=result,
                )
            except GitHubActionError as exc:
                return github_response(str(exc), "github_error")
        if low in {"no", "n", "nahi", "cancel", "stop"}:
            PENDING_GITHUB.clear()
            return github_response("Repository creation cancelled. Your local commit is still safe.", "github_cancelled")
        return github_response("Please answer yes or no: should I create the GitHub repository?", "github_confirmation", stage=stage)

    if stage == "push_confirm":
        if low in {"yes", "y", "haan", "ha", "ok", "okay", "do it", "kar do", "push karo", "push"}:
            try:
                result = git_push(path)
                repo = open_repo(path)
                PENDING_GITHUB.clear()
                return github_response(
                    f"🎉 Done! Code pushed successfully.\n\nRepository: {repo['url']}\nBranch: {result['branch']}\n\nI opened the repository in your browser.",
                    "github_push_success", push=result, repo=repo,
                )
            except GitHubActionError as exc:
                return github_response(str(exc), "github_error")
        if low in {"no", "n", "nahi", "cancel", "stop"}:
            PENDING_GITHUB.clear()
            return github_response("Okay, I did not push the code. Your local commit and repository are untouched.", "github_cancelled")
        return github_response("Please answer yes or no: should I push the code to GitHub?", "github_confirmation", stage=stage)

    PENDING_GITHUB.clear()
    return github_response("The GitHub workflow state was reset. Please start again.", "github_reset")


def extract_search_query(message: str) -> str:
    """Extract a likely file/folder name from a natural-language command."""
    query = message.strip()

    # Remove common command/context words without touching the target name.
    patterns = [
        r"\baura\b",
        r"\bplease\b",
        r"\bfind\b",
        r"\bsearch\b",
        r"\blocate\b",
        r"\blook\s+for\b",
        r"\bwhere\s+is\b",
        r"\bshow\s+me\b",
        r"\bshow\b",
        r"\bdisplay\b",
        r"\bopen\b",
        r"\bmy\b",
        r"\bmere\b",
        r"\blaptop\b",
        r"\bcomputer\b",
        r"\bpc\b",
        r"\bdrives?\b",
        r"\bin\b",
        r"\bon\b",
        r"\bfrom\b",
        r"\bthe\b",
        r"\ba\b",
        r"\ban\b",
        r"\bthis\b",
        r"\bfolder\b",
        r"\bfiles?\b",
        r"\bdirectory\b",
        r"\bcontents\b",
        r"\bstructure\b",
        r"\bsubfolders\b",
        r"\binside\b",
        r"\bwithin\b",
        r"\ball\b",
        r"\bmein\b",
        r"\bpar\b",
        r"\bke\b",
        r"\bki\b",
        r"\bka\b",
        r"\bandar\b",
        r"\bdikhao\b",
        r"\bkaro\b",
        r"\bkhol(?:o)?\b",
        r"\bdhundo\b",
        r"\bdhoondo\b",
        r"\bkahan\b",
        r"\bhai\b",
        r"\baur\b",
        r"\bsaari\b",
        r"\bsabhi\b",
    ]
    for pattern in patterns:
        query = re.sub(pattern, " ", query, flags=re.IGNORECASE)

    query = re.sub(r"[^\w.\- ]", " ", query)
    return " ".join(query.split()).strip()


def search_local_items(query: str, max_results: int = 50):
    """Search accessible local drive roots for file/folder names; read-only."""
    query = query.strip().casefold()
    if not query:
        raise HTTPException(
            status_code=400,
            detail="Please provide a folder or file name.",
        )

    results: list[dict[str, str]] = []
    scanned_dirs = 0
    errors_skipped = 0
    deadline = time.monotonic() + 60
    completed = True

    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.isdir(root):
            continue

        def on_error(_error):
            nonlocal errors_skipped
            errors_skipped += 1

        for current, dirs, files in os.walk(
            root,
            topdown=True,
            onerror=on_error,
            followlinks=False,
        ):
            scanned_dirs += 1
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in SKIP_SEARCH_DIRS
                and not os.path.islink(os.path.join(current, name))
            ]

            for name in dirs:
                if query in name.casefold():
                    results.append({
                        "name": name,
                        "type": "folder",
                        "path": os.path.join(current, name),
                    })
                    if len(results) >= max_results:
                        completed = False
                        break

            if len(results) >= max_results:
                break

            for name in files:
                if query in name.casefold():
                    results.append({
                        "name": name,
                        "type": "file",
                        "path": os.path.join(current, name),
                    })
                    if len(results) >= max_results:
                        completed = False
                        break

            if len(results) >= max_results:
                break

            if scanned_dirs % 100 == 0 and time.monotonic() >= deadline:
                completed = False
                break

        if not completed:
            break

    return {
        "query": query,
        "results": results,
        "count": len(results),
        "scanned_directories": scanned_dirs,
        "skipped_errors": errors_skipped,
        "completed": completed,
        "message": (
            "Search completed."
            if completed
            else "Search stopped at its time or result limit. Results may be incomplete."
        ),
    }


@app.get("/tools/search")
def search_files_and_folders(
    name: str = Query(..., min_length=1, max_length=200),
):
    return search_local_items(name)


def build_directory_tree(
    path: str,
    max_depth: int = 4,
    max_items: int = 500,
):
    """Build a bounded, read-only tree of a folder."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail="The provided path is not an accessible folder.",
        )

    items_seen = 0
    errors_skipped = 0
    truncated = False

    def scan(folder_path: str, depth: int) -> dict[str, Any]:
        nonlocal items_seen, errors_skipped, truncated

        node: dict[str, Any] = {
            "name": os.path.basename(folder_path) or folder_path,
            "type": "folder",
            "path": folder_path,
            "children": [],
        }

        if depth >= max_depth:
            node["children_truncated"] = True
            return node

        try:
            with os.scandir(folder_path) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda entry: (
                        not entry.is_dir(follow_symlinks=False),
                        entry.name.casefold(),
                    ),
                )

                for entry in entries:
                    if items_seen >= max_items:
                        truncated = True
                        break

                    try:
                        if entry.is_symlink():
                            continue

                        items_seen += 1

                        if entry.is_dir(follow_symlinks=False):
                            child: dict[str, Any] = {
                                "name": entry.name,
                                "type": "folder",
                                "path": entry.path,
                                "children": [],
                            }
                            if entry.name.casefold() in SKIP_TREE_EXPANSION:
                                child["contents_skipped"] = (
                                    "Large or internal folder; not expanded by default."
                                )
                            else:
                                child = scan(entry.path, depth + 1)
                            node["children"].append(child)

                        elif entry.is_file(follow_symlinks=False):
                            node["children"].append({
                                "name": entry.name,
                                "type": "file",
                                "path": entry.path,
                            })

                    except OSError:
                        errors_skipped += 1

        except OSError:
            errors_skipped += 1
            node["error"] = "Folder could not be accessed."

        return node

    tree = scan(path, 0)
    return {
        "root": tree,
        "items_scanned": items_seen,
        "errors_skipped": errors_skipped,
        "truncated": truncated,
        "max_depth": max_depth,
        "max_items": max_items,
    }


@app.get("/tools/tree")
def get_folder_tree(
    path: str = Query(..., min_length=1),
    max_depth: int = Query(4, ge=1, le=10),
    max_items: int = Query(500, ge=1, le=5000),
):
    return build_directory_tree(
        path,
        max_depth=max_depth,
        max_items=max_items,
    )


def get_tree_intent(user_message: str) -> bool:
    return any(phrase in user_message for phrase in [
        "andar ki files",
        "folder ke andar",
        "inside the folder",
        "inside folder",
        "folder contents",
        "show contents",
        "directory tree",
        "folder structure",
        "subfolders",
        "open folder",
        "folder kholo",
        "structure dikhao",
        "files and folders",
        "files aur subfolders",
        "show files",
        "files dikhao",
    ])


def extract_explicit_windows_path(message: str) -> str | None:
    """Extract a drive-letter path, allowing spaces in folder names."""
    match = re.search(r"(?i)\b([a-z]:\\.+)$", message.strip())
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


class LaptopActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    text: str | None = Field(default=None, max_length=2000)
    app: str | None = Field(default=None, max_length=100)
    x: int | None = Field(default=None, ge=0, le=10000)
    y: int | None = Field(default=None, ge=0, le=10000)
    key: str | None = Field(default=None, max_length=80)
    direction: str | None = Field(default=None, max_length=10)
    amount: int | None = Field(default=None, ge=1, le=10)


@app.get("/tools/laptop/status")
def laptop_status():
    """Return basic screen dimensions and supported actions."""
    try:
        from app.laptop_control import get_laptop_status
        return get_laptop_status()
    except LaptopActionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/tools/laptop/action")
def laptop_action(request: LaptopActionRequest):
    """Perform one allowlisted, user-requested desktop action."""
    try:
        return perform_laptop_action(
            action=request.action,
            text=request.text,
            app=request.app,
            x=request.x,
            y=request.y,
            key=request.key,
            direction=request.direction,
            amount=request.amount,
        )
    except LaptopActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



# -------------------------
# VOICE + WEBSITE CONTROL
# -------------------------

WEBSITE_ALIASES = {
    "github": "https://github.com",
    "github.com": "https://github.com",
    "gmail": "https://mail.google.com",
    "gmail.com": "https://mail.google.com",
    "google": "https://www.google.com",
    "google.com": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "youtube.com": "https://www.youtube.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
    "linkedin": "https://www.linkedin.com",
    "gitlab": "https://gitlab.com",
    "stackoverflow": "https://stackoverflow.com",
    "chatgpt": "https://chatgpt.com",
    "leetcode": "https://leetcode.com",
    "leetcode.com": "https://leetcode.com",
    "instagram": "https://www.instagram.com",
    "instagram.com": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "facebook.com": "https://www.facebook.com",
    "twitter": "https://x.com",
    "twitter.com": "https://x.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "reddit.com": "https://www.reddit.com",
}


class VoiceRequest(BaseModel):
    seconds: int = Field(default=5, ge=2, le=10)
    language: str = Field(default="auto", max_length=20)


def open_known_website(name: str) -> dict[str, Any]:
    key = name.strip().casefold()

    if key not in WEBSITE_ALIASES:
        raise LaptopActionError(
            f"Website '{name}' is not supported yet."
        )

    url = WEBSITE_ALIASES[key]
    import webbrowser
    webbrowser.open(url)

    return {
        "ok": True,
        "action": "open_website",
        "website": key,
        "url": url,
        "message": f"Opened {key}.",
    }


def parse_voice_app_command(message: str) -> dict[str, Any] | None:
    """Normalize common Whisper/Hinglish app phrases into the laptop allowlist."""
    low = message.casefold().strip()

    app_aliases = {
        "google chrome": "chrome", "chrome": "chrome", "browser": "chrome",
        "microsoft edge": "edge", "edge": "edge", "firefox": "firefox",
        "notepad": "notepad", "calculator": "calculator", "calc": "calculator",
        "paint": "paint", "explorer": "explorer", "file explorer": "explorer",
        "camera": "camera", "windows camera": "camera", "photos": "photos",
        "clock": "clock", "calendar": "calendar", "mail": "mail",
        "settings": "settings", "task manager": "task manager",
        "snipping tool": "snipping tool", "media player": "media player",
        "vs code": "vscode", "visual studio code": "vscode", "vscode": "vscode",
        "visual studio": "visual studio", "terminal": "terminal",
        "windows terminal": "terminal", "powershell": "powershell",
        "command prompt": "cmd", "cmd": "cmd", "postman": "postman",
        "github desktop": "github desktop", "word": "word",
        "microsoft word": "word", "excel": "excel", "microsoft excel": "excel",
        "powerpoint": "powerpoint", "microsoft powerpoint": "powerpoint",
        "outlook": "outlook", "teams": "teams", "microsoft teams": "teams",
        "whatsapp": "whatsapp", "spotify": "spotify", "discord": "discord",
        "zoom": "zoom", "telegram": "telegram", "obs": "obs studio",
        "obs studio": "obs studio",
    }

    # Prefer longer names so "visual studio code" wins over "code"-like fragments.
    for spoken_name in sorted(app_aliases, key=len, reverse=True):
        if not re.search(rf"\b{re.escape(spoken_name)}\b", low):
            continue

        # Only execute when the sentence clearly asks to open/start/launch the app.
        open_words = [
            "open", "open karo", "kholo", "khol", "chalao", "start",
            "launch", "run", "shuru karo", "chalu karo",
        ]
        if not any(word in low for word in open_words):
            continue

        return {"action": "open_app", "app": app_aliases[spoken_name]}

    return None


async def resolve_desktop_intent(message: str) -> dict[str, Any] | None:
    """Use the local Ollama model only to normalize natural-language commands.

    The model may choose only a small, allowlisted intent vocabulary. It does
    not receive permission to run shell commands or arbitrary Python.
    """
    prompt = f"""
You are AURA's desktop command parser. Convert the user's natural-language
command into ONE JSON object. The command can be English, Hindi, Hinglish,
or have speech-recognition mistakes.

Allowed intent values:
- open_app: {{"intent":"open_app","app":"..."}}
- open_website: {{"intent":"open_website","website":"..."}}
- open_and_type: {{"intent":"open_and_type","app":"...","text":"..."}}
- open_and_key: {{"intent":"open_and_key","app":"...","key":"..."}}
- whatsapp_draft: {{"intent":"whatsapp_draft","contact":"...","message":"..."}}
- search_web: {{"intent":"search_web","query":"..."}}
- none: {{"intent":"none"}}

Known apps include Chrome, Edge, Firefox, Notepad, Calculator, Paint,
Explorer, Camera, Photos, Clock, Calendar, Mail, Settings, Task Manager,
Snipping Tool, Media Player, VS Code, Visual Studio, Terminal, PowerShell,
CMD, Postman, GitHub Desktop, Word, Excel, PowerPoint, Outlook, Teams,
WhatsApp, Spotify, Discord, Zoom, Telegram and OBS Studio.

Known websites include GitHub, Gmail, Google, YouTube, Google Drive,
Google Calendar, LinkedIn, GitLab, Stack Overflow, ChatGPT, LeetCode,
Instagram, Facebook, X/Twitter and Reddit.

Rules:
1. Return JSON only. No markdown and no explanation.
2. Preserve the user's message text exactly when extracting a message to type.
3. "my LeetCode account/profile" means open the LeetCode website; do not ask for credentials.
4. A WhatsApp message must be a draft only. Never return a send intent.
5. If the command is unclear, return {{"intent":"none"}}.

User command:
{message}
"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                OLLAMA_URL,
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": "Return JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("message", {}).get("content", "").strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                return None
            if parsed.get("intent") == "none":
                return None
            return parsed
    except Exception:
        return None


def _normalize_intent(intent: dict[str, Any]) -> dict[str, Any] | None:
    """Convert the model's normalized intent into our safe desktop task format."""
    kind = str(intent.get("intent", "")).casefold().strip()
    aliases = {
        "open_app": "open_app",
        "open_website": "open_website",
        "open_and_type": "open_and_type",
        "open_and_key": "open_and_key",
        "whatsapp_draft": "whatsapp_draft",
        "search_web": "search_web",
    }
    kind = aliases.get(kind)
    if not kind:
        return None

    if kind == "open_app":
        return {"type": "open_app", "app": str(intent.get("app", "")).strip()}
    if kind == "open_website":
        return {"type": "open_website", "website": str(intent.get("website", "")).strip()}
    if kind == "open_and_type":
        return {
            "type": "open_and_type",
            "app": str(intent.get("app", "")).strip(),
            "text": str(intent.get("text", "")).strip(),
        }
    if kind == "open_and_key":
        return {
            "type": "open_and_key",
            "app": str(intent.get("app", "")).strip(),
            "key": str(intent.get("key", "")).strip(),
        }
    if kind == "whatsapp_draft":
        return {
            "type": "whatsapp_draft",
            "contact": str(intent.get("contact", "")).strip(),
            "message": str(intent.get("message", "")).strip(),
        }
    if kind == "search_web":
        return {"type": "search_web", "query": str(intent.get("query", "")).strip()}
    return None


def parse_desktop_task(message: str) -> dict[str, Any] | None:
    """Parse safe, allowlisted multi-step desktop tasks."""
    raw = message.strip()

    # WhatsApp: draft a message, but NEVER send automatically.
    m = re.search(
        r"\b(?:write|type|likho)\s+(?:this\s+)?message\s+to\s+(.+?)\s+"
        r"(?:on|in)\s+whatsapp\s*:\s*(.+)$",
        raw, re.IGNORECASE,
    )
    if m:
        return {
            "type": "whatsapp_draft",
            "contact": m.group(1).strip().strip('"'),
            "message": m.group(2).strip().strip('"'),
        }

    m = re.search(
        r"\b(?:open|launch|start|kholo|khol|chalao)\s+whatsapp\b.*?"
        r"\b(?:write|type|likho|message|msg)\s+(?:to|for|ko)?\s*"
        r"(.+?)\s*:\s*(.+)$",
        raw, re.IGNORECASE,
    )
    if m:
        return {
            "type": "whatsapp_draft",
            "contact": m.group(1).strip().strip('"'),
            "message": m.group(2).strip().strip('"'),
        }

    apps = (
        "chrome|google chrome|browser|edge|microsoft edge|firefox|"
        "notepad|calculator|calc|paint|explorer|file explorer|"
        "camera|photos|clock|calendar|mail|settings|task manager|"
        "snipping tool|media player|vs code|vscode|visual studio code|"
        "visual studio|terminal|windows terminal|powershell|cmd|"
        "postman|github desktop|word|microsoft word|excel|microsoft excel|"
        "powerpoint|microsoft powerpoint|outlook|teams|microsoft teams|"
        "whatsapp|spotify|discord|zoom|telegram|obs|obs studio"
    )

    m = re.search(
        rf"\b(?:open|launch|start|kholo|khol|chalao|open karo)\s+({apps})\b"
        rf".*?\b(?:write|type|likho|enter|daalo)\b\s*[:\-]?\s*(.+)$",
        raw, re.IGNORECASE,
    )
    if m:
        return {
            "type": "open_and_type",
            "app": m.group(1),
            "text": m.group(2).strip().strip('"'),
        }

    m = re.search(
        rf"\b(?:open|launch|start|kholo|khol|chalao)\s+({apps})\b"
        rf".*?\b(?:press|dabao|key press karo)\s+(.+)$",
        raw, re.IGNORECASE,
    )
    if m:
        return {
            "type": "open_and_key",
            "app": m.group(1),
            "key": m.group(2).strip(),
        }

    return None


def execute_desktop_task(task: dict[str, Any]) -> dict[str, Any]:
    """Execute the safe desktop workflows parsed above."""
    task_type = task.get("type")

    if task_type == "open_and_type":
        open_result = perform_laptop_action(
            action="open_app",
            app=task["app"],
        )
        time.sleep(1.5)
        type_result = perform_laptop_action(
            action="type_text",
            text=task["text"],
        )
        return {
            "ok": True,
            "action": "open_and_type",
            "app": task["app"],
            "steps": [open_result, type_result],
            "message": f"Opened {task['app']} and typed the requested text.",
        }

    if task_type == "open_and_key":
        open_result = perform_laptop_action(
            action="open_app",
            app=task["app"],
        )
        time.sleep(1.5)
        key = task["key"]
        key_result = perform_laptop_action(
            action="hotkey" if "+" in key else "press_key",
            key=key,
        )
        return {
            "ok": True,
            "action": "open_and_key",
            "app": task["app"],
            "steps": [open_result, key_result],
            "message": f"Opened {task['app']} and pressed {key}.",
        }

    if task_type == "open_app":
        return perform_laptop_action(action="open_app", app=task["app"])

    if task_type == "open_website":
        website = task["website"].casefold().strip()
        # Map fuzzy model output to an existing alias.
        website_alias = {
            "leetcode": "leetcode", "leetcode account": "leetcode", "leetcode profile": "leetcode",
            "github": "github", "gmail": "gmail", "google": "google", "youtube": "youtube",
            "google drive": "google drive", "drive": "drive", "google calendar": "google calendar",
            "calendar": "calendar", "linkedin": "linkedin", "gitlab": "gitlab",
            "stackoverflow": "stackoverflow", "stack overflow": "stackoverflow", "chatgpt": "chatgpt",
            "instagram": "instagram", "facebook": "facebook", "twitter": "twitter", "x": "x", "reddit": "reddit",
        }
        key = website_alias.get(website)
        if not key:
            raise LaptopActionError(f"I don't recognize the website '{website}'.")
        result = open_known_website(key)
        return result

    if task_type == "search_web":
        query = task["query"].strip()
        if not query:
            raise LaptopActionError("Tell me what you want me to search for.")
        import urllib.parse
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        webbrowser.open(url)
        return {"ok": True, "action": "search_web", "query": query, "message": f"Searching for {query}."}

    if task_type == "whatsapp_draft":
        contact = task["contact"]
        message_text = task["message"]

        webbrowser.open("https://web.whatsapp.com")
        time.sleep(4.0)

        # Open New Chat, search the contact, open the chat, then draft.
        perform_laptop_action(action="hotkey", key="ctrl+alt+n")
        time.sleep(1.0)
        perform_laptop_action(action="type_text", text=contact)
        time.sleep(1.0)
        perform_laptop_action(action="press_key", key="enter")
        time.sleep(1.0)
        perform_laptop_action(action="type_text", text=message_text)

        return {
            "ok": True,
            "action": "whatsapp_draft",
            "contact": contact,
            "message": message_text,
            "message_sent": False,
            "requires_confirmation": True,
            "message_to_user": (
                f"I opened the WhatsApp chat for {contact} and drafted the message. "
                "I did not send it. Say 'send it' if you want to send it."
            ),
        }

    raise LaptopActionError("Unsupported desktop task.")


def parse_website_command(message: str) -> dict[str, Any] | None:
    low = message.casefold().strip()

    # Examples:
    # "open github", "github kholo", "open gmail"
    for name in sorted(WEBSITE_ALIASES, key=len, reverse=True):
        patterns = [
            rf"\b(?:open|launch|visit|go to|kholo|khol|open karo)\s+{re.escape(name)}\b",
            rf"\b{re.escape(name)}\s+(?:open|kholo|khol|chalao)\b",
        ]
        if any(re.search(p, low) for p in patterns):
            return {"name": name}

    return None


@app.get("/voice/status")
def voice_status():
    return {
        "ok": True,
        "mode": "local",
        "speech_to_text": "faster-whisper",
        "text_to_speech": "Windows SAPI via pyttsx3",
    }


@app.post("/voice/listen")
def voice_listen(request: VoiceRequest):
    """Record from the default microphone and transcribe locally."""
    try:
        result = listen_and_transcribe(
            seconds=request.seconds,
            language=request.language,
        )
        return {
            "ok": True,
            "text": result["text"],
            "language": result["language"],
            "message": "Voice command transcribed locally.",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/voice/speak")
def voice_speak(request: ChatRequest):
    try:
        speak_text(request.message)
        return {"ok": True, "message": "Spoken locally."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/voice/command")
async def voice_command(request: VoiceRequest):
    """Listen -> transcribe -> execute the same AURA command pipeline."""
    try:
        transcription = listen_and_transcribe(
            seconds=request.seconds,
            language=request.language,
        )
        spoken_text = transcription["text"].strip()

        if not spoken_text:
            return {
                "ok": False,
                "message": "I could not hear a command.",
            }

        desktop_task = parse_desktop_task(spoken_text)
        if desktop_task:
            try:
                task_result = execute_desktop_task(desktop_task)
                response_text = (
                    task_result.get("message_to_user")
                    or task_result.get("message")
                    or "Done."
                )
                speak_text(response_text)
                return {
                    "ok": True,
                    "input": spoken_text,
                    "language": transcription.get("language"),
                    "action": "desktop_task",
                    "result": task_result,
                }
            except LaptopActionError as exc:
                error_message = str(exc)
                try:
                    speak_text(f"I could not complete that desktop task. {error_message}")
                except Exception:
                    pass
                return {
                    "ok": False,
                    "input": spoken_text,
                    "action": "desktop_task_error",
                    "message": error_message,
                    "task": desktop_task,
                }

        # Natural-language fallback: let local Ollama normalize the command when
        # Whisper wording does not match our exact regexes.
        ai_intent = await resolve_desktop_intent(spoken_text)
        normalized_task = _normalize_intent(ai_intent) if ai_intent else None
        if normalized_task:
            try:
                task_result = execute_desktop_task(normalized_task)
                response_text = (
                    task_result.get("message_to_user")
                    or task_result.get("message")
                    or "Done."
                )
                speak_text(response_text)
                return {
                    "ok": True,
                    "input": spoken_text,
                    "action": "ai_desktop_task",
                    "result": task_result,
                }
            except LaptopActionError as exc:
                error_message = str(exc)
                try:
                    speak_text(f"I could not complete that task. {error_message}")
                except Exception:
                    pass
                return {
                    "ok": False,
                    "input": spoken_text,
                    "action": "ai_desktop_task_error",
                    "message": error_message,
                    "task": normalized_task,
                }

        website_action = parse_website_command(spoken_text)
        if website_action:
            result = open_known_website(website_action["name"])
            speak_text(result["message"])
            return {
                "ok": True,
                "input": spoken_text,
                "action": result,
            }

        # Voice uses a broader app-name normalizer before the regular desktop parser.
        # This makes Hinglish/Whisper variants like "camera kholo", "WhatsApp open karo",
        # and "calculator chalao" use the same allowlisted controls as typed commands.
        voice_action = parse_voice_app_command(spoken_text)
        parsed_action = voice_action or parse_laptop_command(spoken_text)

        if parsed_action is not None:
            # Handle laptop-control errors separately so a failed app action
            # does not become an unexplained voice HTTP 500.
            try:
                result = perform_laptop_action(**parsed_action)
                response_text = str(result.get("message", "Done."))

                try:
                    speak_text(response_text)
                except Exception:
                    pass

                return {
                    "ok": True,
                    "input": spoken_text,
                    "action": "laptop_control",
                    "result": result,
                }

            except LaptopActionError as exc:
                return {
                    "ok": False,
                    "input": spoken_text,
                    "action": "laptop_control_error",
                    "message": str(exc),
                    "parsed_action": parsed_action,
                }

            except Exception as exc:
                # Return the real exception type/message for debugging.
                return {
                    "ok": False,
                    "input": spoken_text,
                    "action": "laptop_control_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "parsed_action": parsed_action,
                }

        # Send every other voice command through the same agent pipeline,
        # including the confirmation-based GitHub workflow.
        agent_result = await agent(ChatRequest(message=spoken_text))
        response_text = agent_result.get("message") or agent_result.get("result", {}).get("message") or "Done."
        speak_text(str(response_text))
        return {
            "ok": True,
            "input": spoken_text,
            "action": "agent",
            "result": agent_result,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {exc}",
        )

@app.get("/github/status")
def github_status():
    """Check local GitHub CLI authentication without changing anything."""
    return github_auth_status()


@app.get("/github/pending")
def github_pending():
    """Show the current local GitHub workflow stage."""
    if not PENDING_GITHUB:
        return {"pending": False}
    safe = dict(PENDING_GITHUB)
    return {"pending": True, "stage": safe.get("stage"), "path": safe.get("path"), "repo_name": safe.get("repo_name"), "visibility": safe.get("visibility")}


@app.post("/agent")
async def agent(message: ChatRequest):
    original_message = message.message.strip()
    user_message = original_message.casefold()

    # Confirmation-based GitHub workflow. Replies like "yes", repo names,
    # "private", and commit messages are consumed while this workflow is pending.
    if PENDING_GITHUB or is_github_push_intent(original_message):
        return handle_github_workflow(original_message)

    # Safe multi-step desktop tasks.
    desktop_task = parse_desktop_task(original_message)
    if desktop_task:
        try:
            task_result = execute_desktop_task(desktop_task)
            return {
                "assistant": "AURA AI",
                "action": "desktop_task",
                "result": task_result,
            }
        except LaptopActionError as exc:
            return {
                "assistant": "AURA AI",
                "action": "desktop_task_error",
                "message": str(exc),
                "task": desktop_task,
            }

    # Natural-language desktop fallback using the local model.
    ai_intent = await resolve_desktop_intent(original_message)
    normalized_task = _normalize_intent(ai_intent) if ai_intent else None
    if normalized_task:
        try:
            task_result = execute_desktop_task(normalized_task)
            return {"assistant": "AURA AI", "action": "ai_desktop_task", "result": task_result}
        except LaptopActionError as exc:
            return {
                "assistant": "AURA AI",
                "action": "ai_desktop_task_error",
                "message": str(exc),
                "task": normalized_task,
            }

    # Website commands such as "GitHub kholo" or "Gmail open karo".
    website_action = parse_website_command(original_message)
    if website_action:
        try:
            result = open_known_website(website_action["name"])
            return {
                "assistant": "AURA AI",
                "action": "open_website",
                "result": result,
            }
        except LaptopActionError as exc:
            return {
                "assistant": "AURA AI",
                "action": "website_error",
                "message": str(exc),
            }


    # First handle explicit, allowlisted desktop commands. Unknown commands continue
    # through the existing search/chat flow instead of being executed speculatively.
    parsed_action = parse_laptop_command(original_message)
    if parsed_action is not None:
        try:
            result = perform_laptop_action(**parsed_action)
            return {"assistant": "AURA AI", "action": "laptop_control", "result": result}
        except LaptopActionError as exc:
            return {"assistant": "AURA AI", "action": "laptop_control_error", "message": str(exc)}

    # Handle approval of a previously displayed folder option.
    option_match = re.search(
        r"\b(?:option|folder|choice)\s*(\d+)\b",
        user_message,
    )
    if option_match and PENDING_FOLDER_CHOICES:
        option_number = int(option_match.group(1))
        if not 1 <= option_number <= len(PENDING_FOLDER_CHOICES):
            return {
                "assistant": "AURA AI",
                "action": "invalid_selection",
                "message": (
                    f"Please choose an option between 1 and "
                    f"{len(PENDING_FOLDER_CHOICES)}."
                ),
                "folders": PENDING_FOLDER_CHOICES,
            }

        selected = PENDING_FOLDER_CHOICES[option_number - 1]
        selected_path = selected["path"]

        if not os.path.isdir(selected_path):
            PENDING_FOLDER_CHOICES.clear()
            return {
                "assistant": "AURA AI",
                "action": "folder_not_found",
                "message": "This folder is no longer accessible.",
                "path": selected_path,
            }

        tree = await asyncio.to_thread(build_directory_tree, selected_path)
        PENDING_FOLDER_CHOICES.clear()
        return {
            "assistant": "AURA AI",
            "action": "show_folder_tree",
            "folder": selected_path,
            "result": tree,
        }

    # Preserve the existing project-files command.
    if any(phrase in user_message for phrase in [
        "list my project files",
        "show my project files",
        "show project files",
        "project ki files",
        "project ke files",
        "files ki list",
    ]):
        result = await list_project_files()
        return {
            "assistant": "AURA AI",
            "action": "list_project_files",
            "result": result,
        }

    tree_intent = get_tree_intent(user_message)

    # If an explicit path is included, use that exact folder.
    explicit_path = extract_explicit_windows_path(original_message)
    if explicit_path and tree_intent:
        if os.path.isdir(explicit_path):
            tree = await asyncio.to_thread(build_directory_tree, explicit_path)
            PENDING_FOLDER_CHOICES.clear()
            return {
                "assistant": "AURA AI",
                "action": "show_folder_tree",
                "folder": os.path.abspath(explicit_path),
                "result": tree,
            }
        return {
            "assistant": "AURA AI",
            "action": "folder_not_found",
            "message": "The specified folder path does not exist or is inaccessible.",
            "path": explicit_path,
        }

    search_intent = tree_intent or any(
        phrase in user_message for phrase in [
            "find ",
            "search ",
            "locate ",
            "look for ",
            "where is ",
            "kahan hai",
            "dhundo",
            "dhoondo",
            "find karo",
            "search karo",
            "laptop mein",
            "mere laptop",
        ]
    )

    if search_intent:
        query = extract_search_query(original_message)
        if not query:
            return {
                "assistant": "AURA AI",
                "action": "clarification",
                "message": "Please tell me the file or folder name you want to find.",
            }

        # A new search invalidates old pending options.
        PENDING_FOLDER_CHOICES.clear()
        result = await asyncio.to_thread(search_local_items, query, 50)

        if tree_intent:
            folders = [
                item for item in result["results"]
                if item["type"] == "folder"
            ]

            if not folders:
                return {
                    "assistant": "AURA AI",
                    "action": "search_local_items",
                    "message": (
                        "No matching folder was found. Try searching by its exact name."
                    ),
                    "result": result,
                }

            if len(folders) > 1:
                PENDING_FOLDER_CHOICES.extend(folders)
                return {
                    "assistant": "AURA AI",
                    "action": "select_folder",
                    "message": (
                        "Multiple matching folders were found. "
                        "Reply with 'Option 1', 'Option 2', etc. to select one."
                    ),
                    "folders": [
                        {
                            "option": index + 1,
                            "name": folder["name"],
                            "type": folder["type"],
                            "path": folder["path"],
                        }
                        for index, folder in enumerate(folders)
                    ],
                    "search_completed": result["completed"],
                }

            folder_path = folders[0]["path"]
            tree = await asyncio.to_thread(build_directory_tree, folder_path)
            return {
                "assistant": "AURA AI",
                "action": "show_folder_tree",
                "folder": folder_path,
                "result": tree,
            }

        return {
            "assistant": "AURA AI",
            "action": "search_local_items",
            "result": result,
        }

    # Normal questions use the local Ollama model.
    return await chat(message)


@app.get("/tools/drives")
async def list_drives():
    """List available local Windows drives."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root):
            drives.append({"name": f"Drive {letter}", "path": root})
    return {"drives": drives}


@app.get("/tools/browse")
async def browse_folder(path: str = Query(..., min_length=3)):
    """Browse a single folder on a local Windows drive, read-only."""
    try:
        target = Path(path).resolve(strict=True)

        if not target.is_absolute() or not target.drive:
            raise HTTPException(
                status_code=400,
                detail="Please provide an absolute local folder path.",
            )

        drive = target.drive
        if (
            len(drive) != 2
            or drive[1] != ":"
            or not os.path.isdir(f"{drive}\\")
        ):
            raise HTTPException(
                status_code=403,
                detail="Only available local drives are supported.",
            )

        if not target.is_dir():
            raise HTTPException(
                status_code=400,
                detail="The requested path is not a folder.",
            )

        items = []
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    is_folder = entry.is_dir(follow_symlinks=False)
                    items.append({
                        "name": entry.name,
                        "type": "folder" if is_folder else "file",
                    })
                except OSError:
                    continue

        items.sort(
            key=lambda item: (
                item["type"] != "folder",
                item["name"].casefold(),
            )
        )
        return {"path": str(target), "items": items}

    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="Windows denied access to this folder.",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Folder not found.")
    except OSError:
        raise HTTPException(
            status_code=400,
            detail="Could not access this folder.",
        )
