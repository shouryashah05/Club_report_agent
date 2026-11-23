"""
"Ask the Agent" — a conversational analyst built on Gemini function calling.

  1. TOOLS: the agent gets a registry of analytics tools (name, description,
     JSON parameter schema). Gemini decides which to call and with what
     arguments; we execute the call and hand the observation back, looping
     until the model writes its answer. Bad arguments come back as an error
     observation so the model can correct itself.
  2. SHORT-TERM MEMORY: the last few conversation turns are replayed to the
     model on every request, so follow-ups like "what about its events?" work.
  3. LONG-TERM MEMORY: facts the user asks it to remember are embedded
     (Gemini embeddings) and stored on disk; before every answer the most
     similar memories are retrieved and given to the model as context.

Without a GEMINI_API_KEY (or if the API call fails) the agent falls back to
a keyword planner and bag-of-words retrieval so it still works offline.
"""
import json
import math
import os
import re
from collections import Counter, deque
from datetime import datetime

import llm
from agent import METRIC_WEIGHTS, calculate_metrics, collect_data, rank_clubs, understand_request, validate_and_clean

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory_store")
LONG_TERM_FILE = os.path.join(MEMORY_DIR, "long_term_memory.json")
SHORT_TERM_WINDOW = 6   # conversation turns replayed to the model
MAX_TOOL_STEPS = 4      # cap on model requests per question (saves quota)

CLUB_ALIASES = {
    "Robotics Club": ["robotics", "robot"],
    "Coding Club": ["coding", "code club", "programming"],
    "AI/ML Club": ["ai/ml", "aiml", "ai ml", "ai-ml", "machine learning", "ml club"],
    "Cybersecurity Club": ["cybersecurity", "cyber", "security"],
    "IoT Club": ["iot", "internet of things"],
}
PRONOUNS = re.compile(r"\b(it|its|they|their|them|that club|this club|same club)\b")
STOPWORDS = set("""a an the is are was were be been of in on at to for and or but with about
what which who whom why how when where do does did i me my we our you your it its they their
them this that these those club clubs remember note please tell show give can could would
should has have had there here than then so as by from""".split())


# ---------------------------------------------------------------------------
# Analytics snapshot (re-uses the base pipeline unchanged)
# ---------------------------------------------------------------------------
_SNAPSHOT = None


def snapshot() -> dict:
    global _SNAPSHOT
    if _SNAPSHOT is None:
        request = understand_request("Analyze all technical events conducted last semester")
        data = validate_and_clean(collect_data(request))
        _SNAPSHOT = {"request": request, "data": data, "ranked": rank_clubs(calculate_metrics(data))}
    return _SNAPSHOT


def find_clubs(text: str) -> list:
    t = (text or "").lower()
    hits = []
    for club, aliases in CLUB_ALIASES.items():
        positions = [t.find(a) for a in aliases + [club.lower()] if a in t]
        if positions:
            hits.append((min(positions), club))
    return [c for _, c in sorted(hits)]


def resolve_club(name: str) -> str:
    matches = find_clubs(name)
    if not matches:
        raise ValueError(f"Unknown club '{name}'. Valid clubs: {', '.join(CLUB_ALIASES)}.")
    return matches[0]


# ---------------------------------------------------------------------------
# LONG-TERM MEMORY — a small vector store persisted to disk
# ---------------------------------------------------------------------------
def bow(text: str) -> Counter:
    tokens = re.findall(r"[a-z0-9/]+", text.lower())
    return Counter(t for t in tokens if t not in STOPWORDS and len(t) > 1)


def cosine(a, b) -> float:
    if isinstance(a, Counter):
        dot = sum(a[t] * b[t] for t in set(a) & set(b))
        na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b))
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class LongTermMemory:
    GEMINI_THRESHOLD = 0.64   # cosine similarity cut-offs per embedding type
    BOW_THRESHOLD = 0.15

    def __init__(self, path: str = LONG_TERM_FILE):
        self.path = path
        self.items = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2, ensure_ascii=False)

    def _embed(self, text):
        try:
            return llm.embed(text) if llm.available() else None
        except Exception:
            return None

    def add(self, text: str, club: str = None) -> dict:
        for existing in self.items:  # don't store the same fact twice
            if existing["text"].strip().lower() == text.strip().lower():
                return self.public(existing)
        item = {"id": max((i["id"] for i in self.items), default=0) + 1, "text": text.strip(), "club": club,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "vector": self._embed(f"{club or ''} {text}")}
        self.items.append(item)
        self._save()
        return self.public(item)

    def search(self, query: str, k: int = 3) -> list:
        if not self.items:
            return []
        qvec = self._embed(query)
        scored = []
        for item in self.items:
            if qvec is not None and item.get("vector") is None:   # saved while offline: embed now
                item["vector"] = self._embed(f"{item['club'] or ''} {item['text']}")
                self._save()
            if qvec is not None and item.get("vector"):
                score, threshold, method = cosine(qvec, item["vector"]), self.GEMINI_THRESHOLD, "gemini-embedding"
            else:
                score = cosine(bow(query), bow(item["text"] + " " + (item["club"] or "")))
                threshold, method = self.BOW_THRESHOLD, "bag-of-words"
            if score >= threshold:
                scored.append({**self.public(item), "score": round(score, 3), "method": method})
        return sorted(scored, key=lambda x: x["score"], reverse=True)[:k]

    def clear(self):
        self.items = []
        self._save()

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k != "vector"}

    def listing(self):
        return [self.public(i) for i in self.items]


LTM = LongTermMemory()


# ---------------------------------------------------------------------------
# TOOLS — what the agent is allowed to call
# ---------------------------------------------------------------------------
def tool_get_rankings(top_n: int = 5) -> dict:
    ranked = snapshot()["ranked"].head(int(top_n))
    return {"semester": snapshot()["request"]["semester"],
            "rankings": [{"rank": int(r.rank), "club": r.club_name, "score": round(r.performance_score, 3),
                          "score_confidence": r.score_confidence} for r in ranked.itertuples()]}


def tool_get_club_details(club_name: str) -> dict:
    club = resolve_club(club_name)
    ranked = snapshot()["ranked"]
    r = ranked[ranked.club_name == club].iloc[0].to_dict()
    return {"club": club, "rank": int(r["rank"]), "of_clubs": len(ranked), "score": round(r["performance_score"], 3),
            "events": int(r["num_events"]), "total_registrations": int(r["total_registrations"]),
            "total_attendance": int(r["total_attendance"]), "attendance_rate": r["attendance_rate"],
            "avg_feedback_out_of_5": r["avg_feedback"], "feedback_coverage": r["feedback_coverage"],
            "has_estimated_attendance": bool(r["has_estimated_attendance"])}


def tool_compare_clubs(club_a: str, club_b: str) -> dict:
    return {"a": tool_get_club_details(club_a), "b": tool_get_club_details(club_b)}


def tool_get_club_events(club_name: str) -> dict:
    club = resolve_club(club_name)
    data = snapshot()["data"]
    out = []
    for e in data["events"][data["events"].club_name == club].itertuples():
        reg = int((data["registrations"].event_id == e.event_id).sum())
        att_rows = data["attendance"][data["attendance"].event_id == e.event_id]
        estimated = bool(att_rows["estimated"].any()) if len(att_rows) else False
        att = int(att_rows["attended"].sum()) if estimated else len(att_rows)
        fb = data["feedback"][data["feedback"].event_id == e.event_id]["rating"]
        out.append({"event_id": e.event_id, "event": e.event_name, "date": e.date, "registrations": reg,
                    "attendance": att, "attendance_estimated": estimated, "feedback_responses": int(len(fb)),
                    "avg_feedback": round(float(fb.mean()), 2) if len(fb) else None})
    return {"club": club, "events": out}


def tool_get_data_quality_issues(club_name: str = None) -> dict:
    notes = snapshot()["data"]["data_quality_notes"]
    club = resolve_club(club_name) if club_name else None
    return {"club": club, "issues": [n for n in notes if not club or club in n]}


def tool_get_scoring_method() -> dict:
    return {"weights": METRIC_WEIGHTS,
            "method": "Each metric is min-max normalised across clubs, then combined with the weights. If a club "
                      "lacks a metric, that weight is dropped and the rest re-normalised; score_confidence is the "
                      "share of weight that was available.",
            "missing_data_rules": ["No attendance log: estimated as 85% of registrations and flagged.",
                                   "No feedback: event excluded from the feedback average, not scored as 0."]}


def tool_save_memory(text: str, club_name: str = None) -> dict:
    club = None
    if club_name:
        try:
            club = resolve_club(club_name)
        except ValueError:
            club = None
    return {"saved": LTM.add(text, club)}


def tool_search_memory(query: str) -> dict:
    return {"matches": LTM.search(query)}


def tool_list_memories() -> dict:
    return {"memories": LTM.listing()}


S = "STRING"
TOOLS = {
    "get_rankings": (tool_get_rankings, "Ranked leaderboard of all clubs for the semester, best first.",
                     {"top_n": ("INTEGER", "How many clubs to return (default 5)")}, []),
    "get_club_details": (tool_get_club_details, "Rank, score and all metrics for one club.",
                         {"club_name": (S, "Club name, e.g. 'IoT Club'")}, ["club_name"]),
    "compare_clubs": (tool_compare_clubs, "Side-by-side metrics for two clubs.",
                      {"club_a": (S, "First club"), "club_b": (S, "Second club")}, ["club_a", "club_b"]),
    "get_club_events": (tool_get_club_events, "Event-by-event registrations, attendance and feedback for one club.",
                        {"club_name": (S, "Club name")}, ["club_name"]),
    "get_data_quality_issues": (tool_get_data_quality_issues,
                                "Missing or estimated data flags, for one club or the whole dataset.",
                                {"club_name": (S, "Optional club name; omit for all clubs")}, []),
    "get_scoring_method": (tool_get_scoring_method, "How the composite performance score is calculated.", {}, []),
    "save_memory": (tool_save_memory, "Store a fact in long-term memory so it is available in future sessions. "
                                      "Use when the user asks you to remember or note something.",
                    {"text": (S, "The fact to remember, written as a standalone sentence"),
                     "club_name": (S, "Optional club the fact is about")}, ["text"]),
    "search_memory": (tool_search_memory, "Similarity search over long-term memory.",
                      {"query": (S, "What to look for")}, ["query"]),
    "list_memories": (tool_list_memories, "List everything stored in long-term memory.", {}, []),
}


def tool_catalog() -> dict:
    return {name: {"description": desc, "parameters": list(params)} for name, (_, desc, params, _) in TOOLS.items()}


def run_tool(name: str, args: dict) -> dict:
    """Execute a tool call; errors become observations the model can react to."""
    if name not in TOOLS:
        return {"error": f"Unknown tool '{name}'."}
    try:
        return llm.to_jsonable(TOOLS[name][0](**args))
    except (ValueError, TypeError) as e:
        return {"error": str(e)}


def _declarations():
    from google.genai import types
    decls = []
    for name, (_, desc, params, required) in TOOLS.items():
        schema = types.Schema(type="OBJECT", properties={
            p: types.Schema(type=t, description=d) for p, (t, d) in params.items()}, required=required) \
            if params else None
        decls.append(types.FunctionDeclaration(name=name, description=desc, parameters=schema))
    return [types.Tool(function_declarations=decls)]


# ---------------------------------------------------------------------------
# SHORT-TERM MEMORY — per-session window of recent turns
# ---------------------------------------------------------------------------
class ShortTermMemory:
    def __init__(self):
        self.turns = deque(maxlen=SHORT_TERM_WINDOW)
        self.focus_club = None  # used by the offline planner to resolve "it"

    def to_dict(self):
        return {"window_size": SHORT_TERM_WINDOW,
                "turns": [{"user": t["user"], "tools": t["tools"]} for t in self.turns]}


SESSIONS = {}


def get_session(session_id: str) -> ShortTermMemory:
    return SESSIONS.setdefault(session_id, ShortTermMemory())


def reset_session(session_id: str = "default"):
    SESSIONS.pop(session_id, None)


# ---------------------------------------------------------------------------
# GEMINI AGENT LOOP — recall -> reason -> call tools -> observe -> answer
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the Club Analytics assistant for a college's technical clubs.
You answer questions about club performance in semester {semester} using ONLY the tools provided;
never invent numbers. Use as few tool calls as possible (request several in one step when you can), then answer in 2-5 plain-text sentences (no markdown), citing figures.
Clubs: {clubs}.
If the user asks you to remember or note something, call save_memory.
If a tool returns an error, fix the arguments and try again.
Mention data-quality caveats (estimated attendance, missing feedback) when they affect the answer.
Use any relevant long-term memories below as extra context and say when you are drawing on them.

Relevant long-term memories (retrieved by similarity search):
{memories}"""


def _ask_gemini(question: str, stm: ShortTermMemory, recalled: list, trace: list) -> tuple:
    from google.genai import types
    memories = "\n".join(f"- (#{m['id']}) {m['text']}" for m in recalled) or "- none"
    system = SYSTEM_PROMPT.format(semester=snapshot()["request"]["semester"], clubs=", ".join(CLUB_ALIASES),
                                  memories=memories)
    contents = []
    for t in stm.turns:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=t["user"])]))
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=t["agent"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
    config = types.GenerateContentConfig(
        system_instruction=system, tools=_declarations(), temperature=0.3,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

    tools_used = []
    for _ in range(MAX_TOOL_STEPS):
        resp = llm.generate(contents, config)
        calls = resp.function_calls or []
        if not calls:
            return (resp.text or "").strip(), tools_used
        contents.append(resp.candidates[0].content)
        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            trace.append({"step": "Function call", "detail": f"Gemini chose `{fc.name}`.",
                          "data": {"tool": fc.name, "arguments": args}})
            obs = run_tool(fc.name, args)
            trace.append({"step": "Observation", "detail": "Tool returned an error; the model will adapt."
                          if "error" in obs else "Tool result sent back to the model.", "data": obs})
            tools_used.append(fc.name)
            mentioned = find_clubs(str(args.get("club_name") or args.get("club_a") or ""))
            if mentioned:  # remembered so the offline fallback can still resolve "it"
                stm.focus_club = mentioned[0]
            responses.append(types.Part.from_function_response(name=fc.name, response={"result": obs}))
        contents.append(types.Content(role="user", parts=responses))
    return "I could not finish within the tool-call limit. Please rephrase the question.", tools_used


# ---------------------------------------------------------------------------
# OFFLINE FALLBACK — keyword planner + plain-text summary of the observation
# ---------------------------------------------------------------------------
def _offline_plan(question: str, stm: ShortTermMemory):
    q = question.lower().strip()
    clubs = find_clubs(q)
    if not clubs and stm.focus_club and PRONOUNS.search(q):
        clubs = [stm.focus_club]
    club = clubs[0] if clubs else None
    if q.startswith(("remember", "note that", "note:", "keep in mind")):
        text = re.sub(r"^(remember( that)?|note that|note:|keep in mind( that)?)\s*", "", question.strip(), flags=re.I)
        return "save_memory", {"text": text, "club_name": club}
    if "remember" in q or "memory" in q:
        return "list_memories", {}
    if len(clubs) >= 2:
        return "compare_clubs", {"club_a": clubs[0], "club_b": clubs[1]}
    if any(w in q for w in ["quality", "missing", "issue", "gap", "estimated", "flag"]):
        return "get_data_quality_issues", ({"club_name": club} if club else {})
    if any(w in q for w in ["score", "weight", "calculat", "method"]) and not club:
        return "get_scoring_method", {}
    if club and "event" in q:
        return "get_club_events", {"club_name": club}
    if club:
        return "get_club_details", {"club_name": club}
    if any(w in q for w in ["top", "best", "rank", "leader", "worst", "perform"]):
        return "get_rankings", {}
    return None, {}


def _offline_answer(tool, obs, recalled):
    if tool is None:
        ans = "Offline mode understands questions about rankings, a club, two clubs, events, data quality or memory."
    elif "error" in obs:
        ans = obs["error"]
    elif tool == "get_rankings":
        ans = "Leaderboard: " + "; ".join(f"#{r['rank']} {r['club']} ({r['score']:.2f})" for r in obs["rankings"]) + "."
    elif tool == "get_club_details":
        d = obs
        ans = (f"{d['club']} is #{d['rank']} of {d['of_clubs']} (score {d['score']:.2f}): {d['events']} events, "
               f"{d['attendance_rate'] * 100:.0f}% attendance, {d['avg_feedback_out_of_5']}/5 average feedback.")
    elif tool == "compare_clubs":
        a, b = obs["a"], obs["b"]
        ans = (f"{a['club']} is #{a['rank']} ({a['score']:.2f}) and {b['club']} is #{b['rank']} ({b['score']:.2f}); "
               f"attendance {a['attendance_rate'] * 100:.0f}% vs {b['attendance_rate'] * 100:.0f}%, feedback "
               f"{a['avg_feedback_out_of_5']} vs {b['avg_feedback_out_of_5']}.")
    elif tool == "get_club_events":
        ans = f"{obs['club']}: " + "; ".join(
            f"{e['event']} {e['attendance']}/{e['registrations']} attended"
            f"{' (estimated)' if e['attendance_estimated'] else ''}" for e in obs["events"]) + "."
    elif tool == "get_data_quality_issues":
        ans = " ".join(obs["issues"]) or "No data-quality issues found."
    elif tool == "get_scoring_method":
        ans = obs["method"]
    elif tool == "save_memory":
        ans = f"Saved to long-term memory (#{obs['saved']['id']})."
    else:
        ans = "Stored memories: " + (" | ".join(m["text"] for m in obs["memories"]) or "none")
    if recalled and tool not in ("save_memory", "list_memories"):
        ans += " From memory: " + " ".join(f"\"{m['text']}\"" for m in recalled)
    return ans


def _ask_offline(question, stm, recalled, trace):
    tool, args = _offline_plan(question, stm)
    obs = {}
    if tool:
        trace.append({"step": "Function call", "detail": f"Offline planner chose `{tool}`.",
                      "data": {"tool": tool, "arguments": {k: v for k, v in args.items() if v is not None}}})
        obs = run_tool(tool, {k: v for k, v in args.items() if v is not None})
        trace.append({"step": "Observation", "detail": "Tool returned:", "data": obs})
        club = args.get("club_name") or args.get("club_a")
        if club:
            stm.focus_club = club
    return _offline_answer(tool, obs, recalled), [tool] if tool else []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def ask(question: str, session_id: str = "default") -> dict:
    stm = get_session(session_id)
    trace = []

    recalled = LTM.search(question)
    trace.append({"step": "Long-term memory recall",
                  "detail": (f"{len(recalled)} memory(ies) retrieved by similarity and added to the context."
                             if recalled else "No stored memory is similar enough to this question."),
                  "data": recalled})
    if stm.turns:
        trace.append({"step": "Short-term memory",
                      "detail": f"Replaying the last {len(stm.turns)} turn(s) so follow-up questions have context."})

    mode = "gemini" if llm.available() else "offline"
    if mode == "gemini":
        try:
            answer, tools_used = _ask_gemini(question, stm, recalled, trace)
        except Exception as e:
            trace.append({"step": "Fallback", "detail": f"Gemini call failed ({type(e).__name__}: {str(e)[:160]}). "
                                                        "Using the offline planner."})
            mode = "offline"
    if mode == "offline":
        answer, tools_used = _ask_offline(question, stm, recalled, trace)

    trace.append({"step": "Answer", "detail": answer})
    stm.turns.append({"user": question, "agent": answer, "tools": tools_used})
    return {"answer": answer, "mode": mode, "model": llm.last_model_used if mode == "gemini" else "offline planner",
            "tools_used": tools_used, "trace": trace, "short_term_memory": stm.to_dict(),
            "long_term_memory": LTM.listing(), "recalled_ids": [m["id"] for m in recalled], "tools": tool_catalog()}


if __name__ == "__main__":
    import sys
    print(f"Ask the Agent — {'Gemini ' + llm.MODEL if llm.available() else 'offline mode'}. Empty line to quit.")
    while True:
        try:
            q = input("\nYOU:   ").strip()
        except EOFError:
            break
        if not q:
            break
        res = ask(q, "cli")
        print(f"       [tools: {', '.join(res['tools_used']) or 'none'}]")
        print(f"AGENT: {res['answer']}")
    sys.exit(0)
