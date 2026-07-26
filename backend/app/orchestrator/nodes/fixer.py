"""
Node: fixer
------------
When tests fail, the Fixer:
1. Reads the implicated source files via filesystem MCP
2. Prompts GPT-4.1 to propose a minimal patch (unified diff format)
3. Applies the patch to the workdir via filesystem MCP
4. Increments attempt_count

The graph then routes to test_runner_retry to check if the patch fixed it.
Bounded at MAX_FIXER_ATTEMPTS — compile_report handles escalation when exceeded.

Input state:  test_results, diff, analysis_issues, workdir, attempt_count
Output state: patch, attempt_count, current_node
"""

import json
import os
import re

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import AzureChatOpenAI

from app.config import settings
from app.mcp import filesystem_client
from app.orchestrator.state import GraphState

logger = structlog.get_logger(__name__)

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
            max_tokens=4096,
        )
    return _llm


FIXER_SYSTEM_PROMPT = """You are an expert software engineer fixing failing tests.

You will be given:
1. The original PR diff (what was changed)
2. The test failure output from pytest
3. The current content of the relevant source files

Your job is to propose a patch that fixes ALL the test failures.

IMPORTANT: Fix EVERY bug you find in ALL files. Do NOT fix only one file —
include patches for ALL files that have issues causing test failures.
The PR may have introduced bugs in multiple files. Fix them ALL in one response.

Return ONLY a JSON object with this exact structure — no markdown, no extra text:
{
  "explanation": "<one sentence summary of ALL bugs found and fixed>",
  "files": [
    {
      "path": "<relative file path e.g. tinydb/table.py>",
      "patch": "<unified diff patch starting with @@ — only the changed lines>"
    }
  ]
}

The "files" array MUST contain an entry for EVERY file that needs fixing.

The "patch" field must be a valid unified diff fragment, e.g.:
@@ -727,7 +727,7 @@
         current_id = self._next_id
-        self._next_id += 2
+        self._next_id += 1
         return current_id

Rules:
- Fix ALL bugs across ALL files in a single response
- Include a separate entry in "files" for each file that needs patching
- Keep each individual change minimal — revert the specific bug only
- The patch must apply cleanly to the file as provided
- Return ONLY the JSON, no markdown fences, no comments outside JSON
"""


def _extract_implicated_files(failures: list[str], workdir: str) -> list[str]:
    """
    Extract file paths from pytest failure lines.
    e.g. "FAILED tests/test_table.py::TestTable::test_insert" → "tests/test_table.py"
    """
    paths = set()
    for failure in failures:
        match = re.search(r"([\w/\-\.]+\.py)", failure)
        if match:
            rel_path = match.group(1)
            abs_path = os.path.join(workdir, rel_path)
            if os.path.isfile(abs_path):
                paths.add(abs_path)
    return list(paths)


async def fixer(state: GraphState) -> dict:
    """
    LangGraph node: fixer

    Reads implicated files, asks GPT-4.1 for a patch, applies it.
    """
    attempt = state.get("attempt_count", 0) + 1
    logger.info("node.fixer.start", run_id=state["run_id"], attempt=attempt)

    test_results = state.get("test_results", {})
    failures = test_results.get("failures", [])
    workdir = state["workdir"]

    # 1. Identify and read implicated files
    implicated_paths = _extract_implicated_files(failures, workdir)

    # Also include files from the diff that aren't test files
    diff = state.get("diff", "")
    for line in diff.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            rel = line[6:].strip()
            if not rel.startswith("tests/") and not rel.startswith("test_"):
                abs_path = os.path.join(workdir, rel)
                if os.path.isfile(abs_path) and abs_path not in implicated_paths:
                    implicated_paths.append(abs_path)

    # Also include files from analyzer issues — these are the files with known bugs
    analysis_issues = state.get("analysis_issues", [])
    for issue in analysis_issues:
        rel = issue.get("file", "")
        if rel and not rel.startswith("tests/") and not rel.startswith("test_"):
            abs_path = os.path.join(workdir, rel)
            if os.path.isfile(abs_path) and abs_path not in implicated_paths:
                implicated_paths.append(abs_path)

    file_contents = {}
    for abs_path in implicated_paths[:5]:  # cap at 5 files to avoid token overflow
        try:
            content = await filesystem_client.read_file(abs_path)
            rel_path = os.path.relpath(abs_path, workdir)
            file_contents[rel_path] = content
        except Exception as e:
            logger.warning("node.fixer.read_error", path=abs_path, error=str(e))

    if not file_contents:
        logger.warning("node.fixer.no_files", failures=failures)
        return {
            "current_node": "fixer",
            "attempt_count": attempt,
        }

    # 2. Build prompt
    files_section = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in file_contents.items()
    )

    # Include analyzer issues so the fixer knows about ALL bugs
    issues_section = ""
    if analysis_issues:
        issue_lines = []
        for issue in analysis_issues:
            if issue.get("severity") in ("critical", "warning"):
                issue_lines.append(
                    f"- **{issue.get('severity', '?').upper()}** `{issue.get('file', '?')}` "
                    f"line {issue.get('line', '?')}: {issue.get('description', '?')}"
                )
        if issue_lines:
            issues_section = "\n## Code Analyzer Issues (fix ALL of these)\n" + "\n".join(issue_lines)

    human_message = f"""## Original PR Diff
```diff
{diff[:3000]}
```

## Test Failure Output
```
{test_results.get('output', '')[-2000:]}
```
{issues_section}

## Current Source Files
{files_section}

Fix ALL the bugs in ALL files. Return patches for every file that needs fixing."""

    # 3. Call GPT-4.1
    try:
        llm = _get_llm()
        response = await llm.ainvoke([
            SystemMessage(content=FIXER_SYSTEM_PROMPT),
            HumanMessage(content=human_message),
        ])
        raw = response.content.strip()
        # Strip markdown fences if the LLM wrapped the JSON anyway
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        raw = raw.strip()
        patch_proposal = json.loads(raw)
    except json.JSONDecodeError as e:
        # Partial JSON — try to extract explanation at minimum
        logger.error("node.fixer.llm_error", error=str(e))
        return {
            "current_node": "fixer",
            "attempt_count": attempt,
        }
    except Exception as e:
        logger.error("node.fixer.llm_error", error=str(e))
        return {
            "current_node": "fixer",
            "attempt_count": attempt,
        }

    # 4. Apply patch — apply unified diff or write full content
    explanation = patch_proposal.get("explanation", "")
    files_to_write = patch_proposal.get("files", [])
    patch_summary_parts = [f"## Fixer Attempt {attempt}\n**{explanation}**\n"]

    for file_entry in files_to_write:
        rel_path = file_entry.get("path", "")
        if not rel_path:
            continue
        abs_path = os.path.join(workdir, rel_path)

        try:
            if "patch" in file_entry:
                patch_text_raw = file_entry["patch"]
                patch_applied = _apply_unified_diff(abs_path, patch_text_raw)
                if patch_applied:
                    # Include the actual diff in the summary so the developer sees the fix
                    patch_summary_parts.append(f"### `{rel_path}`")
                    patch_summary_parts.append(f"```diff")
                    patch_summary_parts.append(patch_text_raw.strip())
                    patch_summary_parts.append(f"```")
                    logger.info("node.fixer.file_patched", path=rel_path, method="diff")
                else:
                    patch_summary_parts.append(f"### `{rel_path}` — ⚠️ patch failed to apply")
                    logger.warning("node.fixer.patch_failed", path=rel_path)
            elif "content" in file_entry:
                # Full file write (legacy fallback)
                new_content = file_entry["content"]
                if new_content:
                    await filesystem_client.write_file(abs_path, new_content)
                    patch_summary_parts.append(f"### `{rel_path}` — full file replaced")
                    logger.info("node.fixer.file_patched", path=rel_path, method="full")
        except Exception as e:
            logger.error("node.fixer.write_error", path=rel_path, error=str(e))

    this_attempt_patch = "\n".join(patch_summary_parts)

    # Accumulate patches across attempts so the final report shows ALL fixes
    previous_patch = state.get("patch") or ""
    accumulated_patch = (previous_patch + "\n\n" + this_attempt_patch).strip()

    logger.info(
        "node.fixer.done",
        attempt=attempt,
        files_patched=len(files_to_write),
        explanation=explanation[:100],
    )

    return {
        "current_node": "fixer",
        "patch": accumulated_patch,
        "attempt_count": attempt,
    }


def _apply_unified_diff(file_path: str, diff_text: str) -> bool:
    """
    Apply a unified diff fragment to a file in-place.
    Handles LLM-generated diffs which often have:
    - Wrong line numbers in @@ headers
    - Trailing whitespace differences
    - Missing/extra newlines
    Returns True on success, False on failure.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            original_lines = f.readlines()

        # Parse the @@ hunk(s) from the diff
        patched_lines = list(original_lines)
        hunk_header_re = re.compile(
            r"^@@\s+-(?P<start>\d+)(?:,(?P<count>\d+))?\s+\+\d+(?:,\d+)?\s+@@"
        )

        diff_lines = diff_text.splitlines(keepends=True)
        # Ensure every diff line ends with \n for consistent comparison
        diff_lines = [l if l.endswith("\n") else l + "\n" for l in diff_lines]

        offset = 0  # track cumulative line shifts from earlier hunks
        i = 0
        while i < len(diff_lines):
            m = hunk_header_re.match(diff_lines[i])
            if m:
                orig_start = int(m.group("start")) - 1  # 0-indexed
                i += 1
                hunk_orig = []
                hunk_new = []
                while i < len(diff_lines) and not hunk_header_re.match(diff_lines[i]):
                    line = diff_lines[i]
                    if line.startswith("-"):
                        hunk_orig.append(line[1:])
                    elif line.startswith("+"):
                        hunk_new.append(line[1:])
                    else:  # context line (starts with ' ' or is plain text)
                        ctx = line[1:] if line.startswith(" ") else line
                        hunk_orig.append(ctx)
                        hunk_new.append(ctx)
                    i += 1

                if not hunk_orig:
                    # Pure insertion — nothing to match, skip
                    continue

                # Try to find the hunk in the patched lines
                match_pos = _find_hunk(patched_lines, hunk_orig, orig_start + offset)
                if match_pos is not None:
                    end = match_pos + len(hunk_orig)
                    patched_lines[match_pos:end] = hunk_new
                    offset += len(hunk_new) - len(hunk_orig)
                else:
                    logger.warning(
                        "node.fixer.hunk_mismatch",
                        file=file_path,
                        start=orig_start,
                    )
                    return False
            else:
                i += 1

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(patched_lines)
        return True

    except Exception as e:
        logger.error("node.fixer.diff_apply_error", file=file_path, error=str(e))
        return False


def _find_hunk(
    lines: list[str], hunk_orig: list[str], hint_pos: int
) -> int | None:
    """
    Find where `hunk_orig` matches inside `lines`.

    Strategy:
    1. Try exact match at hint_pos (the @@ line number)
    2. Try normalized match at hint_pos (strip trailing whitespace)
    3. Slide a window through the entire file looking for a normalized match
    """
    def _normalize(s: str) -> str:
        return s.rstrip()

    hunk_norm = [_normalize(h) for h in hunk_orig]
    n = len(hunk_orig)

    # 1. Exact match at hint
    if hint_pos >= 0 and hint_pos + n <= len(lines):
        if [l for l in lines[hint_pos : hint_pos + n]] == hunk_orig:
            return hint_pos

    # 2. Normalized match at hint
    if hint_pos >= 0 and hint_pos + n <= len(lines):
        file_norm = [_normalize(l) for l in lines[hint_pos : hint_pos + n]]
        if file_norm == hunk_norm:
            return hint_pos

    # 3. Sliding window — search nearby first (±50 lines), then full file
    search_order = []
    for delta in range(1, 51):
        for pos in (hint_pos - delta, hint_pos + delta):
            if 0 <= pos and pos + n <= len(lines):
                search_order.append(pos)
    # Then the rest of the file
    for pos in range(len(lines) - n + 1):
        if pos not in search_order:
            search_order.append(pos)

    for pos in search_order:
        if pos < 0 or pos + n > len(lines):
            continue
        file_norm = [_normalize(l) for l in lines[pos : pos + n]]
        if file_norm == hunk_norm:
            return pos

    return None

