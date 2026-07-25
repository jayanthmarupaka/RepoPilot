"""
Node: compile_report
---------------------
Merges all accumulated state (analysis, test results, patch) into a
single structured `report` dict and sets the final `run_status`.

run_status values:
  "clean"             — no issues, all tests pass, no patch needed
  "issues_found"      — analyzer found issues or tests failed, Fixer not run / not applicable
  "fixed"             — Fixer applied a patch and tests now pass
  "failing_escalated" — Fixer exhausted all attempts, still failing

Input state:  all outputs from analyzer, test_runner, fixer
Output state: report, run_status, current_node
"""

import structlog

from app.config import settings
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)


def _determine_status(state: GraphState) -> str:
    """Determine the final run_status based on current state."""
    test_passed = state.get("test_results", {}).get("passed", False)
    patch = state.get("patch")
    attempt_count = state.get("attempt_count", 0)
    issues = state.get("analysis_issues", [])

    if patch and test_passed:
        return "fixed"

    if not test_passed and attempt_count >= settings.max_fixer_attempts:
        return "failing_escalated"

    if not test_passed or any(
        i.get("severity") in ("critical", "warning") for i in issues
    ):
        return "issues_found"

    return "clean"


async def compile_report(state: GraphState) -> dict:
    """
    LangGraph node: compile_report

    Builds the final structured report dict from all state.
    """
    logger.info("node.compile_report.start", run_id=state["run_id"])

    run_status = _determine_status(state)
    test_results = state.get("test_results", {})
    issues = state.get("analysis_issues", [])

    report = {
        # Run metadata
        "run_id": state["run_id"],
        "pr_url": state["pr_url"],
        "repo": state.get("repo_full_name", ""),
        "pr_number": state.get("pr_number"),
        "head_branch": state.get("head_branch", ""),
        "head_sha": state.get("head_sha", ""),

        # Status
        "run_status": run_status,
        "fixer_attempts": state.get("attempt_count", 0),

        # Analyzer
        "analysis": {
            "issue_count": len(issues),
            "critical_count": sum(1 for i in issues if i.get("severity") == "critical"),
            "warning_count": sum(1 for i in issues if i.get("severity") == "warning"),
            "info_count": sum(1 for i in issues if i.get("severity") == "info"),
            "issues": issues,
        },

        # Test runner
        "tests": {
            "passed": test_results.get("passed", False),
            "total": test_results.get("total", 0),
            "failed_count": test_results.get("failed_count", 0),
            "failures": test_results.get("failures", []),
            "output_snippet": test_results.get("output", "")[-1500:],  # last 1500 chars
        },

        # Fixer
        "patch": state.get("patch"),
    }

    logger.info(
        "node.compile_report.done",
        run_status=run_status,
        issues=len(issues),
        tests_passed=test_results.get("passed"),
    )

    return {
        "current_node": "compile_report",
        "report": report,
        "run_status": run_status,
    }
