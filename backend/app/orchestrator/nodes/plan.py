"""
Node: orchestrator_plan
------------------------
First node in the graph. Parses the PR URL, validates it, fetches PR
metadata from GitHub MCP, and sets up the initial run state.

Input state:  pr_url, run_id
Output state: pr_number, repo_full_name, head_branch, head_sha,
              clone_url, attempt_count, patch, workdir, current_node
"""

import os
import re
import uuid

import structlog

from app.config import settings
from app.mcp import github_client
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)

# Match GitHub PR URLs:
# https://github.com/owner/repo/pull/123
_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


def _parse_pr_url(pr_url: str) -> tuple[str, int]:
    """
    Parse a GitHub PR URL into (repo_full_name, pr_number).
    Raises ValueError if the URL is not a valid GitHub PR URL.
    """
    match = _PR_URL_RE.match(pr_url.strip())
    if not match:
        raise ValueError(
            f"Invalid GitHub PR URL: '{pr_url}'. "
            "Expected format: https://github.com/owner/repo/pull/123"
        )
    owner = match.group("owner")
    repo = match.group("repo")
    number = int(match.group("number"))
    return f"{owner}/{repo}", number


async def orchestrator_plan(state: GraphState) -> dict:
    """
    LangGraph node: orchestrator_plan

    Parses the PR URL, fetches PR metadata via GitHub MCP, sets up
    the per-run workdir path, and initialises all counters.
    """
    logger.info("node.plan.start", run_id=state.get("run_id"), pr_url=state.get("pr_url"))

    try:
        repo_full_name, pr_number = _parse_pr_url(state["pr_url"])
    except ValueError as e:
        return {
            "current_node": "plan",
            "error": str(e),
            "run_status": "failing_escalated",
        }

    # Fetch PR metadata via GitHub MCP
    try:
        metadata = await github_client.get_pr_metadata(repo_full_name, pr_number)
    except Exception as e:
        logger.error("node.plan.metadata_error", error=str(e))
        return {
            "current_node": "plan",
            "error": f"Failed to fetch PR metadata: {e}",
            "run_status": "failing_escalated",
        }

    # Per-run workdir: sandbox_workdir/<run_id>/
    run_id = state.get("run_id") or str(uuid.uuid4())
    sandbox_abs = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    workdir = os.path.join(sandbox_abs, run_id)

    logger.info(
        "node.plan.done",
        repo=repo_full_name,
        pr=pr_number,
        branch=metadata.get("head_branch"),
        workdir=workdir,
    )

    return {
        "current_node": "plan",
        "run_id": run_id,
        "pr_number": pr_number,
        "repo_full_name": repo_full_name,
        "head_branch": metadata.get("head_branch", ""),
        "head_sha": metadata.get("head_sha", ""),
        "clone_url": metadata.get("clone_url", ""),
        "workdir": workdir,
        "attempt_count": 0,
        "patch": None,
        "analysis_issues": [],
        "test_results": {},
        "report": {},
        "run_status": "issues_found",
        "error": None,
    }
