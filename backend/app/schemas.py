from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class WorkerOut(BaseModel):
    id: int
    slug: str
    display_name: str
    has_password: bool = False

    class Config:
        from_attributes = True


class SessionOut(BaseModel):
    id: str
    worker_id: int
    worker_name: str
    started_at: datetime
    ended_at: Optional[datetime]
    note: Optional[str] = None
    status: str
    duration_seconds: Optional[int] = None

    class Config:
        from_attributes = True


class SessionStartIn(BaseModel):
    worker_id: int


class WorkerLoginIn(BaseModel):
    password: str


class WorkerLoginOut(BaseModel):
    worker_id: int
    worker_name: str
    first_time_setup: bool


class LegacyImportIn(BaseModel):
    password: str


class LegacyImportOut(BaseModel):
    imported: bool
    message: str


class SessionStopIn(BaseModel):
    note: str


class ApprovalIn(BaseModel):
    approver_worker_id: int


class SessionEditIn(BaseModel):
    editor_worker_id: int
    new_duration_seconds: int


class DashboardWorkerSummary(BaseModel):
    worker: WorkerOut
    approved_seconds_today: int
    pending_seconds_today: int
    active_session: Optional[SessionOut] = None


class DashboardOut(BaseModel):
    workers: List[DashboardWorkerSummary]
    pending_approval_sessions: List[SessionOut] = Field(
        default_factory=list,
        description="Sessions the given approver still needs to act on.",
    )
    all_pending_sessions: List[SessionOut] = Field(
        default_factory=list,
        description="All sessions awaiting two approvals.",
    )


class ScreenshotOut(BaseModel):
    id: str
    session_id: str
    captured_at: datetime
    url: str
