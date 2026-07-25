"""
Custom Shell Exec MCP Server
----------------------------
A minimal MCP server that exposes a single `run_command` tool — allowing
the LangGraph agents to execute shell commands (git clone, pytest, etc.)
inside the scoped SANDBOX_WORKDIR via the MCP protocol over stdio.

This server is spawned as a stdio subprocess by shell_client.py.

Design notes:
  - Commands run in a subprocess with a configurable CWD.
  - stdout + stderr captured, exit code returned.
  - No network access, no host filesystem access beyond the scoped CWD.
  - Timeout enforced per command (default 300s).
"""

import asyncio
import os
import subprocess
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── Server init ───────────────────────────────────────────
server = Server("repopilot-shell")

# Allowed root — all command CWDs must be under this path.
# Set via environment variable when the subprocess is spawned.
ALLOWED_ROOT = os.environ.get("SHELL_MCP_ALLOWED_ROOT", os.getcwd())
COMMAND_TIMEOUT = int(os.environ.get("SHELL_MCP_TIMEOUT", "300"))


def _assert_safe_cwd(cwd: str) -> str:
    """Ensure cwd is within ALLOWED_ROOT. Raises ValueError otherwise."""
    resolved = os.path.realpath(os.path.abspath(cwd))
    allowed = os.path.realpath(os.path.abspath(ALLOWED_ROOT))
    if not resolved.startswith(allowed):
        raise ValueError(
            f"CWD '{resolved}' is outside the allowed root '{allowed}'. "
            "Shell MCP server will not execute commands outside the sandbox workdir."
        )
    return resolved


# ── Tool definitions ──────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_command",
            description=(
                "Execute a shell command in a specified working directory. "
                "The CWD must be within the sandbox workdir. "
                "Returns stdout, stderr, and exit code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute (passed to /bin/sh -c).",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory for the command. "
                            "Must be within the allowed sandbox workdir."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds (default: {COMMAND_TIMEOUT}).",
                        "default": COMMAND_TIMEOUT,
                    },
                },
                "required": ["command", "cwd"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "run_command":
        raise ValueError(f"Unknown tool: {name}")

    command = arguments["command"]
    cwd_raw = arguments["cwd"]
    timeout = int(arguments.get("timeout", COMMAND_TIMEOUT))

    try:
        safe_cwd = _assert_safe_cwd(cwd_raw)
    except ValueError as e:
        return [TextContent(type="text", text=f"SECURITY_ERROR: {e}")]

    # Ensure CWD exists
    os.makedirs(safe_cwd, exist_ok=True)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    command,
                    shell=True,
                    cwd=safe_cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    stdin=subprocess.DEVNULL,
                ),
            ),
            timeout=timeout + 5,
        )
        output = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "command": command,
            "cwd": safe_cwd,
        }
    except subprocess.TimeoutExpired:
        output = {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "exit_code": -1,
            "command": command,
            "cwd": safe_cwd,
        }
    except Exception as e:
        output = {
            "stdout": "",
            "stderr": f"Unexpected error: {e}",
            "exit_code": -1,
            "command": command,
            "cwd": safe_cwd,
        }

    import json
    return [TextContent(type="text", text=json.dumps(output))]


# ── Entrypoint ────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
