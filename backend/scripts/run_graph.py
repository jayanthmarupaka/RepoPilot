"""
CLI Runner
-----------
Run the RepoPilot graph against a PR URL from the command line.
Useful for testing without spinning up the FastAPI server.

Usage:
    python scripts/run_graph.py --pr-url https://github.com/jayanthmarupaka/tinydb/pull/1
    python scripts/run_graph.py --pr-url https://github.com/jayanthmarupaka/tinydb/pull/626
    python scripts/run_graph.py --pr-url <url> --no-post   # skip GitHub comment
"""

import argparse
import asyncio
import json
import os
import sys
import uuid

# Add backend root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.orchestrator.graph import compiled_graph
from app.tracing.langfuse_setup import get_langfuse_handler


async def run(pr_url: str, post_comment: bool = True) -> dict:
    run_id = str(uuid.uuid4())
    print(f"\n{'='*60}")
    print(f"RepoPilot CLI Runner")
    print(f"PR URL  : {pr_url}")
    print(f"Run ID  : {run_id}")
    print(f"Post?   : {post_comment}")
    print(f"{'='*60}\n")

    initial_state = {
        "pr_url": pr_url,
        "run_id": run_id,
        # These will be populated by orchestrator_plan
        "pr_number": 0,
        "repo_full_name": "",
        "head_branch": "",
        "head_sha": "",
        "clone_url": "",
        "diff": "",
        "analysis_issues": [],
        "test_results": {},
        "patch": None,
        "attempt_count": 0,
        "workdir": "",
        "report": {},
        "run_status": "",
        "current_node": "",
        "error": None,
    }

    # Langfuse handler — traces every node and LLM call
    langfuse_handler = get_langfuse_handler(run_id=run_id, pr_url=pr_url)

    # Run graph
    final_state = await compiled_graph.ainvoke(
        initial_state,
        config={
            "callbacks": [langfuse_handler],
            "run_id": run_id,
            "configurable": {
                "skip_github_post": not post_comment,
            },
        },
    )

    # Pretty print report
    report = final_state.get("report", {})
    run_status = final_state.get("run_status", "unknown")

    print(f"\n{'='*60}")
    print(f"RESULT: {run_status.upper()}")
    print(f"{'='*60}")
    print(json.dumps(report, indent=2, default=str))

    if final_state.get("error"):
        print(f"\n[!] Error: {final_state['error']}")

    print(f"\n[Langfuse] Trace: {settings.langfuse_host}")
    print(f"   Search for run_id: {run_id}")

    return final_state


def main():
    parser = argparse.ArgumentParser(description="RepoPilot CLI — run graph against a PR URL")
    parser.add_argument(
        "--pr-url",
        required=True,
        help="GitHub PR URL, e.g. https://github.com/jayanthmarupaka/tinydb/pull/1",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        default=False,
        help="Skip posting the review comment to GitHub",
    )
    args = parser.parse_args()
    asyncio.run(run(args.pr_url, post_comment=not args.no_post))


if __name__ == "__main__":
    main()
