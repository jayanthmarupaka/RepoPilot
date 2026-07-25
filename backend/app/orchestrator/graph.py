"""
LangGraph StateGraph — RepoPilot Orchestrator
---------------------------------------------
Defines the full multi-agent graph: all nodes, edges, and conditional
routing for the Fixer retry loop.

Graph structure:
    START
      → orchestrator_plan
      → analyzer
      → test_runner
          ├── [pass]  → compile_report
          └── [fail]  → fixer
                          → test_runner_retry
                              ├── [pass]                → compile_report
                              ├── [fail, attempts < 3]  → fixer (loop)
                              └── [fail, attempts >= 3] → compile_report (escalate)
      → post_to_github
      → END

The compiled graph is exported as `compiled_graph` for use by the
FastAPI routes and the CLI script.
"""

from langgraph.graph import StateGraph, END

from app.config import settings
from app.orchestrator.state import GraphState
from app.orchestrator.nodes.plan import orchestrator_plan
from app.orchestrator.nodes.analyzer import analyzer
from app.orchestrator.nodes.test_runner import test_runner, test_runner_retry
from app.orchestrator.nodes.fixer import fixer
from app.orchestrator.nodes.compile_report import compile_report
from app.orchestrator.nodes.post_github import post_to_github


# ── Routing functions ─────────────────────────────────────

def route_after_plan(state: GraphState) -> str:
    """If plan failed (e.g. bad URL), skip to compile_report."""
    if state.get("error"):
        return "compile_report"
    return "analyzer"


def route_after_test_runner(state: GraphState) -> str:
    """After initial test run: pass → compile_report, fail → fixer."""
    if state.get("test_results", {}).get("passed", False):
        return "compile_report"
    return "fixer"


def route_after_test_runner_retry(state: GraphState) -> str:
    """
    After Fixer + retry run:
      - pass → compile_report
      - fail + attempts < max → fixer (loop)
      - fail + attempts >= max → compile_report (escalate)
    """
    passed = state.get("test_results", {}).get("passed", False)
    attempt_count = state.get("attempt_count", 0)
    max_attempts = settings.max_fixer_attempts

    if passed:
        return "compile_report"
    if attempt_count < max_attempts:
        return "fixer"
    # Exhausted retries — escalate
    return "compile_report"


# ── Graph definition ──────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(GraphState)

    # ── Nodes ─────────────────────────────────────────────
    graph.add_node("orchestrator_plan", orchestrator_plan)
    graph.add_node("analyzer", analyzer)
    graph.add_node("test_runner", test_runner)
    graph.add_node("fixer", fixer)
    graph.add_node("test_runner_retry", test_runner_retry)
    graph.add_node("compile_report", compile_report)
    graph.add_node("post_to_github", post_to_github)

    # ── Entry ─────────────────────────────────────────────
    graph.set_entry_point("orchestrator_plan")

    # ── Edges ─────────────────────────────────────────────
    # plan → analyzer (or compile_report on error)
    graph.add_conditional_edges(
        "orchestrator_plan",
        route_after_plan,
        {
            "analyzer": "analyzer",
            "compile_report": "compile_report",
        },
    )

    # analyzer → test_runner (always)
    graph.add_edge("analyzer", "test_runner")

    # test_runner → compile_report (pass) or fixer (fail)
    graph.add_conditional_edges(
        "test_runner",
        route_after_test_runner,
        {
            "compile_report": "compile_report",
            "fixer": "fixer",
        },
    )

    # fixer → test_runner_retry (always)
    graph.add_edge("fixer", "test_runner_retry")

    # test_runner_retry → compile_report (pass or max retries) or fixer (retry)
    graph.add_conditional_edges(
        "test_runner_retry",
        route_after_test_runner_retry,
        {
            "compile_report": "compile_report",
            "fixer": "fixer",
        },
    )

    # compile_report → post_to_github (always)
    graph.add_edge("compile_report", "post_to_github")

    # post_to_github → END
    graph.add_edge("post_to_github", END)

    return graph


# ── Compiled graph (exported) ─────────────────────────────
compiled_graph = build_graph().compile()
