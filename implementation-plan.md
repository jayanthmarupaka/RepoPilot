# RepoPilot — Implementation Plan for Coding Agent

## Project Summary

RepoPilot is a multi-agent system that reviews a GitHub PR end-to-end:
it analyzes the diff, runs the real test suite in a sandbox, and — if
tests fail — attempts a bounded patch/verify/retry loop before posting
a structured report back to GitHub.

**Core architecture**: LangGraph orchestrator → Analyzer agent → Test
Runner agent → (conditionally) Fixer agent, with **MCP** as the tool
transport layer throughout (not framework-native tool wrappers), and
**Langfuse** tracing every node.

**Stack**: FastAPI, LangGraph, MCP (GitHub MCP server + filesystem/shell
MCP server), Docker/Docker Compose, Azure OpenAI GPT-4.1 (sole LLM),
Langfuse (self-hosted or free cloud tier), SSE for live trace streaming,
single-page HTML frontend (no React).

**Test fixtures already prepared** (do not recreate these — they exist):
- Fork: `https://github.com/jayanthmarupaka/tinydb`
- PR #1 "testing-1" (branch `baseline-clean` → fork `master`): clean
  no-op-equivalent diff, all tests pass. Use as the "clean baseline" case.
- PR #626 "fix: adjust ID increment logic" (branch `bug-off-by-one` →
  fork `master`): intentionally broken — `Table._get_next_id()` in
  `tinydb/table.py` increments `self._next_id` by 2 instead of 1,
  causing document IDs to skip and breaking ID-related tests. Use as
  the "known failure / Fixer" case.

**Constraints for the agent building this**:
- Bound all retry loops (max 3 attempts) — never let the Fixer loop
  indefinitely.
- Sandbox code execution must run in an isolated Docker container per
  run — never execute untrusted repo code directly on the host.
- Use MCP servers as the tool-calling layer for GitHub and
  filesystem/shell access — do not fall back to direct REST/SDK calls
  for these, since demonstrating real MCP usage is the point of the
  project.
- Keep the LLM provider to Azure OpenAI GPT-4.1 only — no other model
  providers, no local model fallback.
- Prefer explicit LangGraph nodes/edges over implicit agent loops —
  the retry/verify logic must be visible in the graph structure, not
  hidden inside a single node's internal logic.

---

## Task 1 — Repo scaffold and environment

**Goal**: a runnable empty skeleton with all config in place before any
agent logic is written.

- [ ] Create project structure:
  ```
  repopilot/
    app/
      main.py              # FastAPI entrypoint
      orchestrator/        # LangGraph graph + node definitions
      agents/               # analyzer.py, test_runner.py, fixer.py
      mcp/                  # MCP client setup/config
      tracing/              # Langfuse setup
    docker/
      Dockerfile
      docker-compose.yml
      sandbox.Dockerfile    # image used for per-run test execution
    static/
      index.html            # SSE trace UI
    .env.example
    requirements.txt
    README.md
  ```
- [ ] `requirements.txt`: `fastapi`, `uvicorn`, `langgraph`, `langchain-openai`
      (or the Azure OpenAI equivalent), `langfuse`, `python-dotenv`,
      `sse-starlette` (or equivalent for SSE), MCP client library.
- [ ] `.env.example` with placeholders: `AZURE_OPENAI_ENDPOINT`,
      `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `GITHUB_TOKEN`,
      `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`.
- [ ] `docker-compose.yml` defining at minimum the app service; leave a
      commented-out block for a self-hosted Langfuse stack if the user
      chooses that path over the cloud free tier.
- [ ] Basic FastAPI app in `main.py` with a `/health` endpoint only.

**Definition of done**: `docker compose up` starts the app container and
`GET /health` returns `200 OK`.

---

## Task 2 — MCP servers: GitHub + filesystem/shell

**Goal**: confirm both MCP servers are reachable and callable before
wiring them into any agent.

- [ ] Stand up the official GitHub MCP server (locally or as a
      container), configured with `GITHUB_TOKEN`.
- [ ] Stand up a filesystem/shell MCP server scoped to a working
      directory used for cloning/testing repos (must NOT have access
      to the host filesystem beyond that directory).
- [ ] Write a small standalone script (`scripts/test_mcp_github.py`)
      that connects to the GitHub MCP server and fetches the diff for
      PR #1 (`jayanthmarupaka/tinydb` PR #1) — print the diff to
      confirm connectivity.
- [ ] Write a second script (`scripts/test_mcp_shell.py`) that uses the
      filesystem/shell MCP server to clone the `bug-off-by-one` branch
      into a scratch directory and run `pytest` — confirm you see real
      test failures (the ID-increment bug should surface as failures).

**Definition of done**: both scripts run successfully and print
real data (a diff, and failing test output) — not mocked output.

---

## Task 3 — LangGraph orchestrator skeleton (no Fixer yet)

**Goal**: the graph runs end-to-end for the read-only path: fetch diff
→ analyze → run tests → compile report. No patching yet.

- [ ] Define the LangGraph state schema (e.g. `pr_url`, `diff`,
      `analysis_issues`, `test_results`, `report`).
- [ ] Node: `orchestrator_plan` — validates input, kicks off the run.
- [ ] Node: `analyzer` — calls the GitHub MCP server to fetch the PR
      diff, sends it to Azure OpenAI GPT-4.1 with a prompt asking for
      flagged issues (bugs, style, missing tests), stores structured
      output in state.
- [ ] Node: `test_runner` — uses the filesystem/shell MCP server to
      clone the PR branch into the Docker sandbox and execute the test
      suite; parses pass/fail results into state.
- [ ] Node: `compile_report` — merges analyzer + test runner output
      into a single structured report object.
- [ ] Wire edges: `START → orchestrator_plan → analyzer → test_runner
      → compile_report → END`.
- [ ] Add a CLI or simple script entrypoint that runs the graph against
      PR #1 and PR #626 and prints the final report for both.

**Definition of done**: running against PR #1 produces a report with no
issues / all tests passing; running against PR #626 produces a report
showing the ID-related test failures.

---

## Task 4 — Langfuse tracing

**Goal**: every node's input/output and every MCP/LLM call is visible
as a trace in Langfuse, not just terminal print statements.

- [ ] Wire the Langfuse callback/handler into the LangGraph run
      (either via LangGraph's native callback support or by wrapping
      each node).
- [ ] Confirm a trace appears in the Langfuse dashboard for a full run
      against PR #1, showing each node as a distinct step with
      timing.
- [ ] Confirm the same for PR #626, including the test failure details
      captured in the trace.

**Definition of done**: two full traces (one per PR) are visible and
inspectable in the Langfuse UI, each showing the node sequence and the
LLM calls made by the Analyzer.

---

## Task 5 — Fixer agent + bounded retry loop

**Goal**: extend the graph so failing tests trigger an automated
patch → re-run → retry cycle, bounded at 3 attempts.

- [ ] Node: `fixer` — given the failing test output and the relevant
      diff/file content (fetched via MCP), prompts Azure OpenAI GPT-4.1
      to propose a patch; applies the patch inside the sandbox via the
      filesystem/shell MCP server.
- [ ] Node: `test_runner_retry` — re-runs the test suite after the
      patch is applied (can reuse the Task 3 test runner logic,
      parameterized for a retry context).
- [ ] Conditional edges:
      `test_runner → (tests pass) → compile_report`
      `test_runner → (tests fail) → fixer → test_runner_retry`
      `test_runner_retry → (tests pass) → compile_report`
      `test_runner_retry → (tests still fail, attempts < 3) → fixer` (loop)
      `test_runner_retry → (tests still fail, attempts == 3) →
      compile_report` (escalate as "needs human review", do not loop
      further)
- [ ] Add an `attempt_count` field to the graph state and increment it
      on each Fixer cycle.
- [ ] Run the full graph against PR #626 — confirm it either fixes the
      ID-increment bug within 3 attempts, or cleanly escalates to
      "needs human review" without hanging or looping past the bound.

**Definition of done**: PR #626 run produces either a verified passing
patch or a clean escalation message — never an unbounded loop, never a
silent failure.

---

## Task 6 — Post results back to GitHub

**Goal**: close the loop — the report (and patch, if any) is posted as
a real PR comment via MCP.

- [ ] Node: `post_to_github` — formats the compiled report as a
      markdown PR comment (issues found, test results, patch diff if
      applicable) and posts it via the GitHub MCP server's comment
      tool.
- [ ] Wire this as the final step before `END` in all graph paths.
- [ ] Run against PR #1 and PR #626 — confirm both PRs on
      `jayanthmarupaka/tinydb` receive an appropriate comment.

**Definition of done**: visually confirm both real PR comments on
GitHub.

---

## Task 7 — FastAPI endpoints + SSE live trace

**Goal**: a usable interface instead of only CLI scripts.

- [ ] `POST /runs` — accepts a PR URL, starts a RepoPilot run
      asynchronously, returns a run ID.
- [ ] `GET /runs/{run_id}/stream` — SSE endpoint streaming each graph
      node's status as it executes (e.g. `{"node": "analyzer",
      "status": "started"}`, `{"node": "analyzer", "status": "done",
      "summary": "..."}`).
- [ ] `static/index.html` — a single page with a text input for a PR
      URL, a "Run" button, and a live-updating trace panel consuming
      the SSE stream.
- [ ] Manually test the full flow through the browser UI against both
      PR #1 and PR #626.

**Definition of done**: a non-technical observer can paste a PR URL
into the browser, click Run, and watch the agent trace stream live,
ending in the final report.

---

## Task 8 — README and portfolio polish

**Goal**: the repo reads as a finished, explainable project — this is
what gets shown in interviews.

- [ ] Write `README.md` with: problem statement, architecture diagram
      (reuse/adapt the one from the planning doc), setup instructions,
      the "why MCP, why this architecture" rationale, and a short
      section explicitly noting the bounded-retry and sandboxing
      design choices as intentional safety decisions.
- [ ] Record a short demo GIF/video of a real run against PR #626
      (bug found → sandboxed test run → fix attempted → verified →
      posted to GitHub).
- [ ] Add a Langfuse trace screenshot to the README.
- [ ] Add a "Roadmap / what's next" section noting anything cut for
      time (e.g. multi-repo support, more agents, non-Python repos).

**Definition of done**: a stranger can read the README top to bottom
and understand what the project does, why it's built this way, and see
proof (GIF + trace screenshot) that it actually works.

---

## Suggested order for a focused weekend

**Day 1**: Tasks 1 → 2 → 3 → 4 (read-only pipeline, fully traced)
**Day 2**: Tasks 5 → 6 → 7 → 8 (Fixer loop, GitHub integration, UI, README)

**If time runs short**, stop after Task 6 — a working read-only
pipeline with a real Fixer loop and GitHub comment posting is already a
strong, complete artifact. Tasks 7 (UI) and 8 (polish) can be trimmed
to "CLI output + a written README" without losing the core
demonstration value.