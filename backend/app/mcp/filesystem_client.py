"""
Filesystem MCP Client
---------------------
Wraps the official `@modelcontextprotocol/server-filesystem` Node.js package
over stdio. Creates a fresh session per call — simple and correct.

Used for: reading source files and writing patched files inside the
sandbox workdir during the Fixer cycle.

Scoped to SANDBOX_WORKDIR — the server is started with that path as its
allowed root, so it cannot access anything outside it.

Install: npm install -g @modelcontextprotocol/server-filesystem
"""

import json
import os
import shutil

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import settings

logger = structlog.get_logger(__name__)


def _find_filesystem_server() -> str:
    """
    Locate npx (used to run the filesystem MCP server package).
    On Windows, npm/npx are .cmd scripts so we search for npx.cmd too.
    """
    # Try plain name first (works on Linux/macOS, and Windows if PATH includes npm dir)
    for name in ("npx", "npx.cmd"):
        found = shutil.which(name)
        if found:
            return found

    # Common Node.js install locations on Windows
    import platform
    if platform.system() == "Windows":
        candidates = [
            os.path.join(os.environ.get("APPDATA", ""), "npm", "npx.cmd"),
            os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", "npx.cmd"),
            r"C:\Program Files\nodejs\npx.cmd",
            r"C:\Program Files (x86)\nodejs\npx.cmd",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path

    raise RuntimeError(
        "npx not found. Install Node.js from https://nodejs.org/\n"
        "Then: npm install -g @modelcontextprotocol/server-filesystem"
    )


def _resolve_sandbox_path(path: str) -> str:
    """Resolve a path and assert it's within SANDBOX_WORKDIR."""
    sandbox = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(sandbox, path))
    if not resolved.startswith(sandbox):
        raise ValueError(
            f"Path '{resolved}' is outside sandbox workdir '{sandbox}'. "
            "Filesystem MCP client will not access files outside the sandbox."
        )
    return resolved


def _get_server_params() -> StdioServerParameters:
    sandbox_abs = os.path.realpath(os.path.abspath(settings.sandbox_workdir))
    os.makedirs(sandbox_abs, exist_ok=True)
    npx = _find_filesystem_server()
    return StdioServerParameters(
        command=npx,
        args=["-y", "@modelcontextprotocol/server-filesystem", sandbox_abs],
        env={**os.environ},
    )


async def _call_tool(tool_name: str, arguments: dict):
    """Spawn filesystem MCP server, call one tool, return result, close."""
    params = _get_server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.debug("filesystem_mcp.call_tool", tool=tool_name)
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                raise RuntimeError(
                    f"Filesystem MCP tool '{tool_name}' returned error: {result.content}"
                )
            text = result.content[0].text if result.content else ""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text


# ── Public API ────────────────────────────────────────────

async def read_file(path: str) -> str:
    """
    Read a file from within the sandbox workdir.

    Args:
        path: Absolute path or path relative to SANDBOX_WORKDIR.
    Returns:
        File contents as string.
    """
    safe_path = _resolve_sandbox_path(path)
    result = await _call_tool("read_file", {"path": safe_path})
    if isinstance(result, dict):
        return result.get("contents", "")
    return str(result)


async def write_file(path: str, content: str) -> None:
    """
    Write content to a file within the sandbox workdir.

    Args:
        path: Absolute path or path relative to SANDBOX_WORKDIR.
        content: String content to write.
    """
    safe_path = _resolve_sandbox_path(path)
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    await _call_tool("write_file", {"path": safe_path, "contents": content})
    logger.info("filesystem_mcp.file_written", path=safe_path)


async def list_directory(path: str) -> list[str]:
    """
    List the contents of a directory within the sandbox workdir.

    Args:
        path: Directory path (absolute or relative to SANDBOX_WORKDIR).
    Returns:
        List of entry names.
    """
    safe_path = _resolve_sandbox_path(path)
    result = await _call_tool("list_directory", {"path": safe_path})
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("entries", [])
    return []
