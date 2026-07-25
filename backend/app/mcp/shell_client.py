"""
Shell MCP Client
----------------
Wraps our custom `app/mcp_servers/shell_server.py` over stdio.
Creates a fresh session per call — simple and correct.

Used for: git clone and any shell operations agents need to perform.

The shell server enforces that all CWDs stay within
SHELL_MCP_ALLOWED_ROOT (set to SANDBOX_WORKDIR).
"""

import json
import os
import sys
from urllib.parse import urlparse, urlunparse

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import settings

logger = structlog.get_logger(__name__)


def _shell_server_path() -> str:
    """Absolute path to our custom shell MCP server script."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "mcp_servers", "shell_server.py"))


def _get_server_params() -> StdioServerParameters:
    sandbox_abs = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    os.makedirs(sandbox_abs, exist_ok=True)
    return StdioServerParameters(
        command=sys.executable,  # same Python interpreter as the app
        args=[_shell_server_path()],
        env={
            **os.environ,
            "SHELL_MCP_ALLOWED_ROOT": sandbox_abs,
            "SHELL_MCP_TIMEOUT": "300",
        },
    )


async def _call_tool(tool_name: str, arguments: dict):
    """Spawn shell MCP server, call one tool, return result, close."""
    params = _get_server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.debug("shell_mcp.call_tool", tool=tool_name, args=arguments)
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                raise RuntimeError(
                    f"Shell MCP tool '{tool_name}' returned error: {result.content}"
                )
            text = result.content[0].text if result.content else "{}"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"stdout": text, "stderr": "", "exit_code": 0}


# ── Public API ────────────────────────────────────────────

async def run_command(command: str, cwd: str, timeout: int = 300) -> dict:
    """
    Run a shell command in the given CWD (must be within SANDBOX_WORKDIR).

    Args:
        command: Shell command string.
        cwd: Working directory for the command.
        timeout: Max seconds before the command is killed.

    Returns:
        {"stdout": str, "stderr": str, "exit_code": int, "command": str, "cwd": str}
    """
    result = await _call_tool(
        "run_command",
        {"command": command, "cwd": cwd, "timeout": timeout},
    )
    logger.debug(
        "shell_mcp.command_result",
        command=command,
        exit_code=result.get("exit_code"),
    )
    return result


async def clone_repo_branch(
    clone_url: str,
    branch: str,
    target_dir: str,
    github_token: str | None = None,
) -> dict:
    """
    Convenience wrapper: git clone a specific branch into target_dir.

    Args:
        clone_url: HTTPS clone URL of the repo.
        branch: Branch name to clone.
        target_dir: Destination directory (within SANDBOX_WORKDIR).
        github_token: Optional token injected into the clone URL for auth.

    Returns:
        run_command result dict.
    """
    if github_token:
        parsed = urlparse(clone_url)
        authed = parsed._replace(netloc=f"x-access-token:{github_token}@{parsed.netloc}")
        clone_url = urlunparse(authed)

    # Pass `-c credential.helper=` to disable Git Credential Manager on Windows from hanging the background process
    cmd = f"git clone -c credential.helper= --branch {branch} --depth 1 {clone_url} {target_dir}"
    sandbox_abs = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    return await run_command(cmd, cwd=sandbox_abs)
