"""
GitHub MCP Client
-----------------
Wraps the official `github/github-mcp-server` binary over stdio.
Creates a fresh MCP session per call (spawns subprocess, calls tool, closes).
Simple, correct, and avoids the complexity of managing a long-lived subprocess.

Tools used:
  - pull_request_read (method: get)          → PR metadata
  - pull_request_read (method: get_diff)     → fetch full diff directly
  - pull_request_read (method: get_files)    → list of changed files
  - add_issue_comment                        → post a general comment on a PR

Install the binary: https://github.com/github/github-mcp-server/releases
"""

import json
import os
import shutil

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import settings

logger = structlog.get_logger(__name__)


def _find_github_mcp_binary() -> str:
    """
    Locate the github-mcp-server binary.
    Search order:
      1. System PATH  (works if user added it globally)
      2. backend/bin/ (drop-in: just put the exe there, no PATH change needed)
      3. Common install locations
    """
    # 1. System PATH
    binary = shutil.which("github-mcp-server")
    if binary:
        return binary

    # 2. Local bin/ directory next to the project root (Windows-friendly)
    here = os.path.dirname(os.path.abspath(__file__))
    # Walk up from app/mcp/ → app/ → backend root
    backend_root = os.path.normpath(os.path.join(here, "..", ".."))
    local_candidates = [
        os.path.join(backend_root, "bin", "github-mcp-server.exe"),  # Windows
        os.path.join(backend_root, "bin", "github-mcp-server"),       # Linux/macOS
    ]
    for path in local_candidates:
        if os.path.isfile(path):
            return path

    # 3. Common system install locations
    system_candidates = [
        "/usr/local/bin/github-mcp-server",
        os.path.expanduser("~/.local/bin/github-mcp-server"),
        os.path.expanduser("~/AppData/Local/Programs/github-mcp-server/github-mcp-server.exe"),
    ]
    for path in system_candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise RuntimeError(
        "github-mcp-server binary not found.\n"
        "Option A (easiest): place the binary in backend/bin/github-mcp-server.exe\n"
        "Option B: add to system PATH\n"
        "Download from: https://github.com/github/github-mcp-server/releases"
    )


def _get_server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=_find_github_mcp_binary(),
        args=["stdio"],
        env={
            **os.environ,
            "GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token,
        },
    )


async def _call_tool(tool_name: str, arguments: dict):
    """
    Spawn a fresh GitHub MCP server process, call one tool, return result, close.
    Fresh subprocess per call — simple and correct.
    """
    params = _get_server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.debug("github_mcp.call_tool", tool=tool_name, args=arguments)
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                raise RuntimeError(
                    f"GitHub MCP tool '{tool_name}' returned error: {result.content}"
                )
            text = result.content[0].text if result.content else "{}"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text


# ── Public API ────────────────────────────────────────────

async def get_pr_diff(repo: str, pr_number: int) -> str:
    """
    Fetch the unified diff for a PR by aggregating per-file patches.

    Args:
        repo: "owner/repo" e.g. "jayanthmarupaka/tinydb"
        pr_number: PR number integer
    Returns:
        Unified diff string (concatenated per-file patches).
    """
    owner, repo_name = repo.split("/", 1)
    data = await _call_tool(
        "pull_request_read",
        {"method": "get_diff", "owner": owner, "repo": repo_name, "pullNumber": pr_number},
    )
    if isinstance(data, dict) and "diff" in data:
        return str(data["diff"])
    # If the response directly returns a string or the diff is the root element
    return str(data)


async def get_pr_metadata(repo: str, pr_number: int) -> dict:
    """
    Fetch PR metadata: title, body, head branch, base branch, clone URL.

    Args:
        repo: "owner/repo"
        pr_number: PR number integer
    """
    owner, repo_name = repo.split("/", 1)
    data = await _call_tool(
        "pull_request_read",
        {"method": "get", "owner": owner, "repo": repo_name, "pullNumber": pr_number},
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected PR metadata response: {data}")
    # github-mcp-server doesn't include clone_url in pull_request_read responses,
    # so we construct it from the repo full_name.
    head_repo_full = data.get("head", {}).get("repo", {}).get("full_name", "")
    clone_url = f"https://github.com/{head_repo_full}.git" if head_repo_full else ""
    return {
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "head_branch": data.get("head", {}).get("ref", ""),
        "head_sha": data.get("head", {}).get("sha", ""),
        "base_branch": data.get("base", {}).get("ref", "master"),
        "clone_url": clone_url,
        "html_url": data.get("html_url", ""),
    }


async def get_pr_files(repo: str, pr_number: int) -> list[dict]:
    """
    Fetch the list of files changed in a PR.
    Each dict: filename, status, additions, deletions, patch.

    Args:
        repo: "owner/repo"
        pr_number: PR number integer
    """
    owner, repo_name = repo.split("/", 1)
    data = await _call_tool(
        "pull_request_read",
        {"method": "get_files", "owner": owner, "repo": repo_name, "pullNumber": pr_number},
    )
    return data if isinstance(data, list) else []


async def post_pr_comment(repo: str, pr_number: int, body: str) -> None:
    """
    Post a general comment on a PR (not an inline review comment).

    Uses add_issue_comment — PRs are Issues in the GitHub API,
    so issue_number == pr_number. This posts a top-level PR comment.

    Args:
        repo: "owner/repo"
        pr_number: PR number integer
        body: Markdown comment body
    """
    owner, repo_name = repo.split("/", 1)
    await _call_tool(
        "add_issue_comment",
        {
            "owner": owner,
            "repo": repo_name,
            "issue_number": pr_number,
            "body": body,
        },
    )
    logger.info("github_mcp.comment_posted", repo=repo, pr=pr_number)
