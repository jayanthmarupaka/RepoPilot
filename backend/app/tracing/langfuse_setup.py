"""
Langfuse Tracing Setup
-----------------------
Provides a factory function that returns a configured Langfuse
LangChain callback handler, keyed per run_id.

Usage:
    handler = get_langfuse_handler(run_id="abc-123")
    compiled_graph.invoke(state, config={"callbacks": [handler]})

Auto-instruments:
  - All LangChain LLM calls (AzureChatOpenAI) — captured as child spans
  - All LangGraph node invocations — captured as top-level spans

Manual instrumentation (done in each MCP client):
  - MCP tool calls — wrapped with langfuse.span() in github_client, etc.
"""

import structlog
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler  # langfuse v3+ (was langfuse.callback in v2)

from app.config import settings

logger = structlog.get_logger(__name__)

# ── Singleton Langfuse client ─────────────────────────────
_langfuse_client: Langfuse | None = None


def get_langfuse_client() -> Langfuse:
    """Return the singleton Langfuse client."""
    global _langfuse_client
    if _langfuse_client is None:
        _langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        logger.info("langfuse.client_initialized", host=settings.langfuse_host)
    return _langfuse_client


def get_langfuse_handler(run_id: str, pr_url: str = "") -> CallbackHandler:
    """
    Create a Langfuse LangChain callback handler for a specific run.

    In langfuse v3+, CallbackHandler reads LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY, and LANGFUSE_HOST from environment variables.
    The trace_context dict is used to attach run metadata.

    Args:
        run_id: UUID of the RepoPilot run — used as the trace ID.
        pr_url: PR URL — added as trace metadata for easy lookup.

    Returns:
        CallbackHandler to pass as config={"callbacks": [handler]}
    """
    # Ensure the singleton client is alive (validates credentials on first call)
    get_langfuse_client()

    handler = CallbackHandler(
        public_key=settings.langfuse_public_key,
        trace_context={
            "name": f"repopilot-run-{run_id[:8]}",
            "tags": ["repopilot", "pr-review"],
            "metadata": {
                "run_id": run_id,
                "pr_url": pr_url,
            },
        },
    )
    logger.info("langfuse.handler_created", run_id=run_id)
    return handler
