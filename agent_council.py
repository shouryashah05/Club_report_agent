"""
"Agent Council" — role-based multi-agent collaboration (Coordinator–Worker)
with FIPA-ACL style messages and a human-in-the-loop approval gate.

The base project (agent.py) is ONE agent running seven tools in sequence.
Here the same tools are split across specialised agents, each with a role,
a goal and a restricted toolset:

  Coordinator  — decomposes the task, dispatches work, owns the approval gate
  DataAgent    — collect_data + validate_and_clean            (deterministic)
  AnalystAgent — calculate_metrics + rank_clubs + re-ranking  (deterministic)
  AuditorAgent — Gemini reviewer: decides which data to stress-test, reads
                 the Analyst's re-ranks and accepts or rejects the proposal
  WriterAgent  — Gemini writer: explains rankings and drafts the report
  Human        — the Faculty Coordinator, who approves or rejects publishing

If the Auditor rejects, the Analyst sends a revised proposal (one negotiation
round). Every interaction is a Message on a shared bus and is streamed to the
UI as it happens. Nothing in agent.py is modified — agents call its functions.
Without a Gemini key the Auditor and Writer fall back to rule-based logic.
"""
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime

import llm
from agent import (OUTPUT_DIR, calculate_metrics, collect_data, explain_rankings, generate_report, rank_clubs,
                   understand_request, validate_and_clean)

HUMAN = "Faculty Coordinator"
MAX_PENDING = 20

# ---------------------------------------------------------------------------
# Communication protocol (FIPA-ACL subset)
# ---------------------------------------------------------------------------
PERFORMATIVES = ["request", "inform", "propose", "accept-proposal", "reject-proposal", "query-if", "agree",
                 "failure"]


@dataclass
class Message:
    sender: str
    receiver: str
    performative: str
    content: str
    data: dict = field(default_factory=dict)
    time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class MessageBus:
    """Shared channel: delivers each message to the receiver's inbox, logs it and notifies listeners."""

    def __init__(self, on_message=None):
        self.log = []
        self.agents = {}
        self.on_message = on_message

    def register(self, agent):
        self.agents[agent.name] = agent

    def send(self, msg: Message):
        assert msg.performative in PERFORMATIVES, msg.performative
        msg.data = llm.to_jsonable(msg.data)
        self.log.append(msg)
        if msg.receiver in self.agents:
            self.agents[msg.receiver].inbox.append(msg)
        if self.on_message:
            self.on_message(msg)
        return msg


class RoleAgent:
    def __init__(self, name, role, goal, tools, bus, uses_llm=False):
        self.name, self.role, self.goal, self.tools, self.uses_llm = name, role, goal, tools, uses_llm
        self.inbox = []
        self.bus = bus
        bus.register(self)

    def send(self, to, performative, content, data=None):
        return self.bus.send(Message(self.name, to, performative, content, data or {}))

    def profile(self):
        brain = (f"Gemini ({llm.MODEL})" if llm.available() else "rules (offline)") if self.uses_llm else "rules"
        return {"name": self.name, "role": self.role, "goal": self.goal, "tools": self.tools, "brain": brain}


def _ranking_list(ranked):
    return [{"rank": int(r.rank), "club": r.club_name, "score": round(r.performance_score, 3),
             "attendance_rate": r.attendance_rate, "avg_feedback": r.avg_feedback,
             "feedback_coverage": r.feedback_coverage, "estimated_attendance": bool(r.has_estimated_attendance)}
            for r in ranked.itertuples()]


def _ranking_text(ranked):
    return ", ".join(f"#{r.rank} {r.club_name} ({r.performance_score:.2f})" for r in ranked.itertuples())


# ---------------------------------------------------------------------------
# Worker agents
# ---------------------------------------------------------------------------
class DataAgent(RoleAgent):
    def handle(self, request):
        data = validate_and_clean(collect_data(request))
        att = data["attendance"]
        estimated = sorted(att[att["estimated"] == True]["event_id"].unique().tolist())
        self.send("Coordinator", "inform",
                  f"Loaded {len(data['events'])} events, {len(data['registrations'])} registrations, "
                  f"{len(data['attendance'])} attendance rows and {len(data['feedback'])} feedback responses. "
                  f"{len(data['data_quality_notes'])} data-quality issue(s); attendance estimated for "
                  f"{', '.join(estimated) or 'no events'}.",
                  {"issues": data["data_quality_notes"], "estimated_events": estimated})
        return data, estimated


class AnalystAgent(RoleAgent):
    def propose(self, data, to="AuditorAgent"):
        ranked = rank_clubs(calculate_metrics(data))
        self.send(to, "propose", f"Proposed ranking: {_ranking_text(ranked)}.", {"ranking": _ranking_list(ranked)})
        return ranked

    def rerank_without(self, data, excluded_events, to="AuditorAgent"):
        """Sensitivity check: re-rank with the given events removed entirely."""
        trimmed = {**data}
        for key in ("events", "registrations", "attendance", "feedback"):
            trimmed[key] = data[key][~data[key].event_id.isin(excluded_events)]
        alt = rank_clubs(calculate_metrics(trimmed))
        self.send(to, "inform", f"Re-ranked without {', '.join(excluded_events)}: {_ranking_text(alt)}.",
                  {"excluded": excluded_events, "ranking": _ranking_list(alt)})
        return alt, trimmed


class AuditorAgent(RoleAgent):
    SYSTEM = ("You are AuditorAgent, a critical reviewer in a multi-agent analytics team that ranks college "
              "technical clubs. You never compute scores yourself: you inspect the Analyst's proposal and the "
              "data-quality notes, ask for sensitivity checks, and decide whether the ranking is reliable enough "
              "to publish. Be concise, specific and cite numbers. Respond only with the JSON requested.")

    def plan_checks(self, ranked, notes, candidate_events):
        """Decide which events to remove in a sensitivity check."""
        if llm.available():
            try:
                out = llm.generate_json(
                    f"Proposed ranking (JSON): {_ranking_list(ranked)}\nData-quality notes: {notes}\n"
                    f"Events whose data is estimated or incomplete: {candidate_events}\n\n"
                    "Which of those events should the Analyst remove in a sensitivity re-rank to test whether the "
                    "ranking depends on weak data? Return JSON: {\"events\": [event ids, subset of the list above], "
                    "\"request\": \"one or two sentences addressed to AnalystAgent explaining what you want and why\"}",
                    system=self.SYSTEM)
                events = [e for e in out.get("events", []) if e in candidate_events]
                return events, out.get("request", "").strip(), "gemini"
            except Exception as e:
                self.send("Coordinator", "failure", f"Gemini unavailable ({type(e).__name__}); using audit rules.")
        risky = ranked[ranked.has_estimated_attendance].club_name.tolist()
        events = [e for e in candidate_events if e in self.estimated] or candidate_events
        return events, (f"{', '.join(risky) or 'Some clubs'} rely on estimated attendance. Please re-rank without "
                        f"{', '.join(events)} so we know if those positions hold."), "rules"

    def review(self, ranked, alt, excluded, notes, revision=False):
        """Return (accept: bool, findings: list[str], message: str)."""
        base_pos = dict(zip(ranked.club_name, ranked["rank"]))
        alt_pos = dict(zip(alt.club_name, alt["rank"]))
        moves = {c: int(alt_pos[c] - base_pos[c]) for c in base_pos if c in alt_pos}
        if llm.available():
            try:
                out = llm.generate_json(
                    f"{'REVISED p' if revision else 'P'}roposed ranking: {_ranking_list(ranked)}\n"
                    f"Sensitivity re-rank without {excluded}: {_ranking_list(alt)}\n"
                    f"Rank change per club when weak data is removed (positive = drops): {moves}\n"
                    f"Data-quality notes: {notes}\n\n"
                    "Decide whether to ACCEPT the proposed ranking for publication (possibly with caveats) or "
                    "REJECT it because a top-3 position would change without the weak data. "
                    + ("This is already a revised proposal; reject only if it is clearly still unreliable. "
                       if revision else "") +
                    "Return JSON: {\"verdict\": \"accept\" or \"reject\", \"findings\": [short finding per club "
                    "that needs a caveat, each citing numbers], \"message\": \"one or two sentences to "
                    "AnalystAgent stating your verdict and the main reason\"}", system=self.SYSTEM)
                return out.get("verdict") == "accept", [str(f) for f in out.get("findings", [])], \
                    out.get("message", "").strip()
            except Exception as e:
                self.send("Coordinator", "failure", f"Gemini unavailable ({type(e).__name__}); using audit rules.")
        findings = []
        for club, moved in moves.items():
            if base_pos[club] <= 3 or moved:
                if ranked[ranked.club_name == club].has_estimated_attendance.iloc[0] or moved:
                    findings.append(f"{club}: rank #{base_pos[club]} " + (
                        "holds without the weak data (robust)." if moved == 0 else
                        f"moves to #{alt_pos[club]} without the weak data (sensitive)."))
        for r in ranked[ranked.feedback_coverage < 0.6].itertuples():
            findings.append(f"{r.club_name}: feedback collected for only {r.feedback_coverage * 100:.0f}% of events.")
        top3_changed = any(moves[c] for c in base_pos if base_pos[c] <= 3)
        accept = revision or not top3_changed
        return accept, findings, ("Ranking accepted with caveats." if accept else
                                  "Rejected: a top-3 position depends on estimated data.")


class WriterAgent(RoleAgent):
    def handle(self, request, ranked, notes, findings):
        explanations, summary = {}, ""
        if llm.available():
            try:  # one call for both the per-club paragraphs and the executive summary
                out = llm.generate_json(
                    f"Ranked clubs (JSON): {_ranking_list(ranked)}\nAuditor findings: {findings}\n"
                    f"Data-quality notes: {notes}\n\n"
                    "Return JSON: {\"summary\": \"3-4 sentence executive summary for the faculty coordinator: who "
                    "performed best and why, and which results need caution\", \"clubs\": {\"<exact club name>\": "
                    "\"2-3 factual sentences on why it landed at its rank, citing its numbers and any auditor "
                    "caveat\"}}. Plain text inside the strings, no markdown.",
                    system="You are WriterAgent, a concise technical writer.")
                clubs = out.get("clubs", {}) if isinstance(out.get("clubs"), dict) else {}
                explanations = {c: str(clubs[c]) for c in ranked.club_name if c in clubs}
                summary = str(out.get("summary", "")).strip()
            except Exception:
                explanations, summary = {}, ""
        if len(explanations) < len(ranked):
            explanations = explain_rankings(ranked)  # base tool (rule-based fallback)
        report = generate_report(request, ranked, explanations, notes)
        if summary:
            head, rest = report.split("\n## Ranking Summary", 1)
            report = f"{head}\n## Executive Summary\n\n{summary}\n\n## Ranking Summary{rest}"
        report += "\n\n## Audit Findings\n\n" + (
            "\n".join(f"- {f}" for f in findings) if findings else "- No audit concerns raised.")
        report += ("\n\n_Produced collaboratively by DataAgent, AnalystAgent, AuditorAgent and WriterAgent._")
        self.send("Coordinator", "inform",
                  f"Draft report ready ({len(report.splitlines())} lines" +
                  (", with a Gemini-written executive summary)." if summary else ")."),
                  {"report_md": report})
        return report


# ---------------------------------------------------------------------------
# Coordinator — orchestrates the council and owns the human approval gate
# ---------------------------------------------------------------------------
PENDING = OrderedDict()  # run_id -> {"report_md", "bus"}
_LOCK = threading.Lock()


def run_council(query: str = "Analyze all technical events conducted last semester and tell me which "
                             "clubs performed the best.", on_message=None) -> dict:
    bus = MessageBus(on_message)
    coordinator = RoleAgent("Coordinator", "Manager", "Decompose the request and deliver an approved report",
                            ["dispatch", "approval_gate"], bus)
    data_agent = DataAgent("DataAgent", "Data Engineer", "Collect and validate all event data",
                           ["collect_data", "validate_and_clean"], bus)
    analyst = AnalystAgent("AnalystAgent", "Data Analyst", "Score and rank every club fairly",
                           ["calculate_metrics", "rank_clubs"], bus)
    auditor = AuditorAgent("AuditorAgent", "Critic / Reviewer", "Challenge rankings that rest on weak data",
                           [], bus, uses_llm=True)
    writer = WriterAgent("WriterAgent", "Technical Writer", "Explain the results in a clear report",
                         ["explain_rankings", "generate_report"], bus, uses_llm=True)
    agents = [a.profile() for a in bus.agents.values()] + [
        {"name": HUMAN, "role": "Human-in-the-loop", "goal": "Approve or reject publication", "tools": [],
         "brain": "human"}]

    request = understand_request(query)
    coordinator.send("DataAgent", "request", f"Collect and validate data for semester {request['semester']}.")
    data, estimated = data_agent.handle(request)

    coordinator.send("AnalystAgent", "request", "Compute per-club metrics and propose a ranking to AuditorAgent.")
    ranked = analyst.propose(data)

    notes = data["data_quality_notes"]
    no_feedback = [n.split()[1] for n in notes if n.startswith("[NO_FEEDBACK]")]
    candidates = estimated + [e for e in no_feedback if e not in estimated]
    findings, verdict_sent = [], False
    auditor.estimated = estimated
    if candidates:
        events, req_text, _ = auditor.plan_checks(ranked, notes, candidates)
        if events:
            auditor.send("AnalystAgent", "request", req_text or f"Please re-rank without {', '.join(events)}.",
                         {"events": events})
            alt, trimmed = analyst.rerank_without(data, events)
            accept, findings, verdict_msg = auditor.review(ranked, alt, events, notes)
            if not accept:
                auditor.send("AnalystAgent", "reject-proposal", verdict_msg, {"findings": findings})
                analyst.send("AuditorAgent", "propose",
                             f"Revised proposal: use the conservative ranking without {', '.join(events)}: "
                             f"{_ranking_text(alt)}.", {"ranking": _ranking_list(alt)})
                ranked, data = alt, trimmed
                accept, findings2, verdict_msg = auditor.review(ranked, alt, events, notes, revision=True)
                findings = findings + [f for f in findings2 if f not in findings]
            auditor.send("AnalystAgent", "accept-proposal" if accept else "reject-proposal", verdict_msg,
                         {"findings": findings})
            verdict_sent = True
    if not verdict_sent:
        auditor.send("AnalystAgent", "accept-proposal", "Ranking accepted; no data concerns found.")
    auditor.send("WriterAgent", "inform", "Findings to disclose in the report: " + (" ".join(findings) or "none."),
                 {"findings": findings})

    coordinator.send("WriterAgent", "request", "Write the final report including the audit findings.")
    report = writer.handle(request, ranked, notes, findings)

    run_id = uuid.uuid4().hex[:8]
    coordinator.send(HUMAN, "query-if", "The report is ready. Do you approve publishing it to the club notice board?",
                     {"run_id": run_id})
    with _LOCK:
        PENDING[run_id] = {"report_md": report, "bus": bus}
        while len(PENDING) > MAX_PENDING:
            PENDING.popitem(last=False)

    return {"run_id": run_id, "status": "pending_approval", "agents": agents,
            "messages": [asdict(m) for m in bus.log], "ranking": _ranking_list(ranked), "findings": findings,
            "report_md": report}


def decide(run_id: str, approve: bool, comment: str = "") -> dict:
    with _LOCK:
        run = PENDING.pop(run_id, None)
    if run is None:
        return {"run_id": run_id, "status": "expired", "messages": []}
    bus = run["bus"]
    bus.on_message = None
    start = len(bus.log)
    if approve:
        bus.send(Message(HUMAN, "Coordinator", "agree", comment or "Approved. Publish it."))
        report = run["report_md"] + f"\n\n_Approved for publication by the {HUMAN} on " \
                                    f"{datetime.now():%Y-%m-%d %H:%M}._"
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "approved_report.md"), "w", encoding="utf-8") as f:
            f.write(report)
        bus.send(Message("Coordinator", HUMAN, "inform", "Published. Saved to output/approved_report.md."))
        status = "published"
    else:
        bus.send(Message(HUMAN, "Coordinator", "reject-proposal", comment or "Rejected. Do not publish."))
        bus.send(Message("Coordinator", HUMAN, "inform", "Publication blocked. Nothing was written."))
        status = "rejected"
    return {"run_id": run_id, "status": status, "messages": [asdict(m) for m in bus.log[start:]]}


def stream_council():
    """Yield NDJSON-ready events while the council runs, so the UI shows messages live."""
    import queue
    q = queue.Queue()
    done = object()

    def worker():
        try:
            result = run_council(on_message=lambda m: q.put({"type": "message", "message": asdict(m)}))
            q.put({"type": "result", **{k: v for k, v in result.items() if k != "messages"}})
        except Exception as e:
            q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        q.put(done)

    threading.Thread(target=worker, daemon=True).start()
    while (item := q.get()) is not done:
        yield item


def _print(m):
    print(f"[{m.time}] {m.sender:>13} -> {m.receiver:<19} ({m.performative}) {m.content}")


if __name__ == "__main__":
    print(f"Agent Council — reviewer brain: {'Gemini ' + llm.MODEL if llm.available() else 'rules (offline)'}\n")
    result = run_council(on_message=_print)
    answer = input("\nApprove publishing? (y/n): ").strip().lower()
    for m in decide(result["run_id"], answer == "y")["messages"]:
        print(f"[{m['time']}] {m['sender']:>13} -> {m['receiver']:<19} ({m['performative']}) {m['content']}")
