# RepoPilot — Multi-Agent PR Reviewer & Fixer
### Complete Build Plan

---

## 1. Use Case

**Problem**: When a PR is opened on a GitHub repo, a human reviewer has to manually read the diff, run the test suite locally, spot issues, and sometimes fix trivial failures before a "real" review even starts. This is slow and repetitive.

**Solution**: RepoPilot is a multi-agent system that, given a GitHub PR, automatically:
1. Analyzes the code diff for bugs, style issues, and missing test coverage
2. Runs the actual test suite in an isolated sandbox
3. If tests fail, attempts to generate and verify a fix (patch → re-run → confirm pass)
4. Posts a structured review comment back on the PR with findings and (optionally) a proposed patch

**Target user**: an open-source maintainer or a small team lead who wants a first-pass automated PR triage before human review.

**Why this project**: it demonstrates multi-agent coordination, MCP as a real tool-transport layer (not just a LangChain tool wrapper), an agentic coding loop (plan → act → verify → retry), and production-adjacent concerns (sandboxing, observability) — the exact gaps in your current resume.

---

## 2. Architecture

```
                        ┌─────────────────────┐
                        │   FastAPI Gateway    │
                        │  (webhook / CLI in)  │
                        └──────────┬───────────┘
                                   │
                        ┌──────────▼───────────┐
                        │   Orchestrator Agent  │
                        │   (LangGraph graph)   │
                        └──────────┬───────────┘
                 ┌─────────────────┼─────────────────┐
                 │                 │                 │
       ┌─────────▼──────┐ ┌────────▼───────┐ ┌───────▼────────┐
       │ Code Analyzer   │ │  Test Runner   │ │  Fixer Agent   │
       │ Agent           │ │  Agent         │ │  (plan→patch→  │
       │                 │ │                │ │   verify loop) │
       └─────────┬───────┘ └────────┬───────┘ └───────┬────────┘
                 │                  │                  │
        ┌────────▼──────┐  ┌────────▼───────┐ ┌───────▼────────┐
        │ GitHub MCP     │  │ Docker Sandbox │ │ Filesystem/Shell│
        │ Server         │  │ MCP Server     │ │ MCP Server      │
        └────────────────┘  └────────────────┘ └─────────────────┘
                 │
        ┌────────▼────────┐
        │  Langfuse (trace │
        │  every agent step)│
        └──────────────────┘
```

**Flow in words**: FastAPI receives a PR URL → Orchestrator (LangGraph state graph) plans the run → Analyzer reads the diff via GitHub MCP and flags issues → Test Runner executes the suite in a Docker sandbox via a filesystem/shell MCP server → if failures exist, Fixer proposes a patch, applies it in the sandbox, re-runs tests, and loops (bounded retries) until pass or give-up → Orchestrator compiles a final report → posts back to GitHub via MCP → the whole run is traced in Langfuse.

---

## 3. Technologies

| Layer | Tool | Why |
|---|---|---|
| Orchestration | **LangGraph** | explicit control flow for the verify/retry loop; you already know it |
| Tool transport | **MCP** (Model Context Protocol) | the differentiator — real MCP servers, not framework-native tool wrappers |
| GitHub access | **GitHub MCP server** (official) | read diffs, post comments, fetch PR metadata |
| Code execution | **Filesystem/shell MCP server** + **Docker** | sandboxed test runs per repo |
| LLM | **Azure OpenAI GPT-4.1** | what you already have access to |
| Observability | **Langfuse** (self-hosted, free) | trace every agent step, tool call, retry — your interview talking point |
| API layer | **FastAPI** | webhook receiver + status endpoint (you know this cold) |
| Streaming | **SSE** | live agent trace to a simple frontend (reuse your DocMind pattern) |
| Frontend | Single HTML page (no React needed for a weekend scope) | shows live trace, keeps you from losing a day to UI |
| Containerization | **Docker Compose** | sandbox isolation + easy local deploy |

---

## 4. Dataset Needed for Testing

You don't need a "dataset" in the ML sense — you need **real repos and PRs** to test against. Suggested test set:

1. **A small, well-known open-source Python repo with a real test suite** — e.g. a mid-size utility library (100–500 stars range so CI is fast, tests are simple, and you can realistically reproduce failures). Avoid huge repos (slow test suites eat your weekend).
2. **Curate 3–5 PRs manually**:
   - 1 PR that passes cleanly (baseline — confirms Analyzer + Test Runner work without false positives)
   - 1–2 PRs with an intentionally broken test (fork the repo, revert a fix, open a PR against your fork — gives you a guaranteed "Fixer" scenario)
   - 1 PR with a subtle style/logic issue but passing tests (tests the Analyzer's judgment, not just the Test Runner)
3. **Fork it into your own GitHub account** so you have write access to open PRs and post comments without needing permissions on someone else's repo.

This gives you a controlled, reproducible test harness instead of hoping a random live PR behaves the way you expect during a demo.

---

## 5. User Flow

1. User (you, in the demo) pastes a GitHub PR URL into RepoPilot's UI or triggers it via CLI/webhook.
2. RepoPilot immediately shows a "Run started" state with a live trace panel (SSE).
3. As agents work, the trace panel streams each step: *"Analyzer: reading diff…" → "Analyzer: found 2 issues" → "Test Runner: executing suite in sandbox…" → "Test Runner: 1 failure" → "Fixer: proposing patch…" → "Fixer: re-running tests…" → "Fixer: tests pass"*.
4. On completion, RepoPilot shows a structured report: issues found, test results, proposed patch (diff view), and posts the same as a PR comment on GitHub.
5. User can accept/reject the patch (out of scope to auto-merge — keep a human in the loop).

---

## 6. Agent Flow (LangGraph state graph)

```
START
  │
  ▼
[Orchestrator: plan] ──fetch PR diff via GitHub MCP──▶ [Analyzer]
                                                            │
                                                   issues + diff summary
                                                            │
                                                            ▼
                                                     [Test Runner]
                                                    (Docker sandbox exec)
                                                            │
                                          ┌─────────────────┴─────────────────┐
                                    tests pass                          tests fail
                                          │                                    │
                                          ▼                                    ▼
                                 [Orchestrator: compile report]         [Fixer: propose patch]
                                          │                                    │
                                          │                          apply patch in sandbox
                                          │                                    │
                                          │                          [Test Runner: re-run]
                                          │                                    │
                                          │                    ┌───────────────┴──────────────┐
                                          │              tests pass                     still failing
                                          │                    │                                │
                                          │                    ▼                    retry (max N, e.g. 3)
                                          │           [Orchestrator: compile report]             │
                                          │                    │                                 │
                                          │                    │                    exceeded retries → escalate
                                          │                    │                    to "needs human review"
                                          └────────────────────┴─────────────────────────────────┘
                                                                │
                                                                ▼
                                                  [Post comment to GitHub via MCP]
                                                                │
                                                               END
```

**Key design point**: bound the Fixer's retry loop (e.g. max 3 attempts) so a stubborn failure doesn't burn your API budget or hang the demo — this is exactly the kind of "how agents recover, not just act" judgment that's the current bar in the field.

---

## 7. Hosting the Application

For a portfolio project, keep hosting simple and free/cheap:

- **App (FastAPI + LangGraph)**: containerize with Docker, deploy to **Render** or **Fly.io** free/hobby tier — both support Docker Compose-style deploys with minimal config.
- **Sandbox execution**: since test runs need Docker-in-Docker, either (a) run the whole thing on a single VM you control (a $5–6/mo DigitalOcean/Azure B1s VM works fine and avoids DinD headaches on managed PaaS), or (b) keep sandboxed execution local-only for the demo and deploy just the orchestration/UI layer to the cloud, with a note in the README that sandbox execution runs locally by design (for security — arbitrary code execution as a public-facing service is a real risk, and saying so shows judgment).
- **Langfuse**: self-host via their Docker Compose setup on the same VM, or use their free cloud tier to skip the ops overhead entirely — recommended for a weekend timeline.
- **Secrets**: Azure OpenAI key, GitHub token — use environment variables + `.env` (gitignored), never commit.

**Recommendation given your timeline**: don't fight cloud sandboxing this weekend. Run everything (including Docker sandbox exec) on one VM, or even fully local with a public demo video/GIF in the README instead of a live-hosted link. A working local demo with a clear README beats a half-broken cloud deploy.

---

## 8. Requirements & Structured Build Plan

### Prerequisites (before you start coding)
- [ ] Docker + Docker Compose installed
- [ ] GitHub personal access token (repo scope) for your test fork
- [ ] Azure OpenAI endpoint/key (you have this from work — confirm it's fine to use for a personal project, or use a personal OpenAI/Anthropic key instead to keep it clean)
- [ ] A forked test repo with 3–5 curated PRs (see Section 4)
- [ ] Langfuse account (cloud free tier) or self-hosted instance running

### Day 1 — Core pipeline, no fixing yet
1. Scaffold FastAPI app + Docker Compose skeleton
2. Stand up GitHub MCP server, confirm you can fetch a PR diff via MCP call (not the GitHub REST SDK directly — the point is MCP as the transport)
3. Build the LangGraph graph: `START → Orchestrator(plan) → Analyzer → Test Runner → Orchestrator(compile report) → END` (no Fixer loop yet)
4. Wire Langfuse tracing on every node from the start
5. Get the Test Runner executing the real test suite inside a Docker sandbox
6. **Checkpoint**: run against your "clean" baseline PR and your "known failure" PR — confirm accurate pass/fail reporting end-to-end

### Day 2 — Fixer loop, polish
1. Add the Fixer agent + retry subgraph (patch → sandbox re-run → pass/fail branch → bounded retry)
2. Add the GitHub MCP "post comment" step
3. Build the minimal SSE trace UI (single HTML page, reuse your DocMind SSE pattern)
4. Write the README: problem statement, architecture diagram, a recorded GIF/video of a real run, and a Langfuse trace screenshot
5. **Cut list if short on time**: drop the Fixer's auto-patch step first — Analyzer + Test Runner + full MCP wiring + visible trace is already a strong standalone artifact. Add Fixer as "v2" in the README roadmap if you don't get to it.

### What "done" looks like for the portfolio
- A public GitHub repo with a clear README, architecture diagram, and a demo GIF
- At least one Langfuse trace screenshot showing the multi-agent run
- A short "why MCP, why this architecture" section in the README — this is what you'll actually talk about in interviews, more than the code itself