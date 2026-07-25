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

Your job is to propose a MINIMAL patch that fixes the test failures.

Return ONLY a JSON object with this exact structure — no markdown, no extra text:
{
  "explanation": "<one sentence root cause and fix>",
  "files": [
    {
      "path": "<relative file path e.g. tinydb/table.py>",
      "patch": "<unified diff patch starting with @@ — only the changed lines>"
    }
  ]
}

The "patch" field must be a valid unified diff fragment, e.g.:
@@ -727,7 +727,7 @@
         current_id = self._next_id
-        self._next_id += 2
+        self._next_id += 1
         return current_id

Rules:
- Keep changes absolutely minimal — fix the specific bug only
- Only include files that need changing
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

    human_message = f"""## Original PR Diff
```diff
{diff[:3000]}
```

## Test Failure Output
```
{test_results.get('output', '')[-2000:]}
```

## Current Source Files
{files_section}

Please propose a fix."""

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
    patch_summary_parts = [f"# Fixer Attempt {attempt}\n# {explanation}\n"]

    for file_entry in files_to_write:
        rel_path = file_entry.get("path", "")
        if not rel_path:
            continue
        abs_path = os.path.join(workdir, rel_path)

        try:
            if "patch" in file_entry:
                # Apply unified diff patch
                import subprocess as _sp
                patch_text_raw = file_entry["patch"]
                # Write patch to a temp file and apply with `patch` command
                # Fallback: apply manually using difflib
                patch_applied = _apply_unified_diff(abs_path, patch_text_raw)
                if patch_applied:
                    patch_summary_parts.append(f"# Patched (diff): {rel_path}")
                    logger.info("node.fixer.file_patched", path=rel_path, method="diff")
                else:
                    logger.warning("node.fixer.patch_failed", path=rel_path)
            elif "content" in file_entry:
                # Full file write (legacy fallback)
                new_content = file_entry["content"]
                if new_content:
                    await filesystem_client.write_file(abs_path, new_content)
                    patch_summary_parts.append(f"# Replaced (full): {rel_path}")
                    logger.info("node.fixer.file_patched", path=rel_path, method="full")
        except Exception as e:
            logger.error("node.fixer.write_error", path=rel_path, error=str(e))

    patch_text = "\n".join(patch_summary_parts)

    logger.info(
        "node.fixer.done",
        attempt=attempt,
        files_patched=len(files_to_write),
        explanation=explanation[:100],
    )

    return {
        "current_node": "fixer",
        "patch": patch_text,
        "attempt_count": attempt,
    }


def _apply_unified_diff(file_path: str, diff_text: str) -> bool:
    """
    Apply a unified diff fragment to a file in-place.
    Returns True on success, False on failure.
    """
    import difflib
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            original_lines = f.readlines()

        # Parse the @@ hunk(s) from the diff
        patched_lines = list(original_lines)
        hunk_header_re = re.compile(r"^@@\s+-(?P<start>\d+)(?:,(?P<count>\d+))?\s+\+\d+(?:,\d+)?\s+@@")

        diff_lines = diff_text.splitlines(keepends=True)
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
                    else:  # context line
                        ctx = line[1:] if line.startswith(" ") else line
                        hunk_orig.append(ctx)
                        hunk_new.append(ctx)
                    i += 1

                # Find hunk in original and replace
                end = orig_start + len(hunk_orig)
                if patched_lines[orig_start:end] == hunk_orig:
                    patched_lines[orig_start:end] = hunk_new
                else:
                    # Context mismatch — try fuzzy match
                    logger.warning("node.fixer.hunk_mismatch", file=file_path, start=orig_start)
                    return False
            else:
                i += 1

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(patched_lines)
        return True

    except Exception as e:
        logger.error("node.fixer.diff_apply_error", file=file_path, error=str(e))
        return False
