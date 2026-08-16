from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobSource(StrEnum):
    ADMIN_SCHEDULE = "admin_schedule"
    USER_SUBSCRIPTION = "user_subscription"
    STORED_COMMITMENT = "stored_commitment"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class ProactiveJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID = Field(default_factory=uuid4)
    source: JobSource
    channel: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=512)
    reason_event_id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=20_000)
    due_at: datetime
    recurrence_seconds: int | None = Field(default=None, ge=3600, le=365 * 24 * 3600)
    status: JobStatus = JobStatus.PENDING
    attempts: int = Field(default=0, ge=0, le=100)
    model_started_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_error: str | None = Field(default=None, max_length=2000)

    @field_validator("due_at", "model_started_at", "created_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("proactive job timestamps must include a timezone")
        return value.astimezone(UTC)


class ProactiveJobStore:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proactive_jobs (
                    job_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK(source IN (
                        'admin_schedule','user_subscription','stored_commitment'
                    )),
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    reason_event_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    recurrence_seconds INTEGER,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','running','completed','dead','cancelled'
                    )),
                    attempts INTEGER NOT NULL,
                    model_started_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_proactive_due
                    ON proactive_jobs(status, due_at);
                """
            )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE proactive_jobs
                SET status='dead', updated_at=?,
                    last_error='process stopped after model provider I/O may have begun'
                WHERE status='running' AND model_started_at IS NOT NULL
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE proactive_jobs SET status='pending', updated_at=?
                WHERE status='running' AND model_started_at IS NULL
                """,
                (now,),
            )
            connection.commit()

    async def add(self, job: ProactiveJob) -> None:
        if job.status is not JobStatus.PENDING or job.attempts != 0:
            raise ValueError("new proactive jobs must be pending and unattempted")
        async with self._lock:
            await asyncio.to_thread(self._add_sync, job)

    def _add_sync(self, job: ProactiveJob) -> None:
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO proactive_jobs(
                    job_id, source, channel, chat_id, reason_event_id, prompt,
                    due_at, recurrence_seconds, status, attempts, created_at,
                    model_started_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job.job_id),
                    job.source.value,
                    job.channel,
                    job.chat_id,
                    job.reason_event_id,
                    job.prompt,
                    job.due_at.isoformat(),
                    job.recurrence_seconds,
                    job.status.value,
                    job.attempts,
                    job.created_at.isoformat(),
                    None,
                    now,
                    job.last_error,
                ),
            )
            connection.commit()

    async def claim_due(self, now: datetime | None = None) -> ProactiveJob | None:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        async with self._lock:
            return await asyncio.to_thread(self._claim_due_sync, at)

    def _claim_due_sync(self, now: datetime) -> ProactiveJob | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM proactive_jobs
                WHERE status='pending' AND due_at <= ?
                ORDER BY due_at, created_at LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE proactive_jobs
                SET status='running', attempts=attempts+1, updated_at=?
                WHERE job_id=? AND status='pending'
                """,
                (now.isoformat(), row["job_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            connection.commit()
            value = dict(row)
            value["status"] = JobStatus.RUNNING.value
            value["attempts"] = int(row["attempts"]) + 1
            return self._decode(value)

    async def mark_model_started(self, job_id: UUID) -> bool:
        """Persist the no-retry boundary immediately before provider I/O."""

        async with self._lock:
            return await asyncio.to_thread(self._mark_model_started_sync, job_id)

    def _mark_model_started_sync(self, job_id: UUID) -> bool:
        with closing(self._connect()) as connection:
            now = datetime.now(UTC).isoformat()
            result = connection.execute(
                """
                UPDATE proactive_jobs SET model_started_at=?, updated_at=?
                WHERE job_id=? AND status='running' AND model_started_at IS NULL
                """,
                (now, now, str(job_id)),
            )
            if result.rowcount == 1:
                connection.commit()
                return True
            row = connection.execute(
                "SELECT status, model_started_at FROM proactive_jobs WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
            connection.commit()
            return bool(
                row is not None
                and row["status"] == JobStatus.RUNNING.value
                and row["model_started_at"] is not None
            )

    async def complete(self, job: ProactiveJob, *, now: datetime | None = None) -> None:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        async with self._lock:
            await asyncio.to_thread(self._complete_sync, job, at)

    def _complete_sync(self, job: ProactiveJob, now: datetime) -> None:
        if job.recurrence_seconds is None:
            status, due_at = JobStatus.COMPLETED, job.due_at
        else:
            due_at = job.due_at
            step = timedelta(seconds=job.recurrence_seconds)
            while due_at <= now:
                due_at += step
            status = JobStatus.PENDING
        self._transition(job.job_id, status, due_at, None)

    async def defer(self, job_id: UUID, *, delay: timedelta, reason: str) -> None:
        if delay.total_seconds() < 1:
            raise ValueError("defer delay must be positive")
        async with self._lock:
            await asyncio.to_thread(
                self._transition,
                job_id,
                JobStatus.PENDING,
                datetime.now(UTC) + delay,
                reason,
            )

    async def fail(self, job: ProactiveJob, reason: str, *, max_attempts: int = 3) -> None:
        async with self._lock:
            await asyncio.to_thread(self._fail_sync, job, reason, max_attempts)

    def _fail_sync(self, job: ProactiveJob, reason: str, max_attempts: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, model_started_at FROM proactive_jobs WHERE job_id=?",
                (str(job.job_id),),
            ).fetchone()
            if row is None or row["status"] != JobStatus.RUNNING.value:
                connection.rollback()
                raise RuntimeError("proactive job transition was not applied exactly once")
            status = (
                JobStatus.DEAD
                if row["model_started_at"] is not None or job.attempts >= max_attempts
                else JobStatus.PENDING
            )
            result = connection.execute(
                """
                UPDATE proactive_jobs
                SET status=?, due_at=?, updated_at=?, last_error=?, model_started_at=NULL
                WHERE job_id=? AND status='running'
                """,
                (
                    status.value,
                    (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    datetime.now(UTC).isoformat(),
                    reason[:2000],
                    str(job.job_id),
                ),
            )
            if result.rowcount != 1:
                connection.rollback()
                raise RuntimeError("proactive job transition was not applied exactly once")
            connection.commit()

    async def cancel(self, job_id: UUID) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._cancel_sync, job_id)

    def _cancel_sync(self, job_id: UUID) -> bool:
        with closing(self._connect()) as connection:
            result = connection.execute(
                """
                UPDATE proactive_jobs SET status='cancelled', updated_at=?
                WHERE job_id=? AND status IN ('pending','running')
                """,
                (datetime.now(UTC).isoformat(), str(job_id)),
            )
            connection.commit()
            return result.rowcount == 1

    def _transition(
        self,
        job_id: UUID,
        status: JobStatus,
        due_at: datetime,
        error: str | None,
    ) -> None:
        with closing(self._connect()) as connection:
            result = connection.execute(
                """
                UPDATE proactive_jobs
                SET status=?, due_at=?, updated_at=?, last_error=?, model_started_at=NULL
                WHERE job_id=? AND status='running'
                """,
                (
                    status.value,
                    due_at.astimezone(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    error[:2000] if error else None,
                    str(job_id),
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("proactive job transition was not applied exactly once")
            connection.commit()

    async def list(self, *, limit: int = 100) -> tuple[ProactiveJob, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("job list limit must be 1..1000")
        return await asyncio.to_thread(self._list_sync, limit)

    def _list_sync(self, limit: int) -> tuple[ProactiveJob, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM proactive_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._decode(dict(row)) for row in rows)

    @staticmethod
    def _decode(row: dict[str, object]) -> ProactiveJob:
        return ProactiveJob(
            job_id=UUID(str(row["job_id"])),
            source=JobSource(str(row["source"])),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            reason_event_id=str(row["reason_event_id"]),
            prompt=str(row["prompt"]),
            due_at=datetime.fromisoformat(str(row["due_at"])),
            recurrence_seconds=(
                int(str(row["recurrence_seconds"]))
                if row.get("recurrence_seconds") is not None
                else None
            ),
            status=JobStatus(str(row["status"])),
            attempts=int(str(row["attempts"])),
            model_started_at=(
                datetime.fromisoformat(str(row["model_started_at"]))
                if row.get("model_started_at")
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            last_error=str(row["last_error"]) if row.get("last_error") else None,
        )
