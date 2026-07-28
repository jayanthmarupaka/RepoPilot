"""
Node: test_runner / test_runner_retry
--------------------------------------
Clones the PR branch into a per-run sandbox workdir, then runs the
test suite inside an isolated Docker container using `docker run --rm`.

Shared helper `_run_sandbox_pytest` is used by both the initial
test_runner node and the test_runner_retry node (after the Fixer applies a patch).

Input state:  clone_url, head_branch, workdir, run_id, repo_full_name
Output state: test_results, current_node

Docker sandbox:
  - Image: repopilot-sandbox (built from docker/sandbox.Dockerfile)
  - Mount: <workdir>:/workspace
  - Command: sh -c 'pip install -e . -q && pytest --tb=short -q'
  - Container destroyed after each run (--rm)
  - The `docker run` call is made via Python subprocess directly from
    the app container — NOT through MCP. The shell MCP server handles
    git clone; the sandbox container is the execution boundary.
"""

import asyncio
import os
import re
import subprocess
import shutil

import structlog

from app.config import settings
from app.mcp import shell_client
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)


def _find_docker() -> str:
    """
    Locate the docker binary by absolute path.
    shutil.which() may fail inside asyncio executor threads if PATH is not
    fully inherited. Fall back to known install locations on Linux.
    """
    binary = shutil.which("docker")
    if binary:
        return binary
    # Common Linux install locations (docker.io Debian package)
    for candidate in ("/usr/bin/docker", "/usr/local/bin/docker"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        "docker binary not found. Ensure docker.io is installed and docker is on PATH."
    )


# Resolve once at import time so executor threads don't have to re-search each call
try:
    _DOCKER = _find_docker()
except FileNotFoundError:
    _DOCKER = "docker"  # fall back to name, let subprocess raise a clear error


def _rmtree(path: str) -> None:
    """
    Forcefully delete a directory tree, even if it contains read-only files
    (e.g. .git/objects on Windows).
    """
    import platform
    if platform.system() == "Windows":
        subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", path],
            check=False,
            capture_output=True,
        )
    else:
        shutil.rmtree(path, ignore_errors=True)


def _parse_pytest_output(output: str) -> dict:
    """
    Parse pytest stdout/stderr into a structured result dict.

    Returns:
        {
            "passed": bool,
            "total": int,
            "failed_count": int,
            "output": str,
            "failures": list[str],   # short summaries of each failure
        }
    """
    failures = []
    total = 0
    failed_count = 0
    passed = False

    # Extract FAILED lines e.g.: "FAILED tests/test_table.py::TestTable::test_insert - AssertionError"
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failures.append(line.strip())

    # Extract summary line e.g.: "3 failed, 42 passed in 2.34s"
    summary_match = re.search(
        r"(\d+) failed(?:, (\d+) passed)?|(\d+) passed",
        output,
        re.IGNORECASE,
    )
    if summary_match:
        if summary_match.group(1):
            failed_count = int(summary_match.group(1))
            passed_count = int(summary_match.group(2) or 0)
            total = failed_count + passed_count
        elif summary_match.group(3):
            passed_count = int(summary_match.group(3))
            total = passed_count
            failed_count = 0

    passed = failed_count == 0 and ("passed" in output.lower() or "no tests" in output.lower())

    # If docker didn't even start (e.g. image not found), mark as failed
    if "Error response from daemon" in output or "Cannot connect to the Docker daemon" in output:
        passed = False
        failures.append("Docker error: " + output[:200])

    return {
        "passed": passed,
        "total": total,
        "failed_count": failed_count,
        "output": output,
        "failures": failures,
    }


async def _run_sandbox_pytest(workdir: str, run_label: str = "initial") -> dict:
    """
    Run the test suite in a Docker sandbox container.

    Two-phase execution:
    1. pip install -e .  (with network — needed to fetch build tools like hatchling)
    2. pytest            (--network none — isolated for security)

    Args:
        workdir: Absolute path to the cloned repo directory.
        run_label: Label for logging ("initial" or "retry-N").

    Returns:
        Parsed test_results dict.
    """
    # Derive the host-side path for the docker volume mount.
    # When running inside Docker, the container path (/app/sandbox_workdir/<uuid>)
    # is not visible to the host Docker daemon. Use SANDBOX_WORKDIR_HOST if set.
    if settings.sandbox_workdir_host:
        # Replace the container-side base with the host-side base
        container_base = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
        host_workdir = workdir.replace(container_base, settings.sandbox_workdir_host, 1)
        logger.debug(
            "test_runner.path_translation",
            sandbox_workdir_host=settings.sandbox_workdir_host,
            container_base=container_base,
            workdir=workdir,
            host_workdir=host_workdir,
        )
    else:
        host_workdir = workdir
        logger.debug(
            "test_runner.no_host_path",
            workdir=workdir,
            hint="SANDBOX_WORKDIR_HOST is not set — using container path as-is",
        )

    # Use forward slashes for Docker volume mounts (required on Windows)
    workdir_docker = host_workdir.replace("\\", "/")

    logger.info("test_runner.sandbox_run", label=run_label, workdir=workdir, mount=workdir_docker)

    # Phase 1: pip install (WITH network access)
    install_cmd = [
        _DOCKER, "run", "--rm",
        "-v", f"{workdir_docker}:/workspace",
        settings.sandbox_image,
        "sh", "-c", "pip install -e . -q 2>&1",
    ]
    try:
        install_result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                install_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        install_output = install_result.stdout + install_result.stderr
        if install_result.returncode != 0:
            logger.warning("test_runner.install_failed", label=run_label, output=install_output[:300])
            return {
                "passed": False,
                "total": 0,
                "failed_count": 0,
                "output": f"pip install failed:\n{install_output}",
                "output_snippet": install_output[-1500:],
                "failures": [],
            }
    except Exception as e:
        output = f"pip install error: {e}"
        return {
            "passed": False, "total": 0, "failed_count": 0,
            "output": output, "output_snippet": output, "failures": [],
        }

    # Phase 2: pytest (WITHOUT network — isolated sandbox)
    pytest_cmd = [
        _DOCKER, "run", "--rm",
        "-v", f"{workdir_docker}:/workspace",
        "--network", "none",
        settings.sandbox_image,
        "sh", "-c", "pytest --tb=short -q 2>&1",
    ]
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                pytest_cmd,
                capture_output=True,
                text=True,
                timeout=180,
            ),
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = "Sandbox timed out after 180 seconds"
    except Exception as e:
        output = f"Sandbox execution error: {e}"

    parsed = _parse_pytest_output(output)
    logger.info(
        "test_runner.sandbox_done",
        label=run_label,
        passed=parsed["passed"],
        failed=parsed["failed_count"],
    )
    return parsed


async def test_runner(state: GraphState) -> dict:
    """
    LangGraph node: test_runner (initial run)

    Clones the PR branch into workdir, then runs pytest in Docker sandbox.
    """
    logger.info("node.test_runner.start", run_id=state["run_id"])

    workdir = state["workdir"]

    # Clean up if workdir already exists (re-run scenario)
    if os.path.exists(workdir):
        _rmtree(workdir)

    # Clone via shell MCP
    try:
        clone_result = await shell_client.clone_repo_branch(
            clone_url=state["clone_url"],
            branch=state["head_branch"],
            target_dir=workdir,
            github_token=settings.github_token,
        )
        if clone_result["exit_code"] != 0:
            err_msg = f"git clone failed (exit {clone_result['exit_code']}): {clone_result['stderr']}"
            logger.error("node.test_runner.clone_failed", error=err_msg)
            return {
                "current_node": "test_runner",
                "test_results": {
                    "passed": False,
                    "total": 0,
                    "failed_count": 0,
                    "output": err_msg,
                    "failures": [err_msg],
                },
            }
    except Exception as e:
        err_msg = f"Clone exception: {e}"
        logger.error("node.test_runner.clone_exception", error=err_msg)
        return {
            "current_node": "test_runner",
            "test_results": {
                "passed": False,
                "total": 0,
                "failed_count": 0,
                "output": err_msg,
                "failures": [err_msg],
            },
        }

    # Run pytest in sandbox
    test_results = await _run_sandbox_pytest(workdir, run_label="initial")

    return {
        "current_node": "test_runner",
        "test_results": test_results,
    }


async def test_runner_retry(state: GraphState) -> dict:
    """
    LangGraph node: test_runner_retry

    Re-runs pytest in the Docker sandbox after the Fixer has applied a patch.
    The workdir already exists with the patched files — no re-clone needed.
    """
    logger.info(
        "node.test_runner_retry.start",
        run_id=state["run_id"],
        attempt=state["attempt_count"],
    )

    test_results = await _run_sandbox_pytest(
        state["workdir"], run_label=f"retry-{state['attempt_count']}"
    )

    return {
        "current_node": "test_runner_retry",
        "test_results": test_results,
    }
