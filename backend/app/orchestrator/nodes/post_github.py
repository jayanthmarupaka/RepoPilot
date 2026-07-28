"""
Node: post_to_github
---------------------
Final node on all graph paths. Formats the compiled report as a
Markdown PR comment and posts it via GitHub MCP.

Input state:  report, run_status, patch
Output state: current_node
"""

import structlog

from app.mcp import github_client
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)

# Status badges
_STATUS_BADGE = {
    "clean": "✅ **Clean** — No issues found, all tests pass.",
    "issues_found": "⚠️ **Issues Found** — Review the analysis and test results below.",
    "fixed": "🔧 **Auto-Fixed** — RepoPilot proposed and verified a patch. Review before merging.",
    "failing_escalated": "🔴 **Needs Human Review** — Automated fix attempts exhausted. Manual intervention required.",
}


def _format_comment(report: dict, run_status: str, patch: str | None) -> str:
    """Format the compiled report as a GitHub Markdown PR comment."""

    badge = _STATUS_BADGE.get(run_status, "❓ Unknown status")
    lines = [
        "## 🤖 RepoPilot Review",
        "",
        badge,
        "",
        f"> Run ID: `{report.get('run_id', 'N/A')}` | "
        f"Branch: `{report.get('head_branch', 'N/A')}` | "
        f"SHA: `{report.get('head_sha', 'N/A')[:7]}`",
        "",
    ]

    # ── Analysis section ─────────────────────────────────
    analysis = report.get("analysis", {})
    issues = analysis.get("issues", [])
    lines.append("---")
    lines.append("")
    lines.append("### 🔍 Code Analysis")
    lines.append("")

    if issues:
        lines.append(f"Found **{analysis.get('issue_count', 0)} issue(s)**: "
                     f"{analysis.get('critical_count', 0)} critical, "
                     f"{analysis.get('warning_count', 0)} warnings, "
                     f"{analysis.get('info_count', 0)} info.")
        lines.append("")
        lines.append("| Severity | File | Line | Type | Description |")
        lines.append("|---|---|---|---|---|")
        for issue in issues[:20]:  # cap table at 20 rows
            severity_icon = {"critical": "🔴", "warning": "⚠️", "info": "ℹ️"}.get(
                issue.get("severity", "info"), "❓"
            )
            lines.append(
                f"| {severity_icon} {issue.get('severity', '?')} "
                f"| `{issue.get('file', '?')}` "
                f"| {issue.get('line') or '—'} "
                f"| {issue.get('type', '?')} "
                f"| {issue.get('description', '?')} |"
            )
        if len(issues) > 20:
            lines.append(f"\n_... and {len(issues) - 20} more issues._")
    else:
        lines.append("_No code issues detected by the analyzer._")
    lines.append("")

    # ── Test results section ──────────────────────────────
    tests = report.get("tests", {})
    lines.append("---")
    lines.append("")
    lines.append("### 🧪 Test Results")
    lines.append("")

    if tests.get("passed"):
        lines.append(f"✅ All **{tests.get('total', '?')}** tests passed.")
    else:
        lines.append(
            f"❌ **{tests.get('failed_count', '?')}** test(s) failed "
            f"out of {tests.get('total', '?')} total."
        )
        failures = tests.get("failures", [])
        if failures:
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>Failure details</summary>")
            lines.append("")
            lines.append("```")
            lines.extend(failures[:30])
            lines.append("```")
            lines.append("")
            lines.append("</details>")

    output_snippet = tests.get("output_snippet", "")
    if output_snippet:
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Full pytest output (last 1500 chars)</summary>")
        lines.append("")
        lines.append("```")
        lines.append(output_snippet)
        lines.append("```")
        lines.append("")
        lines.append("</details>")

    lines.append("")

    # ── Patch section ─────────────────────────────────────
    if patch:
        lines.append("---")
        lines.append("")
        fixer_attempts = report.get("fixer_attempts", 0)
        lines.append(f"### 🔧 Proposed Patch ({fixer_attempts} attempt(s))")
        lines.append("")
        if run_status == "fixed":
            lines.append("✅ The following patch was applied and **verified passing** by RepoPilot.")
        else:
            lines.append("⚠️ This patch was applied but tests are **still failing**. Use with caution.")
        lines.append("")
        lines.append("```diff")
        lines.append(patch[:3000])
        if len(patch) > 3000:
            lines.append("# ... patch truncated ...")
        lines.append("```")
        lines.append("")

    # ── Footer ────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append(
        "_Automated review by [RepoPilot](https://github.com/jayanthmarupaka/RepoPilot). "
        "This is an AI-generated review — always verify before merging._"
    )

    return "\n".join(lines)


async def post_to_github(state: GraphState) -> dict:
    """
    LangGraph node: post_to_github

    Formats the report as Markdown and posts it as a PR comment.
    """
    logger.info(
        "node.post_github.start",
        run_id=state["run_id"],
        run_status=state.get("run_status"),
    )

    report = state.get("report", {})
    run_status = state.get("run_status", "issues_found")
    patch = state.get("patch")

    comment_body = _format_comment(report, run_status, patch)

    if state.get("skip_post"):
        # CLI --no-post: don't touch GitHub, print the report instead.
        logger.info(
            "node.post_github.skipped",
            run_id=state["run_id"],
            reason="skip_post flag set",
        )
        print(comment_body)
        return {"current_node": "post_to_github"}

    try:
        await github_client.post_pr_comment(
            repo=state["repo_full_name"],
            pr_number=state["pr_number"],
            body=comment_body,
        )
        logger.info(
            "node.post_github.done",
            repo=state["repo_full_name"],
            pr=state["pr_number"],
        )
    except Exception as e:
        logger.exception("node.post_github.error", error=str(e))
        # Non-fatal — report is compiled, posting failed. Log and continue.

    return {"current_node": "post_to_github"}
