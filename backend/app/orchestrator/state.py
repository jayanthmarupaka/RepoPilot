"""
LangGraph State Schema
-----------------------
All data that flows through the RepoPilot graph lives here.
Nodes read from state and return partial dicts to update it.
"""

from typing import TypedDict


class GraphState(TypedDict):
    # ── Input ──────────────────────────────────────────────
    pr_url: str
    run_id: str                  # UUID4 — ties SSE stream to this run

    # ── Parsed from URL ────────────────────────────────────
    pr_number: int
    repo_full_name: str          # "owner/repo" e.g. "jayanthmarupaka/tinydb"

    # ── PR metadata ────────────────────────────────────────
    head_branch: str             # PR source branch (e.g. "bug-off-by-one")
    head_sha: str                # Commit SHA at PR head
    clone_url: str               # HTTPS clone URL of the head repo

    # ── Analyzer outputs ───────────────────────────────────
    diff: str                    # Raw unified diff from GitHub MCP
    analysis_issues: list        # list[dict]: {file, line, type, description}

    # ── Test runner outputs ────────────────────────────────
    test_results: dict
    # {
    #   "passed": bool,
    #   "total": int,
    #   "failed_count": int,
    #   "output": str,          # full pytest stdout+stderr
    #   "failures": list[str],  # list of failure summaries
    # }

    # ── Fixer outputs ──────────────────────────────────────
    patch: str | None            # Proposed patch in unified diff format (None if not attempted)
    attempt_count: int           # Number of Fixer cycles completed (max = MAX_FIXER_ATTEMPTS)

    # ── Working directory ──────────────────────────────────
    workdir: str                 # Absolute path to the per-run clone dir in sandbox_workdir

    # ── Final ──────────────────────────────────────────────
    report: dict                 # Compiled final report (merged all above)
    run_status: str
    # "clean"             — no issues, all tests pass
    # "issues_found"      — analyzer found issues or tests fail, no fixer run
    # "fixed"             — fixer patched it and tests now pass
    # "failing_escalated" — fixer exhausted all attempts, still failing

    # ── Internal / SSE ─────────────────────────────────────
    current_node: str            # Updated by each node — consumed by SSE emitter
    error: str | None            # Set if a node encounters an unrecoverable error
