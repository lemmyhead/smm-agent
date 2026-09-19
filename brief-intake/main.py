"""brief-intake: validate a brief, create the Firestore job, start the orchestrator."""
import logging
import uuid
from functools import lru_cache

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.cloud import firestore

import agent_client
from models import Brief

log = logging.getLogger("brief-intake")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="smm-brief-intake")
DONE_STATES = {"ready_for_review", "failed"}


@lru_cache(maxsize=1)
def db() -> firestore.Client:
    return firestore.Client()


@app.exception_handler(RequestValidationError)
async def invalid_brief(_: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body") or "body", "message": e["msg"]}
               for e in exc.errors()]
    return JSONResponse(status_code=400, content={"error": "invalid brief", "details": details})


def fail(job_id: str, message: str) -> None:
    try:
        db().collection("jobs").document(job_id).update(
            {"status": "failed", "error": message[:1500], "updated_at": firestore.SERVER_TIMESTAMP})
    except Exception:
        log.exception("could not mark job %s as failed", job_id)


def drive_agent(job_id: str, brief: dict) -> None:
    """Runs after the 202 has been sent. The agent updates the job itself; this only
    makes sure a crash on either side can never leave the job silently stuck."""
    try:
        agent_client.run_agent(job_id, brief)
        status = (db().collection("jobs").document(job_id).get().to_dict() or {}).get("status")
        if status not in DONE_STATES:
            fail(job_id, f"The agent run ended while the job was still '{status}'.")
    except Exception as e:
        log.exception("agent run failed for job %s", job_id)
        fail(job_id, f"Could not run the drafting agent: {e}")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/briefs", status_code=202)
def create_brief(brief: Brief, background: BackgroundTasks):
    job_id = str(uuid.uuid4())
    data = brief.model_dump(mode="json")
    db().collection("jobs").document(job_id).set({
        "brief": data, "status": "drafting", "drafts": [], "images": [], "error": None,
        "created_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP,
    })
    background.add_task(drive_agent, job_id, data)
    return {"job_id": job_id}
