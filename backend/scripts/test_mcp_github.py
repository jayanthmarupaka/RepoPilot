"""
Phase 2 Verification Script — GitHub MCP
-----------------------------------------
Connects to the GitHub MCP server via our github_client wrapper and
fetches the diff for PR #1 on jayanthmarupaka/tinydb.

Expected output: real unified diff text printed to stdout.

Usage:
    python scripts/test_mcp_github.py
"""

import asyncio
import os
import sys

# Add backend root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.mcp.github_client import get_pr_diff, get_pr_metadata, get_pr_files


async def main():
    repo = "jayanthmarupaka/tinydb"
    pr_number = 1

    print(f"\n{'='*60}")
    print(f"RepoPilot — GitHub MCP Connectivity Test")
    print(f"Repo: {repo}  |  PR: #{pr_number}")
    print(f"{'='*60}\n")

    # 1. PR metadata
    print("--- PR Metadata -------------------------------------")
    metadata = await get_pr_metadata(repo, pr_number)
    for k, v in metadata.items():
        print(f"  {k}: {v}")
    print()

    # 2. Changed files
    print("--- Changed Files -----------------------------------")
    files = await get_pr_files(repo, pr_number)
    for f in files:
        print(f"  [{f.get('status', '?')}] {f.get('filename', '?')}  "
              f"+{f.get('additions', 0)} -{f.get('deletions', 0)}")
    print()

    # 3. Full diff
    print("--- Unified Diff ------------------------------------")
    diff = await get_pr_diff(repo, pr_number)
    if diff:
        # Print first 80 lines max to avoid flooding the terminal
        lines = diff.splitlines()
        for line in lines[:80]:
            print(line)
        if len(lines) > 80:
            print(f"\n... ({len(lines) - 80} more lines)")
    else:
        print("  (empty diff — this may be expected for a no-op PR)")

    print(f"\n{'='*60}")
    print("[OK] GitHub MCP connectivity: OK")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
