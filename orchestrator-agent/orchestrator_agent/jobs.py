"""Firestore access for jobs/{job_id}. Kept thin so tests can patch these functions."""
import logging
from functools import lru_cache

from google.cloud import firestore

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _db() -> firestore.AsyncClient:
    return firestore.AsyncClient()


def _ref(job_id: str):
    return _db().collection("jobs").document(job_id)


async def update_job(job_id: str, **fields) -> None:
    await _ref(job_id).update({**fields, "updated_at": firestore.SERVER_TIMESTAMP})


async def append_image(job_id: str, image: dict) -> None:
    await update_job(job_id, images=firestore.ArrayUnion([image]))


async def fail_job(job_id: str, message: str) -> None:
    log.error("job %s failed: %s", job_id, message)
    try:
        await update_job(job_id, status="failed", error=message[:1500])
    except Exception:  # never mask the original error
        log.exception("could not mark job %s as failed", job_id)
