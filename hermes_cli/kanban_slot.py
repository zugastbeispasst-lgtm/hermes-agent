"""Persistent kanban worker slot — ``hermes kanban worker-slot``.

The default kanban topology spawns **one subprocess per card** from the
gateway dispatcher (:func:`hermes_cli.kanban_db.dispatch_once` →
``_default_spawn``). That is simple and crash-isolated, but every card
pays the interpreter start, config load and credential-pool build, and a
card whose worker dies is re-queued from scratch on the next tick.

This module implements the opt-in alternative: a single long-lived
process that

  * pulls ``ready`` cards **one at a time** (claim → execute → complete /
    block → next card),
  * executes each card **inside its own interpreter** instead of spawning
    a ``hermes`` subprocess per card (session/process reuse; see
    :func:`default_executor` for the exact semantics),
  * **resumes** cards whose worker vanished instead of re-running them
    from zero: the orphaned ``running`` card keeps its ``current_run_id``
    and its accumulated ``task_runs`` history, only the claim lease is
    re-issued (``kanban_db.adopt_running_task``).

Enabling is a config flip::

    kanban:
      worker_mode: persistent   # default: dispatcher

Read at boot, so the flip needs a gateway restart (see
``docs/kanban/persistent-worker-slot.md``). In ``persistent`` mode the
gateway's embedded dispatcher stays idle — it logs
``idle (worker_mode=persistent)`` and spawns nothing — which is what
prevents a double claim. ``dispatcher`` keeps today's behaviour bit for
bit, including a missing or unrecognised ``worker_mode`` key.

Design notes worth knowing before editing:

* **Never** call ``kanban_db.release_stale_claims()`` from the slot. It
  runs at the top of ``dispatch_once`` and would flip an orphaned
  ``running`` card back to ``ready`` — i.e. turn the intended *resume*
  into a *re-run*. The slot iterates
  ``kanban_db.resumable_running_tasks()`` and adopts before it ever looks
  at the ready queue.
* The slot records **its own pid** as ``worker_pid``. That makes the
  existing live-pid protection in ``release_stale_claims`` extend the
  lease while the slot is alive, and makes the card instantly resumable
  (pid dead) once the slot is gone.
* One card at a time is structural: the loop is single-threaded and a
  module-level lock rejects re-entrant execution.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config: kanban.worker_mode
# ---------------------------------------------------------------------------

WORKER_MODE_CONFIG_KEY = "kanban.worker_mode"
WORKER_MODE_DISPATCHER = "dispatcher"
WORKER_MODE_PERSISTENT = "persistent"
VALID_WORKER_MODES = (WORKER_MODE_DISPATCHER, WORKER_MODE_PERSISTENT)

#: Default claim lease for slot-owned cards. The slot's live pid protects
#: the lease from being reclaimed while it runs; the TTL is only the
#: upper bound after the slot process itself disappeared.
DEFAULT_SLOT_TTL_SECONDS = kb.DEFAULT_CLAIM_TTL_SECONDS

#: Idle poll interval between empty board scans.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class WorkerModeSetting:
    """Result of reading ``kanban.worker_mode`` once at boot.

    ``mode`` is always one of :data:`VALID_WORKER_MODES` — an unknown or
    malformed value is downgraded to ``dispatcher`` so a typo can never
    silently change runtime behaviour. ``source`` names where the value
    came from and is what the CLI/gateway log, so an operator can tell
    "config says dispatcher" apart from "key absent, defaulted".
    """

    mode: str
    source: str
    raw: Any = None

    @property
    def is_persistent(self) -> bool:
        return self.mode == WORKER_MODE_PERSISTENT

    def describe(self) -> str:
        return f"worker_mode={self.mode} (source: {self.source})"


def parse_worker_mode(kanban_cfg: Optional[Mapping[str, Any]]) -> WorkerModeSetting:
    """Parse the ``kanban`` config mapping into a :class:`WorkerModeSetting`.

    * key absent → ``dispatcher`` (today's behaviour, no warning)
    * known value → that value
    * unknown / non-string / empty → warn + ``dispatcher``
    """
    if not isinstance(kanban_cfg, Mapping) or "worker_mode" not in kanban_cfg:
        return WorkerModeSetting(
            mode=WORKER_MODE_DISPATCHER,
            source=f"{WORKER_MODE_CONFIG_KEY} not set (default)",
        )
    raw = kanban_cfg.get("worker_mode")
    if not isinstance(raw, str):
        logger.warning(
            "kanban: %s=%r is not a string; falling back to %r",
            WORKER_MODE_CONFIG_KEY, raw, WORKER_MODE_DISPATCHER,
        )
        return WorkerModeSetting(
            mode=WORKER_MODE_DISPATCHER,
            source=f"fallback (invalid {WORKER_MODE_CONFIG_KEY}={raw!r})",
            raw=raw,
        )
    value = raw.strip().lower()
    if value not in VALID_WORKER_MODES:
        logger.warning(
            "kanban: %s=%r is not one of %s; falling back to %r",
            WORKER_MODE_CONFIG_KEY, raw, ", ".join(VALID_WORKER_MODES),
            WORKER_MODE_DISPATCHER,
        )
        return WorkerModeSetting(
            mode=WORKER_MODE_DISPATCHER,
            source=f"fallback (invalid {WORKER_MODE_CONFIG_KEY}={raw!r})",
            raw=raw,
        )
    return WorkerModeSetting(
        mode=value,
        source=f"config.yaml {WORKER_MODE_CONFIG_KEY}",
        raw=raw,
    )


def read_worker_mode(config: Optional[Mapping[str, Any]] = None) -> WorkerModeSetting:
    """Read ``kanban.worker_mode`` from a config mapping or from disk.

    ``config=None`` loads ``config.yaml`` (profile-aware via
    ``HERMES_HOME``). Config-load failures are not fatal: the slot and the
    gateway both fall back to ``dispatcher``, which is the safe default.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("kanban: cannot load config (%s); using %r", exc, WORKER_MODE_DISPATCHER)
            return WorkerModeSetting(
                mode=WORKER_MODE_DISPATCHER,
                source="fallback (config load failed)",
            )
    kanban_cfg = config.get("kanban", {}) if isinstance(config, Mapping) else {}
    return parse_worker_mode(kanban_cfg)


# ---------------------------------------------------------------------------
# Executor contract
# ---------------------------------------------------------------------------

@dataclass
class ExecutorResult:
    """What an executor reports back to the slot.

    The executor may finalise the card itself (the agent's kanban tools do
    exactly that). The slot only acts on this result when the card is
    *still* ``running`` afterwards — see :meth:`WorkerSlot._run_task`.
    """

    outcome: str = "completed"          # "completed" | "blocked"
    summary: str = ""
    metadata: dict = field(default_factory=dict)
    reason: str = ""


#: ``executor(task, workspace, conn, *, resumed) -> ExecutorResult | None``
Executor = Callable[..., Optional[ExecutorResult]]


@dataclass
class SlotTick:
    """Outcome of a single :meth:`WorkerSlot.run_once` pass."""

    idle: bool = True
    task_id: Optional[str] = None
    resumed: bool = False
    outcome: Optional[str] = None
    error: Optional[str] = None
    adopted: tuple[str, ...] = ()

    @property
    def did_work(self) -> bool:
        return self.task_id is not None


# ---------------------------------------------------------------------------
# Default in-process executor
# ---------------------------------------------------------------------------

def build_slot_prompt(task: kb.Task, conn, *, resumed: bool) -> str:
    """Prompt handed to the in-process agent for one card.

    Fresh cards get the same one-liner the subprocess dispatcher uses, so
    the worker experience is identical. Resumed cards get an explicit
    "this run already exists" instruction **plus** the full rendered
    context (body, comments, prior attempts, parent handoffs) so the
    agent continues instead of restarting the task.
    """
    if not resumed:
        return f"work kanban task {task.id}"
    context = kb.build_worker_context(conn, task.id)
    run_id = task.current_run_id
    return (
        f"resume kanban task {task.id} (run {run_id}).\n\n"
        "A previous worker started this card and died before finishing. "
        "This is a RESUME, not a fresh start: the run is the same row, so do not "
        "re-do work that the context below already shows as done — continue from it.\n\n"
        f"{context}"
    )


class _TaskEnvironment(ContextManager):
    """Per-card environment + cwd for in-process execution.

    Mirrors what ``kanban_db._default_spawn`` puts into a subprocess env so
    the in-process agent sees exactly the same kanban coordinates
    (task id, workspace, run id, claim lock, DB path, acting profile) that
    a spawned worker would.
    """

    _MANAGED = (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_PROFILE",
        "HERMES_HOME",
        "HERMES_TENANT",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
    )

    def __init__(self, task: kb.Task, workspace: Path, *, board: Optional[str] = None):
        self.task = task
        self.workspace = workspace
        self.board = board
        self._saved: dict[str, Optional[str]] = {}
        self._saved_cwd: Optional[str] = None

    def __enter__(self) -> "_TaskEnvironment":
        self._saved = {k: os.environ.get(k) for k in self._MANAGED}
        task = self.task
        os.environ["HERMES_KANBAN_TASK"] = task.id
        os.environ["HERMES_KANBAN_WORKSPACE"] = str(self.workspace)
        if task.current_run_id is not None:
            os.environ["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
        if task.claim_lock:
            os.environ["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
        os.environ["HERMES_KANBAN_DB"] = str(kb.kanban_db_path(board=self.board))
        os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(kb.workspaces_root(board=self.board))
        if task.tenant:
            os.environ["HERMES_TENANT"] = task.tenant
        # Non-interactive: nothing can answer an approval prompt.
        os.environ["HERMES_YOLO_MODE"] = "1"
        os.environ["HERMES_ACCEPT_HOOKS"] = "1"
        if task.assignee:
            os.environ["HERMES_PROFILE"] = task.assignee
            try:
                from hermes_cli.profiles import resolve_profile_env

                os.environ["HERMES_HOME"] = resolve_profile_env(task.assignee)
            except Exception:
                # Unknown/vanished profile: keep the slot's own HERMES_HOME
                # rather than pointing the agent at a non-existent dir.
                pass
        try:
            self._saved_cwd = os.getcwd()
        except OSError:  # pragma: no cover - deleted cwd
            self._saved_cwd = None
        if self.workspace.is_dir():
            os.chdir(self.workspace)
        return self

    def __exit__(self, *exc_info) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._saved_cwd:
            try:
                os.chdir(self._saved_cwd)
            except OSError:  # pragma: no cover
                pass


def default_executor(
    task: kb.Task,
    workspace: Path,
    conn,
    *,
    resumed: bool = False,
    board: Optional[str] = None,
) -> ExecutorResult:
    """Run one card through an in-process ``AIAgent``.

    Process reuse, not transcript reuse: the agent's constructor, config
    load and credential pool are shared across cards, but each card gets a
    fresh conversation. Carrying a transcript across unrelated cards would
    bleed context from a finished task into the next one — the opposite of
    what a handoff summary is for. Context continuity comes from
    ``build_worker_context`` instead, which is exactly the data a resumed
    card needs.

    Imports are lazy so importing ``kanban_slot`` (e.g. from the gateway)
    stays cheap and does not pull ``run_agent`` into every process.
    """
    from hermes_cli.oneshot import run_prompt_inprocess

    prompt = build_slot_prompt(task, conn, resumed=resumed)
    with _TaskEnvironment(task, Path(workspace), board=board):
        response = run_prompt_inprocess(prompt)
    summary = (response or "").strip()
    if len(summary) > kb._CTX_MAX_FIELD_BYTES:
        summary = summary[: kb._CTX_MAX_FIELD_BYTES]
    return ExecutorResult(outcome="completed", summary=summary)


# ---------------------------------------------------------------------------
# Slot
# ---------------------------------------------------------------------------

_SLOT_RUN_LOCK = threading.Lock()


def slot_pid_path(board: Optional[str] = None) -> Path:
    """``<board-root>/worker-slot.pid`` — the slot's PID file."""
    return kb.board_dir(board=board) / "worker-slot.pid"


class WorkerSlot:
    """One long-lived process that drains the board card by card.

    Not thread-safe by design: a single slot per board. Two slots on the
    same board are rejected by the PID-file lock (:meth:`serve`), and
    re-entrant ``run_once`` calls are rejected by a module lock.
    """

    def __init__(
        self,
        *,
        board: Optional[str] = None,
        executor: Optional[Executor] = None,
        worker_mode: Optional[str] = None,
        ttl_seconds: int = DEFAULT_SLOT_TTL_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        claimer: Optional[str] = None,
        require_real_profile: bool = True,
    ) -> None:
        if worker_mode is None:
            setting = read_worker_mode()
            self.worker_mode = setting.mode
            self.worker_mode_setting: Optional[WorkerModeSetting] = setting
        else:
            value = str(worker_mode).strip().lower()
            if value not in VALID_WORKER_MODES:
                raise ValueError(
                    f"invalid worker_mode {worker_mode!r}; expected one of {VALID_WORKER_MODES}"
                )
            self.worker_mode = value
            self.worker_mode_setting = None
        self.board = board
        self.executor = executor or default_executor
        self.ttl_seconds = int(ttl_seconds)
        self.poll_interval = float(poll_interval)
        # One identity for the whole slot: the claimer id is what
        # release_stale_claims compares against, and reusing it means a
        # restart keeps owning the leases it re-issued.
        self.claimer = claimer or f"slot:{os.getpid()}"
        self.require_real_profile = require_real_profile
        self._pidfile_path = slot_pid_path(board=board)
        self._pidfile_fh = None

    # -- connection -------------------------------------------------------

    def connect(self):
        return kb.connect(board=self.board)

    # -- pid file ---------------------------------------------------------

    def acquire_pidfile(self) -> Path:
        """Write + flock the PID file; raises RuntimeError if another slot owns it."""
        path = self._pidfile_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+", encoding="utf-8")
        try:
            import fcntl  # POSIX only; Windows falls through to "no lock".

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    fh.seek(0)
                    holder = fh.read().strip() or "?"
                    fh.close()
                    raise RuntimeError(
                        f"another worker-slot already holds {path} (pid {holder})"
                    ) from exc
                raise
        except ImportError:  # pragma: no cover - Windows
            pass
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._pidfile_fh = fh
        return path

    def release_pidfile(self) -> None:
        fh = self._pidfile_fh
        self._pidfile_fh = None
        if fh is None:
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        finally:
            fh.close()

    # -- queue ------------------------------------------------------------

    def _next_ready(self, conn):
        """First spawnable ``ready`` card, same ordering + filters as dispatch_once."""
        rows = conn.execute(
            "SELECT id, assignee FROM tasks "
            "WHERE status = 'ready' AND claim_lock IS NULL "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        profile_exists = None
        if self.require_real_profile:
            try:
                from hermes_cli.profiles import profile_exists as _pe

                profile_exists = _pe
            except Exception:  # pragma: no cover - partial install
                profile_exists = None
        for row in rows:
            if not row["assignee"]:
                continue
            if profile_exists is not None and not profile_exists(row["assignee"]):
                continue
            return row
        return None

    # -- one pass ---------------------------------------------------------

    def run_once(self, conn=None) -> SlotTick:
        """Claim + execute at most one card. Returns an idle tick when there is none."""
        if not _SLOT_RUN_LOCK.acquire(blocking=False):
            raise RuntimeError(
                "worker-slot is already executing a card in this process; "
                "concurrency is capped at 1 by design"
            )
        own_conn = conn is None
        try:
            if own_conn:
                conn = self.connect()
            # Promote parent-completed cards first — a resumed card may be
            # the parent of a todo card that becomes ready this tick.
            kb.recompute_ready(conn)

            # RESUME BEFORE ANY RECLAIM. adopt_running_task keeps the run
            # row; it must run before anything could flip the card back to
            # 'ready' (which is what dispatch_once's release_stale_claims
            # would do — the slot never calls it).
            adoptable = kb.resumable_running_tasks(conn)
            task = None
            resumed = False
            for task_id in adoptable:
                adopted = kb.adopt_running_task(
                    conn, task_id,
                    ttl_seconds=self.ttl_seconds,
                    claimer=self.claimer,
                    worker_pid=os.getpid(),
                )
                if adopted is not None:
                    task = adopted
                    resumed = True
                    logger.info(
                        "kanban worker-slot: resuming %s (run %s)",
                        adopted.id, adopted.current_run_id,
                    )
                    break

            if task is None:
                row = self._next_ready(conn)
                if row is None:
                    return SlotTick(idle=True, adopted=tuple(adoptable))
                task = kb.claim_task(
                    conn, row["id"],
                    ttl_seconds=self.ttl_seconds,
                    claimer=self.claimer,
                )
                if task is None:
                    return SlotTick(idle=True, adopted=tuple(adoptable))

            tick = self._run_task(conn, task, resumed=resumed)
            tick.adopted = tuple(adoptable)
            return tick
        finally:
            if own_conn and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            _SLOT_RUN_LOCK.release()

    def _run_task(self, conn, task: kb.Task, *, resumed: bool) -> SlotTick:
        tick = SlotTick(idle=False, task_id=task.id, resumed=resumed)
        try:
            workspace = kb.resolve_workspace(task, board=self.board)
        except Exception as exc:
            kb.block_task(
                conn, task.id,
                reason=f"workspace-resolution-failed: {exc}",
            )
            tick.outcome = "blocked"
            tick.error = str(exc)
            return tick
        kb.set_workspace_path(conn, task.id, str(workspace))
        kb._set_worker_pid(conn, task.id, os.getpid())

        result: Optional[ExecutorResult] = None
        error: Optional[BaseException] = None
        try:
            result = self.executor(task, Path(workspace), conn, resumed=resumed)
        except Exception as exc:  # noqa: BLE001 - the slot must survive one bad card
            error = exc
            logger.exception("kanban worker-slot: executor failed on %s", task.id)

        # The executor (or the agent's own kanban tools) usually finalises
        # the card. Only act when it is *still* running.
        fresh = kb.get_task(conn, task.id)
        if fresh is None or fresh.status != "running":
            tick.outcome = fresh.status if fresh is not None else "vanished"
            return tick

        if error is not None:
            kb.block_task(
                conn, task.id,
                reason=f"slot-executor-error: {type(error).__name__}: {error}"[:500],
            )
            tick.outcome = "blocked"
            tick.error = str(error)
            return tick

        if result is None:
            kb.block_task(
                conn, task.id,
                reason=(
                    "protocol-violation: executor returned without completing or "
                    "blocking the card"
                ),
            )
            tick.outcome = "blocked"
            return tick

        if result.outcome == "blocked":
            kb.block_task(conn, task.id, reason=(result.reason or "blocked by worker-slot")[:500])
            tick.outcome = "blocked"
            return tick

        try:
            kb.complete_task(
                conn, task.id,
                summary=result.summary or None,
                metadata=result.metadata or None,
            )
            tick.outcome = "completed"
        except Exception as exc:  # noqa: BLE001 - e.g. QA verdict guard
            kb.block_task(
                conn, task.id,
                reason=f"slot-complete-rejected: {type(exc).__name__}: {exc}"[:500],
            )
            tick.outcome = "blocked"
            tick.error = str(exc)
        return tick

    # -- loop -------------------------------------------------------------

    def serve(
        self,
        *,
        stop_event: Optional[threading.Event] = None,
        install_signal_handlers: bool = True,
        max_ticks: Optional[int] = None,
    ) -> int:
        """Drain the board until stopped. Returns the number of cards executed.

        SIGTERM/SIGINT set the stop event; the in-flight card finishes
        (it is not interrupted mid-task — killing a running agent would
        leave the card half-done and force a resume), then the loop exits
        cleanly and the PID file is removed.
        """
        stop = stop_event or threading.Event()
        if install_signal_handlers:
            self._install_signal_handlers(stop)
        self.acquire_pidfile()
        cards = 0
        ticks = 0
        try:
            logger.info(
                "kanban worker-slot: started pid=%d board=%s %s",
                os.getpid(),
                self.board or kb.get_current_board(),
                self.worker_mode_setting.describe() if self.worker_mode_setting else f"worker_mode={self.worker_mode}",
            )
            while not stop.is_set():
                tick = self.run_once()
                ticks += 1
                if tick.did_work:
                    cards += 1
                    continue
                if max_ticks is not None and ticks >= max_ticks:
                    break
                stop.wait(self.poll_interval)
        finally:
            self.release_pidfile()
            logger.info("kanban worker-slot: stopped after %d card(s)", cards)
        return cards

    @staticmethod
    def _install_signal_handlers(stop: threading.Event) -> None:
        def _handler(signum, _frame):
            logger.info("kanban worker-slot: signal %s received, finishing current card", signum)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
