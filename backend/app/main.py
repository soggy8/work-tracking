import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import DATA_DIR, Base, SessionLocal, engine, get_db
from .models import Screenshot, SessionApproval, Worker, WorkSession
from .schemas import (
    ApprovalIn,
    DashboardOut,
    DashboardWorkerSummary,
    SessionOut,
    SessionStartIn,
    ScreenshotOut,
    WorkerOut,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Work Tracking")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _session_duration_seconds(s: WorkSession) -> Optional[int]:
    if s.ended_at is None:
        return None
    return int((s.ended_at - s.started_at).total_seconds())


def _session_to_out(s: WorkSession, worker_name: str) -> SessionOut:
    return SessionOut(
        id=s.id,
        worker_id=s.worker_id,
        worker_name=worker_name,
        started_at=s.started_at,
        ended_at=s.ended_at,
        status=s.status,
        duration_seconds=_session_duration_seconds(s),
    )


def seed_workers(db: Session) -> None:
    expected = [
        ("andrej", "Andrej"),
        ("krste", "Krste"),
        ("filip", "Filip"),
    ]
    for slug, name in expected:
        row = db.query(Worker).filter(Worker.slug == slug).first()
        if not row:
            db.add(Worker(slug=slug, display_name=name))
    db.commit()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_workers(db)
    finally:
        db.close()


@app.get("/api/workers", response_model=List[WorkerOut])
def list_workers(db: Session = Depends(get_db)):
    return db.query(Worker).order_by(Worker.id).all()


def _day_bounds_utc(d: date):
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return start, end


def _sum_duration_for_worker_today(
    db: Session, worker_id: int, statuses: List[str]
) -> int:
    today = datetime.utcnow().date()
    day_start, day_end = _day_bounds_utc(today)
    q = (
        db.query(
            func.sum(
                func.strftime(
                    "%s",
                    WorkSession.ended_at,
                )
                - func.strftime("%s", WorkSession.started_at)
            )
        )
        .filter(WorkSession.worker_id == worker_id)
        .filter(WorkSession.status.in_(statuses))
        .filter(WorkSession.ended_at.isnot(None))
        .filter(WorkSession.ended_at >= day_start)
        .filter(WorkSession.ended_at < day_end)
    )
    total = q.scalar()
    return int(total or 0)


@app.get("/api/dashboard", response_model=DashboardOut)
def dashboard(approver_worker_id: Optional[int] = None, db: Session = Depends(get_db)):
    workers = db.query(Worker).order_by(Worker.id).all()
    summaries: List[DashboardWorkerSummary] = []
    for w in workers:
        approved = _sum_duration_for_worker_today(db, w.id, ["approved"])
        pending = _sum_duration_for_worker_today(db, w.id, ["pending"])
        active = (
            db.query(WorkSession)
            .filter(WorkSession.worker_id == w.id, WorkSession.status == "active")
            .first()
        )
        active_out = None
        if active:
            active_out = _session_to_out(active, w.display_name)
        summaries.append(
            DashboardWorkerSummary(
                worker=WorkerOut.model_validate(w),
                approved_seconds_today=approved,
                pending_seconds_today=pending,
                active_session=active_out,
            )
        )

    pending_sessions: List[SessionOut] = []
    if approver_worker_id is not None:
        approver = db.query(Worker).filter(Worker.id == approver_worker_id).first()
        if not approver:
            raise HTTPException(404, "Unknown approver_worker_id")
        others = db.query(Worker.id).filter(Worker.id != approver_worker_id).all()
        other_ids = [o[0] for o in others]
        pending = (
            db.query(WorkSession)
            .filter(WorkSession.status == "pending")
            .filter(WorkSession.worker_id != approver_worker_id)
            .all()
        )
        for s in pending:
            if s.worker_id not in other_ids:
                continue
            existing = (
                db.query(SessionApproval)
                .filter(
                    SessionApproval.session_id == s.id,
                    SessionApproval.approver_worker_id == approver_worker_id,
                )
                .first()
            )
            if existing is None:
                wname = db.query(Worker).filter(Worker.id == s.worker_id).first()
                pending_sessions.append(_session_to_out(s, wname.display_name if wname else "?"))

    all_pending_raw = db.query(WorkSession).filter(WorkSession.status == "pending").all()
    all_pending: List[SessionOut] = []
    for s in all_pending_raw:
        wname = db.query(Worker).filter(Worker.id == s.worker_id).first()
        all_pending.append(_session_to_out(s, wname.display_name if wname else "?"))

    return DashboardOut(
        workers=summaries,
        pending_approval_sessions=pending_sessions,
        all_pending_sessions=all_pending,
    )


@app.post("/api/sessions/start", response_model=SessionOut)
def start_session(body: SessionStartIn, db: Session = Depends(get_db)):
    w = db.query(Worker).filter(Worker.id == body.worker_id).first()
    if not w:
        raise HTTPException(404, "Worker not found")
    existing = (
        db.query(WorkSession)
        .filter(WorkSession.worker_id == body.worker_id, WorkSession.status == "active")
        .first()
    )
    if existing:
        raise HTTPException(400, "Already have an active session")
    s = WorkSession(worker_id=body.worker_id, started_at=datetime.utcnow(), status="active")
    db.add(s)
    db.commit()
    db.refresh(s)
    return _session_to_out(s, w.display_name)


@app.post("/api/sessions/{session_id}/stop", response_model=SessionOut)
def stop_session(session_id: str, db: Session = Depends(get_db)):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "active":
        raise HTTPException(400, "Session is not active")
    s.ended_at = datetime.utcnow()
    s.status = "pending"
    db.commit()
    db.refresh(s)
    w = db.query(Worker).filter(Worker.id == s.worker_id).first()
    return _session_to_out(s, w.display_name if w else "?")


@app.post("/api/sessions/{session_id}/approve", response_model=SessionOut)
def approve_session(session_id: str, body: ApprovalIn, db: Session = Depends(get_db)):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "pending":
        raise HTTPException(400, "Session is not pending approval")
    if body.approver_worker_id == s.worker_id:
        raise HTTPException(400, "Cannot approve your own session")
    approver = db.query(Worker).filter(Worker.id == body.approver_worker_id).first()
    if not approver:
        raise HTTPException(404, "Approver not found")

    existing = (
        db.query(SessionApproval)
        .filter(
            SessionApproval.session_id == session_id,
            SessionApproval.approver_worker_id == body.approver_worker_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "Already responded")

    db.add(
        SessionApproval(
            session_id=session_id,
            approver_worker_id=body.approver_worker_id,
            approved=True,
        )
    )
    db.commit()
    db.refresh(s)
    _finalize_session_if_fully_approved(db, s)
    db.refresh(s)
    w = db.query(Worker).filter(Worker.id == s.worker_id).first()
    return _session_to_out(s, w.display_name if w else "?")


@app.post("/api/sessions/{session_id}/reject", response_model=SessionOut)
def reject_session(session_id: str, body: ApprovalIn, db: Session = Depends(get_db)):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "pending":
        raise HTTPException(400, "Session is not pending approval")
    if body.approver_worker_id == s.worker_id:
        raise HTTPException(400, "Cannot reject your own session")

    existing = (
        db.query(SessionApproval)
        .filter(
            SessionApproval.session_id == session_id,
            SessionApproval.approver_worker_id == body.approver_worker_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "Already responded")

    db.add(
        SessionApproval(
            session_id=session_id,
            approver_worker_id=body.approver_worker_id,
            approved=False,
        )
    )
    s.status = "rejected"
    db.commit()
    db.refresh(s)
    w = db.query(Worker).filter(Worker.id == s.worker_id).first()
    return _session_to_out(s, w.display_name if w else "?")


def _finalize_session_if_fully_approved(db: Session, s: WorkSession) -> None:
    """If both other workers approved, mark session approved."""
    if s.status != "pending":
        return
    others = db.query(Worker.id).filter(Worker.id != s.worker_id).all()
    other_ids = [o[0] for o in others]
    approvals = (
        db.query(SessionApproval)
        .filter(SessionApproval.session_id == s.id, SessionApproval.approved.is_(True))
        .all()
    )
    approver_ids = {a.approver_worker_id for a in approvals}
    if len(other_ids) == 2 and all(oid in approver_ids for oid in other_ids):
        s.status = "approved"
        db.commit()


@app.post("/api/sessions/{session_id}/screenshot", response_model=ScreenshotOut)
async def upload_screenshot(
    session_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "active":
        raise HTTPException(400, "Screenshots only while session is active")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    sid_dir = SCREENSHOTS_DIR / session_id
    sid_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = sid_dir / fname
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "File too large")
    async with aiofiles.open(fpath, "wb") as out:
        await out.write(content)

    rel = f"{session_id}/{fname}"
    shot = Screenshot(session_id=session_id, file_path=rel, captured_at=datetime.utcnow())
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return ScreenshotOut(
        id=shot.id,
        session_id=shot.session_id,
        captured_at=shot.captured_at,
        url=f"/api/screenshots/file/{shot.id}",
    )


@app.get("/api/screenshots/file/{screenshot_id}")
def get_screenshot_file(screenshot_id: str, db: Session = Depends(get_db)):
    sh = db.query(Screenshot).filter(Screenshot.id == screenshot_id).first()
    if not sh:
        raise HTTPException(404, "Not found")
    path = SCREENSHOTS_DIR / sh.file_path
    if not path.is_file():
        raise HTTPException(404, "Missing file")
    return FileResponse(path)


@app.get("/api/sessions/{session_id}/screenshots", response_model=List[ScreenshotOut])
def list_screenshots(session_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(Screenshot)
        .filter(Screenshot.session_id == session_id)
        .order_by(Screenshot.captured_at.desc())
        .all()
    )
    return [
        ScreenshotOut(
            id=r.id,
            session_id=r.session_id,
            captured_at=r.captured_at,
            url=f"/api/screenshots/file/{r.id}",
        )
        for r in rows
    ]


@app.get("/api/history/daily")
def daily_history(
    days: int = 14,
    db: Session = Depends(get_db),
):
    """Return per-worker approved seconds per calendar day (UTC) for last N days."""
    if days < 1 or days > 90:
        days = 14
    end = datetime.utcnow().date()
    start = end - timedelta(days=days - 1)
    workers = db.query(Worker).order_by(Worker.id).all()
    result = []
    d = start
    while d <= end:
        day_start, day_end = _day_bounds_utc(d)
        row = {"date": d.isoformat(), "by_worker": {}}
        for w in workers:
            q = (
                db.query(
                    func.sum(
                        func.strftime("%s", WorkSession.ended_at)
                        - func.strftime("%s", WorkSession.started_at)
                    )
                )
                .filter(WorkSession.worker_id == w.id)
                .filter(WorkSession.status == "approved")
                .filter(WorkSession.ended_at.isnot(None))
                .filter(WorkSession.ended_at >= day_start)
                .filter(WorkSession.ended_at < day_end)
            )
            sec = int(q.scalar() or 0)
            row["by_worker"][w.display_name] = sec
        result.append(row)
        d += timedelta(days=1)
    return {"days": result}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
