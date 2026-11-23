# Club_report_agent

Smart Event Analytics Agent: ranks college technical clubs from event, registration, attendance and feedback data, with honest handling of missing data. On top of the base report it has two AI agent features powered by Gemini:

- **Ask the Agent**: a conversational analyst that uses Gemini function calling over analytics tools, with short-term and long-term memory.
- **Agent Council**: role-based agents that work together over FIPA-ACL messages, with a Gemini-powered Auditor and Writer, and a human approval step.

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set `GEMINI_API_KEY`. The `.env` file is git-ignored. Without a key, both agents still run in an offline mode that uses rules instead of Gemini.

## Run

```bash
python app.py
```

- Base report dashboard: http://localhost:8000
- AI agents: http://localhost:8000/agents

**Terminal versions:**

```bash
python agent.py              # base report -> output/report.md
python assistant_agent.py    # chat with the agent in the terminal
python agent_council.py      # run the council; asks y/n for approval
```

## What's where

| File | Purpose |
|---|---|
| `agent.py` | Base pipeline of 7 tools, from request to report (unchanged) |
| `llm.py` | Shared Gemini client: `.env` loading, retry with backoff, JSON and text helpers, embeddings |
| `assistant_agent.py` | Ask the Agent: tool registry, Gemini function-calling loop, short-term memory (last 6 turns), long-term memory (Gemini embeddings on disk, cosine-similarity retrieval) |
| `agent_council.py` | Agent Council: Coordinator, DataAgent, AnalystAgent, AuditorAgent (Gemini), WriterAgent (Gemini); message bus; sensitivity checks; accept/reject negotiation; human approval |
| `app.py` | FastAPI server. `/api/analyze` is unchanged; adds `/api/assistant/*` and `/api/council/*` (messages stream live as NDJSON) |
| `static/agents.*` | AI agents page |
| `memory_store/` | Created at runtime; long-term memory |
| `output/approved_report.md` | Written only after a human approves |

## How it works

- **Tool calling:** Gemini receives 9 function declarations (name, description, parameter schema). It picks tools and arguments; the server runs them and returns the results, looping until the model answers. If a tool returns an error, such as an unknown club name, the model can correct the arguments and call again.
- **Memory:** the last 6 turns are replayed to the model on each request, so follow-ups like "what about its events?" work. Facts the user asks it to remember are embedded with `gemini-embedding-001` and stored on disk. Before each answer, the closest memories (cosine similarity ≥ 0.64) are added to the system prompt.
- **Council:** the Analyst proposes a ranking. The Auditor (Gemini) chooses which weak-data events to stress-test and asks the Analyst to re-rank without them. It then reads the result and accepts or rejects; on a rejection the Analyst sends a revised proposal. The Writer (Gemini) explains each club and writes an executive summary. The Coordinator asks the Faculty Coordinator with `query-if`, and the report is published only after approval.
- **Reliability:** Gemini calls retry on 429 and 503 responses with backoff. If Gemini is still unreachable, each agent falls back to rules and the page shows that it did.
