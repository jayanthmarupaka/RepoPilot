"""
Node: analyzer
--------------
Fetches the PR diff via GitHub MCP and asks GPT-4.1 to identify:
  - Bugs / logic errors
  - Style / code quality issues
  - Missing test coverage

Outputs structured `analysis_issues` into state.

Input state:  repo_full_name, pr_number, diff (optional — fetched here)
Output state: diff, analysis_issues, current_node
"""

import json
import re

import structlog
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.mcp import github_client
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)

# ── LLM (singleton) ───────────────────────────────────────
_llm: AzureChatOpenAI | None = None


def _get_llm() -> AzureChatOpenAI:
    global _llm
    if _llm is None:
        _llm = AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            azure_deployment=settings.azure_openai_deployment,
            temperature=0.1,
            max_tokens=2048,
        )
    return _llm


SYSTEM_PROMPT = """You are a senior software engineer performing a code review of a GitHub pull request diff.

Analyze the diff carefully and identify:
1. **Bugs** — logic errors, off-by-one errors, incorrect behavior, race conditions
2. **Style** — naming, readability, unnecessary complexity, missing docstrings on public methods
3. **Missing tests** — code paths that are not covered or test cases that should be added

Return your findings as a JSON array. Each finding must have this exact structure:
{
  "file": "<filename>",
  "line": <line_number_or_null>,
  "type": "<bug|style|missing_test>",
  "severity": "<critical|warning|info>",
  "description": "<clear, actionable description of the issue>"
}

Return ONLY the JSON array, no markdown, no preamble, no explanation outside the JSON.
If you find no issues, return an empty array: []
"""


def _parse_issues_from_response(response_text: str) -> list[dict]:
    """
    Parse GPT-4.1 response into a list of issue dicts.
    Handles JSON wrapped in markdown code blocks.
    """
    text = response_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        issues = json.loads(text)
        if isinstance(issues, list):
            return issues
        return []
    except json.JSONDecodeError:
        logger.warning("analyzer.parse_error", raw=text[:200])
        return []


async def analyzer(state: GraphState) -> dict:
    """
    LangGraph node: analyzer

    1. Fetches the PR diff via GitHub MCP
    2. Sends it to GPT-4.1 for structured issue detection
    3. Returns diff + analysis_issues
    """
    logger.info("node.analyzer.start", repo=state["repo_full_name"], pr=state["pr_number"])

    # 1. Fetch diff
    try:
        diff = await github_client.get_pr_diff(
            state["repo_full_name"], state["pr_number"]
        )
    except Exception as e:
        logger.error("node.analyzer.diff_error", error=str(e))
        return {
            "current_node": "analyzer",
            "diff": "",
            "analysis_issues": [],
            "error": f"Failed to fetch PR diff: {e}",
        }

    if not diff.strip():
        logger.info("node.analyzer.empty_diff")
        return {
            "current_node": "analyzer",
            "diff": diff,
            "analysis_issues": [],
        }

    # 2. Analyse with GPT-4.1
    try:
        llm = _get_llm()
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=f"Here is the pull request diff to review:\n\n```diff\n{diff}\n```"
            ),
        ]
        response = await llm.ainvoke(messages)
        issues = _parse_issues_from_response(response.content)
    except Exception as e:
        logger.error("node.analyzer.llm_error", error=str(e))
        issues = []

    logger.info(
        "node.analyzer.done",
        issues_found=len(issues),
        critical=[i for i in issues if i.get("severity") == "critical"],
    )

    return {
        "current_node": "analyzer",
        "diff": diff,
        "analysis_issues": issues,
    }
