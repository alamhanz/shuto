"""Background job runner for the web app: one worker thread, progress stored in `jobs`."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from shuto.core.models import Job


class JobStore(Protocol):
    """The job table calls the runner needs (the `Store` has them)."""

    def create_job(self, kind: str, batch_id: str | None = None, total: int = 0) -> int: ...

    def update_job(self, job_id: int, **fields: Any) -> None: ...

    def get_job(self, job_id: int) -> Job | None: ...


MAX_KEPT_RESULTS = 50  # finished-job return values kept in memory (oldest dropped first)
JobFunc = Callable[["JobContext"], Any]


class JobBusy(Exception):
    """A job is already queued or running for this batch."""


class JobCancelled(Exception):
    """Raised inside a job function to stop at once; the job ends as cancelled."""


class JobContext:
    """Passed to the job function: report progress and poll for cancellation."""

    def __init__(
        self,
        store: JobStore,
        job_id: int,
        cancel_event: threading.Event,
        texts: dict[int, str] | None = None,
    ):
        self._store = store
        self._texts = texts if texts is not None else {}
        self.job_id = job_id
        self._cancel = cancel_event
        self.completed = False

    def progress(self, done: int, total: int, message: str = "") -> None:
        """Signature matches core.progress.Progress, so pass `ctx.progress` straight through."""
        self._store.update_job(self.job_id, done=done, total=total)

    def progress_text(self, done: int, total: int, message: str = "") -> None:
        """Like `progress`, and the message is kept for the job panel to show."""
        self.progress(done, total, message)
        self._texts[self.job_id] = message

    def cancelled(self) -> bool:
        """Signature matches core.progress.Cancel."""
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled

    def mark_completed(self) -> None:
        """The work finished although a cancel was asked for: end as done, not cancelled."""
        self.completed = True


class JobRunner:
    """Runs jobs one at a time, in submission order, on a daemon thread (started lazily).

    States: queued -> running -> done | failed | cancelled. At most one active job per batch.
    Jobs left `queued`/`running` by a closed process are not recovered: the web app marks them
    failed at startup (`Store.fail_unfinished_jobs`).
    """

    def __init__(self, store: JobStore):
        self.store = store
        self._queue: queue.Queue[tuple[int, JobFunc] | None] = queue.Queue()
        self._cancel: dict[int, threading.Event] = {}
        self._active: dict[str, int] = {}
        self._results: dict[int, Any] = {}
        self._texts: dict[int, str] = {}  # latest progress message of running jobs that set one
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, kind: str, batch_id: str, func: JobFunc, total: int = 0) -> int:
        with self._lock:
            if self._closed:
                raise JobBusy("shuto is about to update. Wait for it to restart.")
            if batch_id in self._active:
                raise JobBusy(f"Batch {batch_id} already has job {self._active[batch_id]} running.")
            job_id = self.store.create_job(kind, batch_id, total)
            self._active[batch_id] = job_id
            self._cancel[job_id] = threading.Event()
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="shuto-jobs", daemon=True)
                self._thread.start()
        self._queue.put((job_id, func))
        return job_id

    def busy(self) -> bool:
        """True while any job is queued or running."""
        with self._lock:
            return bool(self._active) or self._closed

    def active_kinds(self) -> set[str]:
        """Kinds of the jobs that are queued or running now."""
        with self._lock:
            ids = list(self._active.values())
        kinds = set()
        for job_id in ids:
            job = self.store.get_job(job_id)
            if job is not None:
                kinds.add(job.kind)
        return kinds

    def close(self) -> bool:
        """Refuse new jobs (an update is starting). False if a job is already active."""
        with self._lock:
            if self._active or self._closed:
                return False
            self._closed = True
            return True

    def reopen(self) -> None:
        with self._lock:
            self._closed = False

    def get(self, job_id: int) -> Job | None:
        return self.store.get_job(job_id)

    def result(self, job_id: int) -> Any:
        """Return value of a finished job's function (None if unfinished or failed)."""
        return self._results.get(job_id)

    def text(self, job_id: int) -> str:
        """The latest progress message of a running job ('' when it has none)."""
        return self._texts.get(job_id, "")

    def active_job(self, batch_id: str) -> int | None:
        return self._active.get(batch_id)

    def cancel(self, job_id: int) -> None:
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()

    def wait(self, job_id: int, timeout: float = 60.0) -> Job:
        """Block until the job finishes (used by tests and scripts)."""
        deadline = datetime.now().timestamp() + timeout
        while True:
            job = self.store.get_job(job_id)
            if job is not None and job.state in ("done", "failed", "cancelled"):
                return job
            if datetime.now().timestamp() > deadline:
                raise TimeoutError(f"job {job_id} did not finish within {timeout} s")
            threading.Event().wait(0.02)

    def shutdown(self) -> None:
        """Cancel active jobs and stop the worker (waits briefly)."""
        for event in self._cancel.values():
            event.set()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, func = item
            event = self._cancel[job_id]
            job = self.store.get_job(job_id)
            batch_id = job.batch_id if job else None
            state, error = "done", None
            if event.is_set():
                state = "cancelled"
            else:
                self.store.update_job(job_id, state="running")
                jctx = JobContext(self.store, job_id, event, self._texts)
                try:
                    self._results[job_id] = func(jctx)
                    while len(self._results) > MAX_KEPT_RESULTS:
                        self._results.pop(next(iter(self._results)))
                    if event.is_set() and not jctx.completed:
                        state = "cancelled"
                except JobCancelled:
                    state = "cancelled"
                except Exception as exc:
                    state, error = "failed", f"{type(exc).__name__}: {exc}"
            self.store.update_job(
                job_id, state=state, error=error, finished_at=datetime.now().isoformat()
            )
            self._texts.pop(job_id, None)
            with self._lock:
                if batch_id is not None:
                    self._active.pop(batch_id, None)
                self._cancel.pop(job_id, None)
