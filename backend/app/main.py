"""
RepoPilot — FastAPI Application
---------------------------------
Endpoints:
  GET  /health                      — Liveness check
  POST /runs                        — Start a new RepoPilot run
  GET  /runs/{run_id}/stream        — SSE stream of node-by-node progress
  GET  /runs/{run_id}               — Final report (polling fallback)
  GET  /                            — Static placeholder UI
"""

import asyncio
import json
import uuid
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.models import RunRequest, RunResponse, RunResult
from app.orchestrator.graph import compiled_graph
from app.tracing.langfuse_setup import get_langfuse_handler

# ── Structured logging ────────────────────────────────────
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), settings.log_level, 20)
    ),
)
logger = structlog.get_logger(__name__)

# ── FastAPI app ───────────────────────────────────────────
app = FastAPI(
    title="RepoPilot",
    description="Multi-agent GitHub PR reviewer and auto-fixer via LangGraph + MCP",
    version="0.1.0",
)

# ── Static files ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── In-memory run store ───────────────────────────────────
# { run_id: {"queue": asyncio.Queue, "result": dict | None} }
_runs: dict[str, dict] = {}


# ── Background task: run the graph ───────────────────────

async def _run_graph_task(run_id: str, pr_url: str):
    """
    Background asyncio task that runs the full LangGraph graph,
    emitting SSE events into the run's queue as nodes complete.
    """
    queue: asyncio.Queue = _runs[run_id]["queue"]

    async def emit(node: str, status: str, summary: str | None = None):
        event = json.dumps({"node": node, "status": status, "summary": summary, "run_id": run_id})
        await queue.put(event)

    initial_state = {
        "pr_url": pr_url,
        "run_id": run_id,
        "skip_post": False,
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

    langfuse_handler = get_langfuse_handler(run_id=run_id, pr_url=pr_url)

    try:
        await emit("orchestrator", "started", "Run started")

        # Accumulate full state across all node chunks
        final_state: dict = {}

        # Stream graph execution node by node
        async for chunk in compiled_graph.astream(
            initial_state,
            config={"callbacks": [langfuse_handler], "run_id": run_id},
        ):
            # chunk is a dict: { node_name: partial_state }
            for node_name, node_state in chunk.items():
                if node_name == "__end__":
                    continue
                # Accumulate state
                final_state.update(node_state)

                summary = None
                if node_name == "analyzer":
                    issues = node_state.get("analysis_issues", [])
                    summary = f"Found {len(issues)} issue(s)"
                elif node_name in ("test_runner", "test_runner_retry"):
                    tr = node_state.get("test_results", {})
                    if tr:
                        summary = (
                            f"{'✅ Pass' if tr.get('passed') else '❌ Fail'} — "
                            f"{tr.get('failed_count', 0)} failed / {tr.get('total', 0)} total"
                        )
                elif node_name == "fixer":
                    attempt = node_state.get("attempt_count", "?")
                    summary = f"Attempt {attempt} — patch applied"
                elif node_name == "compile_report":
                    summary = node_state.get("run_status", "")
                elif node_name == "post_to_github":
                    summary = "Comment posted to GitHub"

                await emit(node_name, "done", summary)

        # Save final result from accumulated state
        _runs[run_id]["result"] = {
            "run_id": run_id,
            "run_status": final_state.get("run_status", "unknown"),
            "report": final_state.get("report", {}),
            "error": final_state.get("error"),
        }

    except Exception as e:
        logger.error("run_graph.error", run_id=run_id, error=str(e))
        await emit("error", "error", str(e))
        _runs[run_id]["result"] = {
            "run_id": run_id,
            "run_status": "error",
            "report": {},
            "error": str(e),
        }
    finally:
        # Sentinel to close SSE stream
        await queue.put(None)



# ── Routes ────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health():
    """Liveness check."""
    return {"status": "ok", "env": settings.app_env}


@app.post("/runs", response_model=RunResponse, tags=["Runs"])
async def start_run(request: RunRequest):
    """
    Start a new RepoPilot run for the given GitHub PR URL.
    Returns a run_id to track the run via SSE or polling.
    """
    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _runs[run_id] = {"queue": queue, "result": None}

    # Launch graph as background task
    asyncio.create_task(_run_graph_task(run_id, request.pr_url))

    logger.info("run.started", run_id=run_id, pr_url=request.pr_url)
    return RunResponse(
        run_id=run_id,
        status="started",
        message=f"Run started. Stream at /runs/{run_id}/stream",
    )


@app.get("/runs/{run_id}/stream", tags=["Runs"])
async def stream_run(run_id: str):
    """
    SSE endpoint — streams node-by-node progress of a RepoPilot run.
    Connect with EventSource or `curl -N`.
    Closes automatically when the run completes.
    """
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    queue: asyncio.Queue = _runs[run_id]["queue"]

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=600)  # 10-min timeout
            except asyncio.TimeoutError:
                yield json.dumps({"node": "timeout", "status": "error", "summary": "Run timed out"})
                break

            if event is None:  # Sentinel — run complete
                yield json.dumps({"node": "END", "status": "done", "summary": "Run complete"})
                break

            yield event

    return EventSourceResponse(event_generator())


@app.get("/runs/{run_id}", response_model=RunResult, tags=["Runs"])
async def get_run_result(run_id: str):
    """
    Get the final result of a completed run.
    Returns 202 if the run is still in progress.
    """
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    result = _runs[run_id].get("result")
    if result is None:
        raise HTTPException(status_code=202, detail="Run still in progress")

    return RunResult(**result)


@app.get("/", tags=["Meta"], response_class=HTMLResponse)
async def index():
    """Serve the static SSE UI."""
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()
