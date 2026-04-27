import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False)
    display_name = Column(String(128), nullable=False)
    password_salt = Column(String(64), nullable=True)
    password_hash = Column(String(128), nullable=True)

    sessions = relationship("WorkSession", back_populates="worker")


class WorkSession(Base):
    __tablename__ = "work_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    note = Column(String(1000), nullable=True)
    # active | pending | approved | rejected
    status = Column(String(32), nullable=False, default="active")

    worker = relationship("Worker", back_populates="sessions")
    approvals = relationship("SessionApproval", back_populates="session", cascade="all, delete-orphan")
    screenshots = relationship("Screenshot", back_populates="session", cascade="all, delete-orphan")


class SessionApproval(Base):
    __tablename__ = "session_approvals"
    __table_args__ = (UniqueConstraint("session_id", "approver_worker_id", name="uq_session_approver"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("work_sessions.id"), nullable=False)
    approver_worker_id = Column(Integer, ForeignKey("workers.id"), nullable=False)
    approved = Column(Boolean, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("WorkSession", back_populates="approvals")


class Screenshot(Base):
    __tablename__ = "screenshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("work_sessions.id"), nullable=False)
    file_path = Column(String(512), nullable=False)
    captured_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    session = relationship("WorkSession", back_populates="screenshots")
