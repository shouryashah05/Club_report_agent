from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import io
import traceback
import os
from agent import SmartEventAnalyticsAgent

app = FastAPI(title="Club Events Analytics")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/static/landing.html")

@app.get("/landing")
async def landing_page():
    return RedirectResponse(url="/static/landing.html")

@app.get("/dashboard")
async def dashboard_page():
    return RedirectResponse(url="/static/index.html")

DATA_DIR = os.path.join(BASE_DIR, "data")
EXPECTED_FILES = ["events.csv", "registrations.csv", "attendance.csv", "feedback.csv"]


def _run_agent(raw_data: dict) -> dict:
    """Shared helper: run the analytics agent on four DataFrames."""
    agent = SmartEventAnalyticsAgent()
    return agent.run_with_data(
        query="Analyze all technical events conducted last semester and tell me which clubs performed the best.",
        raw_data=raw_data,
    )


@app.post("/api/analyze")
async def analyze_data(
    events: UploadFile = File(...),
    registrations: UploadFile = File(...),
    attendance: UploadFile = File(...),
    feedback: UploadFile = File(...)
):
    try:
        # Read uploaded files into pandas DataFrames
        events_df = pd.read_csv(io.StringIO((await events.read()).decode('utf-8')))
        registrations_df = pd.read_csv(io.StringIO((await registrations.read()).decode('utf-8')))
        attendance_df = pd.read_csv(io.StringIO((await attendance.read()).decode('utf-8')))
        feedback_df = pd.read_csv(io.StringIO((await feedback.read()).decode('utf-8')))

        raw_data = {
            "events": events_df,
            "registrations": registrations_df,
            "attendance": attendance_df,
            "feedback": feedback_df
        }

        return JSONResponse(content=_run_agent(raw_data))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/analyze-sample")
async def analyze_sample():
    """Run the analytics pipeline on the built-in data/ CSVs."""
    try:
        missing = [f for f in EXPECTED_FILES if not os.path.isfile(os.path.join(DATA_DIR, f))]
        if missing:
            raise HTTPException(status_code=404, detail=f"Missing sample files: {', '.join(missing)}")

        raw_data = {
            name.replace(".csv", ""): pd.read_csv(os.path.join(DATA_DIR, name))
            for name in EXPECTED_FILES
        }
        return JSONResponse(content=_run_agent(raw_data))
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Agent features: conversational assistant + multi-agent council
# (additive — /api/analyze above is unchanged)
# ---------------------------------------------------------------------------
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
import llm
import assistant_agent
import agent_council


class AskBody(BaseModel):
    question: str
    session_id: str = "default"


class DecisionBody(BaseModel):
    run_id: str
    approve: bool
    comment: str = ""


@app.get("/agents")
@app.get("/demo")  # old address, kept so bookmarks still work
async def agents_page():
    return RedirectResponse(url="/static/agents.html")


@app.get("/api/status")
async def status():
    return llm.status()


@app.post("/api/assistant/ask")
def assistant_ask(body: AskBody):
    return JSONResponse(content=assistant_agent.ask(body.question, body.session_id))


@app.post("/api/assistant/reset")
async def assistant_reset(body: AskBody):
    assistant_agent.reset_session(body.session_id)
    return {"ok": True}


@app.get("/api/assistant/memory")
async def assistant_memory():
    return {"memories": assistant_agent.LTM.listing(), "tools": assistant_agent.tool_catalog()}


@app.post("/api/assistant/memory/clear")
async def assistant_clear_memory():
    assistant_agent.LTM.clear()
    return {"ok": True}


@app.post("/api/council/run")
def council_run():
    lines = (json.dumps(event) + "\n" for event in agent_council.stream_council())
    return StreamingResponse(lines, media_type="application/x-ndjson")


@app.post("/api/council/decision")
def council_decision(body: DecisionBody):
    return JSONResponse(content=agent_council.decide(body.run_id, body.approve, body.comment))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
