# RepoPilot — Multi-Agent PR Reviewer & Auto-Fixer

> Automated GitHub PR analysis, sandboxed test execution, and bounded auto-fix — powered by **LangGraph**, **MCP**, and **Azure OpenAI GPT-4.1**.

---

## What It Does

When you point RepoPilot at a GitHub PR URL, it:

1. **Analyzes the diff** — sends it to GPT-4.1 for structured code review (bugs, style issues, missing test coverage)
2. **Runs the test suite** — clones the branch and executes `pytest` inside an isolated Docker sandbox
3. **Attempts an auto-fix** — if tests fail, GPT-4.1 proposes a patch, applies it in the sandbox, and re-runs tests (bounded at 3 attempts)
4. **Posts results back** — formats a full Markdown report and comments directly on the PR via the GitHub API
5. **Streams everything live** — every agent step is visible in real-time via SSE on the UI

---

## Architecture

```
┌──────────────────────────────────────┐
│         FastAPI  (POST /runs)        │
│         SSE  (GET /runs/{id}/stream) │
└──────────────────┬───────────────────┘
                   │
        ┌──────────▼───────────┐
        │  LangGraph Orchestrator │
        │  (StateGraph)          │
        └──────────┬────────────┘
       ┌───────────┼────────────┐
       │           │            │
  ┌────▼────┐ ┌────▼────┐ ┌────▼─────┐
  │Analyzer │ │  Test   │ │  Fixer   │
  │  Agent  │ │ Runner  │ │  Agent   │
  └────┬────┘ └────┬────┘ └────┬─────┘
       │           │            │
  GitHub MCP  Docker sandbox  Filesystem
  (stdio)     (docker run)    MCP (stdio)
       │
  Langfuse (every node + LLM call traced)
```

**MCP is the tool-transport layer** — both the GitHub MCP server and our custom shell MCP server are spawned as stdio subprocesses. This is an intentional architectural choice: MCP as the protocol, not as a LangChain tool wrapper.

### Agent Flow

```
START → [plan] → [analyzer] → [test_runner]
                                    │
                          ┌─── pass ─┘
                          │
                          └─── fail ──→ [fixer] → [test_runner_retry]
                                                         │
                                              ┌── pass ──┘
                                              ├── fail + attempts < 3 → [fixer]
                                              └── fail + attempts == 3 → escalate
                                                         │
                                                  [compile_report]
                                                         │
                                                  [post_to_github]
                                                         │
                                                        END
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) — explicit StateGraph |
| LLM | Azure OpenAI GPT-4.1 |
| Tool transport | [MCP](https://modelcontextprotocol.io/) — stdio subprocess, both servers |
| GitHub access | [github-mcp-server](https://github.com/github/github-mcp-server) (official) |
| Shell/FS access | Custom shell MCP server (`app/mcp_servers/shell_server.py`) + `@modelcontextprotocol/server-filesystem` |
| Sandboxing | Docker — fresh `docker run --rm` per test execution |
| Tracing | [Langfuse](https://cloud.langfuse.com) — every node, LLM call, and MCP tool call |
| API | FastAPI + Uvicorn |
| Streaming | SSE (`sse-starlette`) |

---

## Test Fixtures

Two curated PRs on a fork of [tinydb](https://github.com/msiemens/tinydb):

| PR | Branch | Expected outcome |
|---|---|---|
| [#1 `testing-1`](https://github.com/jayanthmarupaka/tinydb/pull/1) | `baseline-clean` | ✅ Clean — all tests pass |
| [#626](https://github.com/jayanthmarupaka/tinydb/pull/626) | `bug-off-by-one` | 🔴 `Table._get_next_id()` increments by 2 instead of 1 — triggers Fixer |

---

## Project Structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app + SSE endpoints
│   ├── config.py                # Pydantic Settings (reads .env)
│   ├── models.py                # Request/response schemas
│   ├── orchestrator/
│   │   ├── graph.py             # LangGraph StateGraph
│   │   ├── state.py             # GraphState TypedDict
│   │   └── nodes/               # plan, analyzer, test_runner, fixer, compile_report, post_github
│   ├── mcp/
│   │   ├── github_client.py     # GitHub MCP client (stdio)
│   │   ├── filesystem_client.py # Filesystem MCP client (stdio)
│   │   └── shell_client.py      # Shell MCP client (stdio)
│   ├── mcp_servers/
│   │   └── shell_server.py      # Custom shell-exec MCP server
│   └── tracing/
│       └── langfuse_setup.py    # Langfuse callback handler factory
├── scripts/
│   ├── test_mcp_github.py       # Verify GitHub MCP connectivity
│   ├── test_mcp_shell.py        # Verify shell MCP + Docker sandbox
│   └── run_graph.py             # CLI runner (no FastAPI needed)
├── docker/
│   ├── Dockerfile               # App container
│   ├── sandbox.Dockerfile       # Isolated test execution image
│   └── docker-compose.yml
├── static/
│   └── index.html               # Live SSE trace UI
├── .env.example
└── requirements.txt
```

---

## Prerequisites

Before running, install and configure:

| Requirement | Install |
|---|---|
| Python 3.11+ | [python.org](https://www.python.org/) |
| Docker Desktop | [docker.com](https://www.docker.com/) |
| Node.js 20+ | [nodejs.org](https://nodejs.org/) |
| `github-mcp-server` binary | [GitHub releases v1.7.0+](https://github.com/github/github-mcp-server/releases) — place in `backend/bin/` |
| `@modelcontextprotocol/server-filesystem` | `npm install -g @modelcontextprotocol/server-filesystem` |

---

## Setup & Run

### 1 — Clone and enter the backend

```bash
git clone https://github.com/jayanthmarupaka/RepoPilot.git
cd RepoPilot/backend
```

### 2 — Create your `.env`

```bash
cp .env.example .env
```

Then fill in the values in `.env`:

```env
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<your-key>
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-4.1

GITHUB_TOKEN=<your-pat-with-repo-scope>

LANGFUSE_PUBLIC_KEY=<from cloud.langfuse.com>
LANGFUSE_SECRET_KEY=<from cloud.langfuse.com>
LANGFUSE_HOST=https://cloud.langfuse.com

SANDBOX_WORKDIR=./sandbox_workdir
SANDBOX_IMAGE=repopilot-sandbox
MAX_FIXER_ATTEMPTS=3
```

### 3 — Install Python dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 4 — Install MCP Servers

**Filesystem MCP (Node.js)**:
```bash
npm install -g @modelcontextprotocol/server-filesystem
```

**GitHub MCP (Go Binary)**:
1. Download the latest release from [github/github-mcp-server](https://github.com/github/github-mcp-server/releases)
2. Create a `bin/` directory in the `backend/` folder: `mkdir bin`
3. Extract the executable and place it at `backend/bin/github-mcp-server.exe` (Windows) or `backend/bin/github-mcp-server` (Mac/Linux). The client will automatically detect it here without requiring PATH modifications.

### 5 — Build the Docker sandbox image

```bash
docker build -f docker/sandbox.Dockerfile -t repopilot-sandbox .
```

### 6 — Verify MCP connectivity (optional but recommended)

```bash
# Confirm GitHub MCP server can fetch a real PR diff
python scripts/test_mcp_github.py

# Confirm shell MCP + Docker sandbox can clone and run tests
python scripts/test_mcp_shell.py
```

### 7 — Run via CLI (no server needed)

```bash
# Clean PR — expect: run_status: "clean"
python scripts/run_graph.py --pr-url https://github.com/jayanthmarupaka/tinydb/pull/1

# Failing PR — expect: run_status: "fixed" or "failing_escalated"
python scripts/run_graph.py --pr-url https://github.com/jayanthmarupaka/tinydb/pull/2

# Skip posting the GitHub comment (dry run)
python scripts/run_graph.py --pr-url https://github.com/jayanthmarupaka/tinydb/pull/2 --no-post
```

### 8 — Run the FastAPI server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then open **[http://localhost:8000](http://localhost:8000)** — paste a PR URL and watch the live agent trace.

API docs available at **[http://localhost:8000/docs](http://localhost:8000/docs)**.

### 9 — Run via Docker Compose

```bash
# From backend/
docker compose -f docker/docker-compose.yml up --build
```

> **Note**: Docker Compose mounts `/var/run/docker.sock` so the app container can spawn sibling sandbox containers. This is standard practice for CI-like tooling.

> **Note (Windows)**: The GitHub MCP Server Linux binary is downloaded automatically at image build time. No manual steps needed — `docker compose up --build` handles everything.

---

## API Reference

| Method | Endpoint                | Description                                 |
| --------| -------------------------| ---------------------------------------------|
| `GET`  | `/health`               | Liveness check                              |
| `POST` | `/runs`                 | Start a new run — body: `{"pr_url": "..."}` |
| `GET`  | `/runs/{run_id}/stream` | SSE stream of node-by-node progress         |
| `GET`  | `/runs/{run_id}`        | Final report (polling fallback)             |
| `GET`  | `/docs`                 | Interactive Swagger UI                      |

### Example

```bash
# Start a run
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"pr_url": "https://github.com/jayanthmarupaka/tinydb/pull/2"}'
# → {"run_id": "abc-123...", "status": "started", ...}

# Stream live progress
curl -N http://localhost:8000/runs/abc-123.../stream
# → data: {"node": "plan", "status": "done", "summary": null}
# → data: {"node": "analyzer", "status": "done", "summary": "Found 2 issue(s)"}
# → data: {"node": "test_runner", "status": "done", "summary": "❌ Fail — 3 failed / 45 total"}
# → data: {"node": "fixer", "status": "done", "summary": "Attempt 1 — patch applied"}
# → ...
```

---

## Design Decisions

**Why MCP as the tool transport?**
MCP provides a clean, protocol-level abstraction between agents and tools. Using it here (instead of LangChain's native tool wrappers or direct SDK calls) means each agent only sees typed tool definitions — the transport is swappable (stdio → http) without changing agent code. The custom shell MCP server (`shell_server.py`) also demonstrates protocol-level understanding, not just package usage.

**Why explicit LangGraph nodes over a single agent loop?**
The retry/verify logic (Fixer → re-run → conditional route) is visible as graph edges. This means the control flow is auditable, testable, and debuggable — you can see exactly which node ran, in what order, and why routing decisions were made, in both the Langfuse trace and the SSE stream.

**Why `docker run --rm` for sandboxing?**
Arbitrary code execution is a real risk. Running untrusted repo code in a fresh, network-isolated container (`--network none`) that is destroyed after each run means a malicious repo cannot persist state, exfiltrate secrets, or affect other runs. Docker-in-Docker (DinD) was deliberately avoided — it adds privileged containers and networking complexity for no benefit at this scale.

**Why bounded retries at 3?**
An unbounded Fixer loop burns API budget and can hang indefinitely on genuinely unfixable failures. Three attempts covers the realistic case (a simple bug that takes one or two tries to get right) while providing a clean escalation path ("needs human review") when the LLM can't solve it automatically.

---

## Roadmap

- [ ] Support non-Python repos (Node.js, Go, etc.)
- [ ] Multi-repo concurrent runs
- [ ] Switch MCP transport from `stdio` → `http` for multi-service deployments
- [ ] Persist run history in a database (currently in-memory)
- [ ] Auto-open a GitHub PR with the proposed patch (human-in-the-loop approval)
- [ ] Webhook trigger (auto-run on every new PR)
