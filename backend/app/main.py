import uuid
import hashlib
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .database import DATA_DIR, Base, SessionLocal, engine, get_db
from .models import Screenshot, SessionApproval, Worker, WorkSession
from .schemas import (
    ApprovalIn,
    DashboardOut,
    DashboardWorkerSummary,
    LegacyImportIn,
    LegacyImportOut,
    WorkerLoginIn,
    WorkerLoginOut,
    SessionEditIn,
    SessionOut,
    SessionStartIn,
    SessionStopIn,
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
        note=s.note,
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
        ensure_schema(db)
        seed_workers(db)
    finally:
        db.close()


def ensure_schema(db: Session) -> None:
    """Add missing columns for older local SQLite databases."""
    bind = db.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name != "sqlite":
        # PRAGMA/ALTER logic below is SQLite-specific.
        return

    cols = db.execute(text("PRAGMA table_info(work_sessions)")).fetchall()
    names = {c[1] for c in cols}
    if "note" not in names:
        db.execute(text("ALTER TABLE work_sessions ADD COLUMN note VARCHAR(1000)"))
        db.commit()
    worker_cols = db.execute(text("PRAGMA table_info(workers)")).fetchall()
    worker_names = {c[1] for c in worker_cols}
    if "password_salt" not in worker_names:
        db.execute(text("ALTER TABLE workers ADD COLUMN password_salt VARCHAR(64)"))
        db.commit()
    if "password_hash" not in worker_names:
        db.execute(text("ALTER TABLE workers ADD COLUMN password_hash VARCHAR(128)"))
        db.commit()


@app.get("/api/workers", response_model=List[WorkerOut])
def list_workers(db: Session = Depends(get_db)):
    workers = db.query(Worker).order_by(Worker.id).all()
    return [
        WorkerOut(
            id=w.id,
            slug=w.slug,
            display_name=w.display_name,
            has_password=bool(w.password_hash and w.password_salt),
        )
        for w in workers
    ]


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


@app.post("/api/workers/{worker_id}/login", response_model=WorkerLoginOut)
def worker_login(worker_id: int, body: WorkerLoginIn, db: Session = Depends(get_db)):
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(404, "Worker not found")
    password = body.password.strip()
    if len(password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    has_password = bool(worker.password_hash and worker.password_salt)
    if not has_password:
        salt = secrets.token_hex(16)
        worker.password_salt = salt
        worker.password_hash = _hash_password(password, salt)
        db.commit()
        return WorkerLoginOut(
            worker_id=worker.id,
            worker_name=worker.display_name,
            first_time_setup=True,
        )

    expected = _hash_password(password, worker.password_salt or "")
    if expected != worker.password_hash:
        raise HTTPException(401, "Wrong password")
    return WorkerLoginOut(
        worker_id=worker.id,
        worker_name=worker.display_name,
        first_time_setup=False,
    )


@app.post("/api/admin/import-legacy-render-day", response_model=LegacyImportOut)
def import_legacy_render_day(body: LegacyImportIn, db: Session = Depends(get_db)):
    """One-time helper to recover the small Render SQLite data snapshot.

    Protected by a temporary shared password requested by user.
    """
    if body.password != "test":
        raise HTTPException(403, "Wrong import password")

    workers = {w.slug: w for w in db.query(Worker).all()}
    required = ("andrej", "krste", "filip")
    if not all(slug in workers for slug in required):
        raise HTTPException(400, "Missing required workers")

    # Recovery data from the screenshot:
    # 2026-04-26 approved: Andrej 3h9m, Krste 4h11m, Filip 2h31m
    # Plus Andrej currently has 34m pending approval.
    day = date(2026, 4, 26)
    day_start = datetime(day.year, day.month, day.day, 9, 0, 0)
    import_rows = [
        ("andrej", 3 * 3600 + 9 * 60, "legacy-import-2026-04-26-approved"),
        ("krste", 4 * 3600 + 11 * 60, "legacy-import-2026-04-26-approved"),
        ("filip", 2 * 3600 + 31 * 60, "legacy-import-2026-04-26-approved"),
    ]

    created = 0
    for slug, duration, key in import_rows:
        worker = workers[slug]
        exists = (
            db.query(WorkSession)
            .filter(
                WorkSession.worker_id == worker.id,
                WorkSession.note == key,
                WorkSession.status == "approved",
            )
            .first()
        )
        if exists:
            continue
        started = day_start
        ended = started + timedelta(seconds=duration)
        db.add(
            WorkSession(
                worker_id=worker.id,
                started_at=started,
                ended_at=ended,
                note=key,
                status="approved",
            )
        )
        created += 1

    pending_key = "legacy-import-andrej-pending-34m"
    pending_exists = (
        db.query(WorkSession)
        .filter(
            WorkSession.worker_id == workers["andrej"].id,
            WorkSession.note == pending_key,
            WorkSession.status == "pending",
        )
        .first()
    )
    if not pending_exists:
        now = datetime.utcnow()
        db.add(
            WorkSession(
                worker_id=workers["andrej"].id,
                started_at=now - timedelta(minutes=34),
                ended_at=now,
                note=pending_key,
                status="pending",
            )
        )
        created += 1

    if created:
        db.commit()
        return LegacyImportOut(
            imported=True,
            imported_count=created,
            message=f"Imported {created} legacy sessions (3 approved + 1 pending).",
        )
    return LegacyImportOut(
        imported=False,
        imported_count=0,
        message="Legacy sessions already imported.",
    )


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
def stop_session(session_id: str, body: SessionStopIn, db: Session = Depends(get_db)):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "active":
        raise HTTPException(400, "Session is not active")
    note = body.note.strip()
    if len(note) < 3:
        raise HTTPException(400, "Please write a short note (at least 3 characters)")
    s.ended_at = datetime.utcnow()
    s.note = note
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


@app.post("/api/sessions/{session_id}/reduce", response_model=SessionOut)
def reduce_pending_session(
    session_id: str,
    body: SessionEditIn,
    db: Session = Depends(get_db),
):
    s = db.query(WorkSession).filter(WorkSession.id == session_id).first()
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "pending":
        raise HTTPException(400, "Only pending sessions can be edited")
    if body.editor_worker_id != s.worker_id:
        raise HTTPException(403, "Only session owner can edit this session")
    if s.ended_at is None:
        raise HTTPException(400, "Cannot edit a session without end time")

    current_seconds = int((s.ended_at - s.started_at).total_seconds())
    if body.new_duration_seconds < 1:
        raise HTTPException(400, "new_duration_seconds must be at least 1")
    if body.new_duration_seconds >= current_seconds:
        raise HTTPException(400, "Can only reduce duration, not increase or keep same")

    s.ended_at = s.started_at + timedelta(seconds=body.new_duration_seconds)
    # If anyone already voted, reset approvals because the duration changed.
    db.query(SessionApproval).filter(SessionApproval.session_id == s.id).delete()
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
    """Return per-worker approved seconds per calendar day (UTC).

    days=0 means all available history from the first approved session day.
    """
    end = datetime.utcnow().date()
    if days == 0:
        first_approved_end = (
            db.query(func.min(WorkSession.ended_at))
            .filter(WorkSession.status == "approved")
            .filter(WorkSession.ended_at.isnot(None))
            .scalar()
        )
        start = first_approved_end.date() if first_approved_end else end
    else:
        if days < 1:
            days = 14
        # Keep this large to support long-term history while avoiding accidental huge scans.
        if days > 36500:
            days = 36500
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


@app.get("/api/history/this-week")
def this_week_totals(db: Session = Depends(get_db)):
    """Return approved totals per worker for current UTC week (Mon-Sun)."""
    today = datetime.utcnow().date()
    week_start_date = today - timedelta(days=today.weekday())
    week_end_date = week_start_date + timedelta(days=7)
    week_start = datetime(
        week_start_date.year,
        week_start_date.month,
        week_start_date.day,
    )
    week_end = datetime(
        week_end_date.year,
        week_end_date.month,
        week_end_date.day,
    )
    workers = db.query(Worker).order_by(Worker.id).all()
    totals = {}
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
            .filter(WorkSession.ended_at >= week_start)
            .filter(WorkSession.ended_at < week_end)
        )
        totals[w.display_name] = int(q.scalar() or 0)
    return {
        "week_start": week_start_date.isoformat(),
        "week_end_exclusive": week_end_date.isoformat(),
        "totals": totals,
    }


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
