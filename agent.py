

import os
import re
import sys
from datetime import datetime

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_DIR = os.path.join("/tmp", "output") if os.environ.get("VERCEL") else os.path.join(os.path.dirname(__file__), "output")

# Weights used when ALL metrics are available for a club.
# Re-normalized on the fly for any club missing a metric (see calculate_metrics).
METRIC_WEIGHTS = {"attendance_rate": 0.45, "feedback_score": 0.40, "event_volume": 0.15}


# ---------------------------------------------------------------------------
# TOOL 1 — Understand the reporting request
# ---------------------------------------------------------------------------
def understand_request(query: str) -> dict:
    """Lightweight intent + slot extraction. In production this slot would be
    an LLM call (Claude) doing NLU; regex/keyword matching is a fast,
    deterministic stand-in that's easy to demo and unit-test."""
    q = query.lower()

    if "last semester" in q or "previous semester" in q:
        semester = "Odd 2025-26"  # would resolve dynamically against an academic calendar
    else:
        semester = "Odd 2025-26"  # default / most recent completed semester

    intent = "rank_clubs" if any(w in q for w in ["best", "top", "rank", "compare", "perform"]) else "summarize"

    return {
        "raw_query": query,
        "intent": intent,
        "semester": semester,
        "scope": "technical events",
    }


# ---------------------------------------------------------------------------
# TOOL 2 — Collect event / registration / attendance / feedback data
# ---------------------------------------------------------------------------
def collect_data(request: dict) -> dict:
    """Pulls the four data sources and filters to the requested semester.
    Here they're CSVs; in production these calls hit the college's event
    portal API, an attendance/RFID system, and a feedback-form backend."""
    if "raw_data" in request:
        events = request["raw_data"]["events"]
        registrations = request["raw_data"]["registrations"]
        attendance = request["raw_data"]["attendance"]
        feedback = request["raw_data"]["feedback"]
    else:
        events = pd.read_csv(f"{DATA_DIR}/events.csv")
        registrations = pd.read_csv(f"{DATA_DIR}/registrations.csv")
        attendance = pd.read_csv(f"{DATA_DIR}/attendance.csv")
        feedback = pd.read_csv(f"{DATA_DIR}/feedback.csv")

    events = events[events["semester"] == request["semester"]]
    valid_ids = set(events["event_id"])
    registrations = registrations[registrations["event_id"].isin(valid_ids)]
    attendance = attendance[attendance["event_id"].isin(valid_ids)]
    feedback = feedback[feedback["event_id"].isin(valid_ids)]

    return {"events": events, "registrations": registrations, "attendance": attendance, "feedback": feedback}


# ---------------------------------------------------------------------------
# TOOL 3 — Validate & handle missing data
# ---------------------------------------------------------------------------
def validate_and_clean(data: dict) -> dict:
    """Flags data-quality gaps instead of silently zero-filling them:
      - Event has registrations but NO attendance rows  -> estimate attendance
        at 85% of registrations (typical show-up rate) and flag as ESTIMATED.
      - Event has attendance but NO feedback rows        -> excluded from the
        feedback average (not scored as 0) and flagged as NO_FEEDBACK.
    Every flag is logged so the final report can disclose exactly what was
    inferred vs. observed.
    """
    events, registrations, attendance, feedback = (
        data["events"], data["registrations"], data["attendance"], data["feedback"]
    )
    notes = []

    reg_counts = registrations.groupby("event_id").size()
    att_counts = attendance.groupby("event_id").size()
    fb_counts = feedback.groupby("event_id").size()

    estimated_rows = []
    for eid in events["event_id"]:
        has_reg = eid in reg_counts.index
        has_att = eid in att_counts.index
        has_fb = eid in fb_counts.index
        club = events.loc[events.event_id == eid, "club_name"].iloc[0]
        name = events.loc[events.event_id == eid, "event_name"].iloc[0]

        if has_reg and not has_att:
            estimate = round(reg_counts[eid] * 0.85)
            estimated_rows.append({"event_id": eid, "student_id": None, "attended": estimate})
            notes.append(f"[ESTIMATED] {eid} ({name}, {club}): no attendance log found — "
                         f"estimated {estimate} attendees from {reg_counts[eid]} registrations (85% assumed show-up rate).")
        if has_att and not has_fb:
            notes.append(f"[NO_FEEDBACK] {eid} ({name}, {club}): {att_counts[eid]} attendees but zero feedback "
                         f"responses — event excluded from this club's feedback average.")
        if not has_reg:
            notes.append(f"[NO_REGISTRATION_DATA] {eid} ({name}, {club}): no registration records found at all.")

    # Fold estimated attendance rows into a working attendance table (kept
    # separate from the raw file — we never overwrite source data).
    attendance_est = pd.concat([
        attendance,
        pd.DataFrame(estimated_rows).assign(estimated=True) if estimated_rows else pd.DataFrame(columns=attendance.columns),
    ], ignore_index=True) if estimated_rows else attendance.assign(estimated=False)
    if "estimated" not in attendance_est.columns:
        attendance_est["estimated"] = False
    attendance_est["estimated"] = attendance_est["estimated"].fillna(False)

    return {**data, "attendance": attendance_est, "data_quality_notes": notes}


# ---------------------------------------------------------------------------
# TOOL 4 — Calculate per-club metrics
# ---------------------------------------------------------------------------
def calculate_metrics(data: dict) -> pd.DataFrame:
    events, registrations, attendance, feedback = (
        data["events"], data["registrations"], data["attendance"], data["feedback"]
    )

    rows = []
    for club, club_events in events.groupby("club_name"):
        eids = set(club_events["event_id"])
        reg = registrations[registrations.event_id.isin(eids)]
        att = attendance[attendance.event_id.isin(eids)]
        fb = feedback[feedback.event_id.isin(eids)]

        total_reg = len(reg)
        # attendance rows may be real (student_id set) or estimated (count only)
        real_att = att[att["estimated"] == False] if "estimated" in att.columns else att
        est_att = att[att["estimated"] == True] if "estimated" in att.columns else att.iloc[0:0]
        total_att = len(real_att) + (est_att["attended"].sum() if len(est_att) else 0)

        attendance_rate = round(total_att / total_reg, 3) if total_reg else None
        events_with_feedback = fb["event_id"].nunique()
        avg_feedback = round(fb["rating"].mean(), 2) if len(fb) else None
        feedback_coverage = round(events_with_feedback / len(eids), 2) if len(eids) else 0

        rows.append({
            "club_name": club,
            "num_events": len(eids),
            "total_registrations": total_reg,
            "total_attendance": total_att,
            "attendance_rate": attendance_rate,
            "avg_feedback": avg_feedback,
            "feedback_coverage": feedback_coverage,
            "has_estimated_attendance": bool(len(est_att)),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# TOOL 5 — Compare & rank clubs
# ---------------------------------------------------------------------------
def rank_clubs(metrics: pd.DataFrame) -> pd.DataFrame:
    """Min-max normalizes each metric across clubs, then computes a weighted
    composite score. If a club is missing a metric (e.g. no feedback at all),
    that weight is dropped and the rest are re-normalized — so a club is
    never penalized to zero for a gap in the *data*, only scored on what's
    actually known about it (with that fact disclosed in the report)."""
    df = metrics.copy()

    def minmax(col):
        vals = df[col].dropna()
        if vals.empty or vals.max() == vals.min():
            return {i: 1.0 for i in vals.index}
        return {i: (v - vals.min()) / (vals.max() - vals.min()) for i, v in vals.items()}

    norm_att = minmax("attendance_rate")
    norm_fb = minmax("avg_feedback")
    norm_ev = minmax("num_events")

    scores, confidences = [], []
    for i, row in df.iterrows():
        parts, weight_sum = 0.0, 0.0
        if i in norm_att:
            parts += norm_att[i] * METRIC_WEIGHTS["attendance_rate"]; weight_sum += METRIC_WEIGHTS["attendance_rate"]
        if i in norm_fb:
            parts += norm_fb[i] * METRIC_WEIGHTS["feedback_score"]; weight_sum += METRIC_WEIGHTS["feedback_score"]
        if i in norm_ev:
            parts += norm_ev[i] * METRIC_WEIGHTS["event_volume"]; weight_sum += METRIC_WEIGHTS["event_volume"]
        score = round(parts / weight_sum, 4) if weight_sum else 0.0
        scores.append(score)
        confidences.append(round(weight_sum, 2))  # 1.0 = all 3 metrics available

    df["performance_score"] = scores
    df["score_confidence"] = confidences
    df = df.sort_values("performance_score", ascending=False).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    return df


# ---------------------------------------------------------------------------
# TOOL 6 — Explain the rankings in natural language
# ---------------------------------------------------------------------------
def explain_rankings(ranked: pd.DataFrame) -> dict:
    """Tries a Claude API call for richer explanations; falls back to a
    deterministic template if no API key / no network is available, so the
    agent degrades gracefully rather than failing the whole pipeline."""
    explanations = {}
    try:
        explanations = _explain_with_llm(ranked)
    except Exception:
        pass
    if not explanations:
        explanations = {row.club_name: _explain_with_rules(row, ranked) for row in ranked.itertuples()}
    return explanations


def _explain_with_rules(row, ranked: pd.DataFrame) -> str:
    bits = [f"Ranked #{row.rank} with a composite score of {row.performance_score:.2f}."]
    if row.attendance_rate is not None:
        bits.append(f"Attendance rate of {row.attendance_rate*100:.0f}% across {row.num_events} events.")
    if row.avg_feedback is not None:
        bits.append(f"Average feedback rating of {row.avg_feedback}/5 (coverage: {row.feedback_coverage*100:.0f}% of events).")
    else:
        bits.append("No feedback data was available for any event, so ranking relied on attendance and event volume only.")
    if row.has_estimated_attendance:
        bits.append("Note: attendance for at least one event was estimated from registrations, not directly observed.")
    if row.rank > 1:
        leader = ranked.iloc[0]
        gap = leader.performance_score - row.performance_score
        bits.append(f"Trails the top club ({leader.club_name}) by {gap:.2f} points on the composite score.")
    return " ".join(bits)


def _explain_with_llm(ranked: pd.DataFrame) -> dict:
    """Optional richer explanation via the Gemini API — only runs if
    GEMINI_API_KEY is set in the environment; otherwise raises so the
    caller falls back to the rule-based explainer."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("no API key configured")
    from google import genai
    client = genai.Client()
    table = ranked.to_dict(orient="records")
    prompt = (
        "You are an events analytics assistant. Given this ranked table of "
        "college technical clubs (JSON below), write one concise, factual "
        "paragraph per club explaining why it landed at that rank, citing its "
        "own numbers. Return a JSON object mapping club_name to paragraph. Do not return any markdown formatting, just the raw JSON object.\n\n"
        f"{table}"
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt
    )
    import json
    text = resp.text.strip()
    if text.startswith("```json"):
        text = text[7:-3].strip()
    elif text.startswith("```"):
        text = text[3:-3].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# TOOL 7 — Generate the final report
# ---------------------------------------------------------------------------
def generate_report(request: dict, ranked: pd.DataFrame, explanations: dict, notes: list) -> str:
    lines = []
    lines.append(f"# Club Performance Report — {request['semester']}")
    lines.append(f"_Generated {datetime.now():%Y-%m-%d %H:%M} in response to:_ \"{request['raw_query']}\"\n")

    lines.append("## Ranking Summary\n")
    lines.append("| Rank | Club | Events | Attendance Rate | Avg Feedback | Score | Confidence |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in ranked.itertuples():
        att = f"{row.attendance_rate*100:.0f}%" if row.attendance_rate is not None else "N/A"
        fb = f"{row.avg_feedback}/5" if row.avg_feedback is not None else "N/A"
        lines.append(f"| {row.rank} | {row.club_name} | {row.num_events} | {att} | {fb} | "
                     f"{row.performance_score:.2f} | {row.score_confidence*100:.0f}% |")

    lines.append("\n## Why Each Club Ranked Where It Did\n")
    for row in ranked.itertuples():
        lines.append(f"**#{row.rank} — {row.club_name}**  ")
        lines.append(explanations.get(row.club_name, "") + "\n")

    lines.append("## Data Quality Notes\n")
    if notes:
        for n in notes:
            lines.append(f"- {n}")
    else:
        lines.append("- No data gaps detected — all events had complete registration, attendance, and feedback records.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AGENT — planner that sequences the tools above
# ---------------------------------------------------------------------------
class SmartEventAnalyticsAgent:
    def run(self, query: str) -> str:
        request = understand_request(query)
        data = collect_data(request)
        data = validate_and_clean(data)
        metrics = calculate_metrics(data)
        ranked = rank_clubs(metrics)
        explanations = explain_rankings(ranked)
        report = generate_report(request, ranked, explanations, data["data_quality_notes"])
        return report

    def run_with_data(self, query: str, raw_data: dict) -> dict:
        request = understand_request(query)
        request["raw_data"] = raw_data
        data = collect_data(request)
        data = validate_and_clean(data)
        metrics = calculate_metrics(data)
        ranked = rank_clubs(metrics)
        explanations = explain_rankings(ranked)
        report = generate_report(request, ranked, explanations, data["data_quality_notes"])
        return {
            "metrics": metrics.to_dict(orient="records"),
            "ranked": ranked.to_dict(orient="records"),
            "explanations": explanations,
            "notes": data["data_quality_notes"],
            "report_md": report
        }


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else \
        "Analyze all technical events conducted last semester and tell me which clubs performed the best."
    agent = SmartEventAnalyticsAgent()
    report = agent.run(query)
    print(report)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(f"{OUTPUT_DIR}/report.md", "w") as f:
        f.write(report)


if __name__ == "__main__":
    main()
