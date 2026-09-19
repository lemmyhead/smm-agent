"""Calls the deployed orchestrator on Vertex AI Agent Engine over REST (as sa-brief-intake)."""
import json
import logging
import os

import google.auth
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)
USER_ID = "brief-intake"


def _base_url(resource_name: str) -> str:
    # projects/{project}/locations/{location}/reasoningEngines/{id}
    location = resource_name.split("/")[3]
    return f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}"


def run_agent(job_id: str, brief: dict, resource_name: str | None = None) -> None:
    """Creates a session and streams the agent run to completion (blocks for the whole run)."""
    resource_name = resource_name or os.environ["AGENT_ENGINE_RESOURCE"]
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    http = AuthorizedSession(creds)
    base = _base_url(resource_name)

    r = http.post(f"{base}:query", timeout=60,
                  json={"class_method": "async_create_session", "input": {"user_id": USER_ID}})
    if r.status_code != 200:
        raise RuntimeError(f"creating agent session failed ({r.status_code}): {r.text[:300]}")
    out = r.json().get("output") or {}
    session_id = out.get("id") or (out.get("session") or {}).get("id")

    agent_input = {"user_id": USER_ID, "message": json.dumps({"job_id": job_id, "brief": brief})}
    if session_id:
        agent_input["session_id"] = session_id
    with http.post(f"{base}:streamQuery?alt=sse", stream=True, timeout=(15, 900),
                   json={"class_method": "async_stream_query", "input": agent_input}) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"agent query failed ({resp.status_code}): {resp.text[:300]}")
        chunks = sum(1 for line in resp.iter_lines() if line)  # drain: the run ends when the stream does
    log.info("agent run for job %s finished (%d stream chunks)", job_id, chunks)
