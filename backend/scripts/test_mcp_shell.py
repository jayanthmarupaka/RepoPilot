"""
Phase 2 Verification Script — Shell MCP + Docker Sandbox
----------------------------------------------------------
Connects to our custom shell MCP server and:
1. Clones the `bug-off-by-one` branch of jayanthmarupaka/tinydb
2. Runs pytest inside the Docker sandbox container
3. Prints the real test failure output

Expected output: pytest failures related to the ID-increment bug
(`Table._get_next_id()` increments by 2 instead of 1).

Usage:
    python scripts/test_mcp_shell.py

Prerequisites:
    - Docker daemon running
    - repopilot-sandbox image built:
        docker build -f docker/sandbox.Dockerfile -t repopilot-sandbox .
    - SANDBOX_WORKDIR and SANDBOX_IMAGE set in .env
"""

import asyncio
import os
import subprocess
import sys

# Add backend root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.mcp.shell_client import clone_repo_branch, run_command


REPO_URL = "https://github.com/jayanthmarupaka/tinydb.git"
BRANCH = "bug-off-by-one"
RUN_ID = "test-mcp-shell-verify"


async def main():
    print(f"\n{'='*60}")
    print(f"RepoPilot — Shell MCP + Sandbox Connectivity Test")
    print(f"Branch: {BRANCH}")
    print(f"{'='*60}\n")

    sandbox_abs = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    target_dir = os.path.join(sandbox_abs, RUN_ID)

    # Clean up any previous run
    if os.path.exists(target_dir):
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", target_dir], check=False)
        print(f"Cleaned up previous run dir: {target_dir}\n")

    # 1. Clone via shell MCP
    print("--- Step 1: Clone via shell MCP ---------------------")
    clone_result = await clone_repo_branch(
        clone_url=REPO_URL,
        branch=BRANCH,
        target_dir=target_dir,
        github_token=settings.github_token,
    )
    print(f"  exit_code: {clone_result['exit_code']}")
    if clone_result["stdout"]:
        print(f"  stdout: {clone_result['stdout'][:300]}")
    if clone_result["stderr"]:
        print(f"  stderr (git progress): {clone_result['stderr'][:300]}")

    if clone_result["exit_code"] != 0:
        print("\n[X] Clone failed — check GITHUB_TOKEN and network connectivity")
        sys.exit(1)
    print("  [OK] Clone succeeded\n")

    print("--- Step 2: Install repo deps in sandbox ------------")
    install_cmd = [
        "docker", "run", "--rm",
        "-v", f"{target_dir}:/workspace",
        settings.sandbox_image,
        "sh", "-c", "pip install -e . -q 2>&1"
    ]
    install_result = subprocess.run(
        install_cmd, capture_output=True, text=True, timeout=120
    )
    print(f"  exit_code: {install_result.returncode}")
    if install_result.stdout:
        print(f"  stdout: {install_result.stdout[:500]}")
    if install_result.stderr:
        print(f"  stderr: {install_result.stderr[:200]}")
    print()

    print("--- Step 3: Run pytest in Docker sandbox ------------")
    pytest_cmd = [
        "docker", "run", "--rm",
        "-v", f"{target_dir}:/workspace",
        settings.sandbox_image,
        "sh", "-c", "pip install -e . -q && pytest --tb=short -q 2>&1"
    ]
    pytest_result = subprocess.run(
        pytest_cmd, capture_output=True, text=True, timeout=120
    )
    output = pytest_result.stdout + pytest_result.stderr
    print(output[:3000])  # Print up to 3000 chars

    print(f"\n--- Result ------------------------------------------")
    if "failed" in output.lower() or "error" in output.lower():
        print("[OK] Shell MCP + Sandbox: OK — real test failures detected (expected)")
        print("   (The ID-increment bug is causing failures as intended)")
    elif "passed" in output.lower():
        print("[!] All tests passed — check that you're on the bug-off-by-one branch")
    else:
        print("[?] Unexpected output — check Docker and sandbox image setup")

    print(f"{'='*60}\n")

    if os.path.exists(target_dir):
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", target_dir], check=False)
    print(f"Cleaned up: {target_dir}")


if __name__ == "__main__":
    asyncio.run(main())
