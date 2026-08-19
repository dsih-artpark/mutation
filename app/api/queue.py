"""Queue visibility: how deep it is, and roughly how long a new submission
would wait -- computed live rather than the once-at-submission snapshot
`queue_position` used to be.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import Principal, require_principal
from ..db import get_session
from ..progress import queue_snapshot
from ..schemas import QueueOut

router = APIRouter(prefix="/v1/queue", tags=["queue"])


@router.get("", response_model=QueueOut, response_model_exclude_none=True)
def get_queue(
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    """`running` names other users' run ids, so it is included only for a
    service key. Depth and wait are not sensitive and go to everyone (§14)."""
    return queue_snapshot(session, include_running=principal.kind == "service")
