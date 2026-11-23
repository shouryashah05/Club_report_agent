# Extended Problem Statement — Smart Event Analytics Agent

## 1. Problem Statement (base)
College technical clubs run many events each semester, but the faculty coordinator has no quick, fair way to answer *"Which clubs performed best last semester, and why?"* The data is spread across four sources (events, registrations, attendance, feedback) and is often incomplete: organisers forget to log attendance or collect feedback.

**Base system:** a single agent that runs 7 tools in sequence: understand the request → collect data → validate and handle missing data → calculate metrics → rank clubs (weighted score: attendance 45%, feedback 40%, event count 15%) → explain the rankings → generate the report. Missing data is never treated as zero. It is estimated or left out, the score's confidence is adjusted, and every gap is listed in the report.

**Scope:** 5 clubs, 14 events, semester Odd 2025-26. CLI (`agent.py`) plus web dashboard (`app.py`).

## 2. Extension 1 — "Ask the Agent": tool calling with memory
**Gap in the base:** the report is fixed. The coordinator can't ask follow-up questions, and the agent forgets everything between runs.

**What was added (`assistant_agent.py`):**
- **Function calling with Gemini:** 9 tools, each with a name, description and parameter schema. Gemini decides which tools to call and with what arguments; the server runs them and returns the observations until the model writes its answer. The live trace shows each call and result. Bad arguments come back as errors so the model can correct itself.
- **Short-term memory:** the last 6 turns are replayed to the model, so a follow-up like *"what about **its** events?"* resolves to the right club.
- **Long-term memory:** *"Remember that …"* makes the model call `save_memory`. The note is embedded with Gemini and stored on disk. Later questions retrieve it by cosine similarity, even in a new session or after a restart.

## 3. Extension 2 — "Agent Council": multi-agent collaboration
**Gap in the base:** one agent does everything, and nobody checks its work. A club's rank can rest on *estimated* attendance without anyone questioning it.

**What was added (`agent_council.py`):**
- **Role-based agents:** Coordinator, DataAgent, AnalystAgent, AuditorAgent and WriterAgent. Each has a role, a goal and only the tools it needs. The Auditor has no data tools, only judgement.
- **FIPA-ACL messages:** every interaction goes over a message bus as `Message(sender, receiver, performative, content)` and is streamed live to the UI.
- **Collaboration and negotiation:** the Analyst *proposes* a ranking. The Gemini Auditor chooses which weak-data events to stress-test and *requests* a re-rank, then *accepts* or *rejects* the proposal. A rejection triggers a revised proposal. On this data, Robotics Club's #1 holds without the estimated events, while Cybersecurity Club drops from #4 to #5.
- **Human-in-the-loop approval:** the report is written to `output/approved_report.md` only after the Faculty Coordinator approves it.

## 4. Why These Two Extensions
| | Ask the Agent | Agent Council |
|---|---|---|
| Improves | How users **interact** with the analytics | How much the **output can be trusted** |
| Answers | "Ask anything, follow up, remember context" | "Who checked this ranking? Is it robust?" |
| Built on base | The base pipeline's metrics become tools | The base's 7 tools are split across agents |

Both are **additive**: `agent.py` is unchanged, and the original dashboard and `/api/analyze` behave the same. Without network access, both fall back to rule-based behaviour.
