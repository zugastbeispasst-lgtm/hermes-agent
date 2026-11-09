"""Tests for the persistent kanban worker slot (hermes_cli.kanban_slot).

Covers the WS1 acceptance criteria owned by this card:

* AC1 — ``kanban.worker_mode`` is read at boot, defaults to ``dispatcher``,
  warns + falls back on an unknown value, and the gateway dispatcher logs
  "idle (worker_mode=persistent)" instead of spawning.
* AC2 — the slot runs exactly one card at a time: three sequential test
  cards produce three runs with non-overlapping ``started_at``/``ended_at``,
  no ``crashed`` outcome, and no extra runs.
* AC8 — no board schema change (``PRAGMA user_version`` + table list match
  the frozen base fingerprint measured at f384a9fe).

Plus the resume contract that makes the slot different from the dispatcher:
an orphaned ``running`` card is adopted with the *same* ``run_id`` rather
than re-queued and restarted.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_slot as ks


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK",
        "HERMES_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


def _make_slot(executor, **kwargs) -> ks.WorkerSlot:
    return ks.WorkerSlot(
        executor=executor,
        worker_mode=ks.WORKER_MODE_PERSISTENT,
        require_real_profile=False,
        poll_interval=0.01,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC1 — kanban.worker_mode
# ---------------------------------------------------------------------------

def test_worker_mode_absent_defaults_to_dispatcher():
    setting = ks.parse_worker_mode({})
    assert setting.mode == ks.WORKER_MODE_DISPATCHER
    assert setting.is_persistent is False
    assert "not set" in setting.source


def test_worker_mode_absent_from_non_mapping_config_defaults_to_dispatcher():
    setting = ks.parse_worker_mode(None)
    assert setting.mode == ks.WORKER_MODE_DISPATCHER


def test_worker_mode_persistent_from_kanban_config():
    setting = ks.parse_worker_mode({"worker_mode": "persistent"})
    assert setting.mode == ks.WORKER_MODE_PERSISTENT
    assert setting.is_persistent is True
    assert "kanban.worker_mode" in setting.source


def test_worker_mode_is_case_and_whitespace_insensitive():
    assert ks.parse_worker_mode({"worker_mode": "  Persistent "}).mode == "persistent"


def test_worker_mode_unknown_value_warns_and_falls_back(caplog):
    with caplog.at_level(logging.WARNING, logger=ks.logger.name):
        setting = ks.parse_worker_mode({"worker_mode": "garbage"})
    assert setting.mode == ks.WORKER_MODE_DISPATCHER
    assert "fallback" in setting.source
    assert setting.raw == "garbage"
    assert any("falling back" in rec.message for rec in caplog.records)


def test_worker_mode_non_string_warns_and_falls_back(caplog):
    with caplog.at_level(logging.WARNING, logger=ks.logger.name):
        setting = ks.parse_worker_mode({"worker_mode": True})
    assert setting.mode == ks.WORKER_MODE_DISPATCHER
    assert any("not a string" in rec.message for rec in caplog.records)


def test_read_worker_mode_uses_config_mapping():
    setting = ks.read_worker_mode({"kanban": {"worker_mode": "persistent"}})
    assert setting.mode == ks.WORKER_MODE_PERSISTENT


def test_read_worker_mode_defaults_when_key_missing():
    setting = ks.read_worker_mode({"kanban": {}})
    assert setting.mode == ks.WORKER_MODE_DISPATCHER


def test_worker_slot_rejects_unknown_worker_mode():
    with pytest.raises(ValueError):
        ks.WorkerSlot(worker_mode="garbage")


def test_worker_slot_help_lists_subcommand(capsys):
    outer = argparse.ArgumentParser()
    sub = outer.add_subparsers(dest="command")
    kc.build_parser(sub)
    with pytest.raises(SystemExit) as exc:
        outer.parse_args(["kanban", "worker-slot", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--once" in out
    assert "--poll-interval" in out


def test_worker_slot_parser_wires_action_and_flags():
    outer = argparse.ArgumentParser()
    sub = outer.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = outer.parse_args(["kanban", "worker-slot", "--dry-run", "--ttl", "42"])
    assert args.kanban_action == "worker-slot"
    assert args.dry_run is True
    assert args.ttl == 42


def test_cli_refuses_to_start_in_dispatcher_mode(kanban_home, capsys):
    # No kanban.worker_mode in config → dispatcher → refuse without --force.
    rc = kc.kanban_command(
        argparse.Namespace(kanban_action="worker-slot", once=True, dry_run=True,
                           ttl=900, poll_interval=1.0, force=False, json=True,
                           max_ticks=None, board=None)
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "worker_mode" in err
    assert "persistent" in err


def test_cli_dry_run_reports_mode_source(kanban_home, capsys, monkeypatch):
    monkeypatch.setattr(
        ks, "read_worker_mode",
        lambda config=None: ks.WorkerModeSetting(
            mode=ks.WORKER_MODE_PERSISTENT,
            source="config.yaml kanban.worker_mode",
        ),
    )
    rc = kc.kanban_command(
        argparse.Namespace(kanban_action="worker-slot", once=False, dry_run=True,
                           ttl=900, poll_interval=1.0, force=False, json=True,
                           max_ticks=None, board=None)
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "worker_mode=persistent" in captured.err
    assert "config.yaml kanban.worker_mode" in captured.err


def test_gateway_dispatcher_idles_in_persistent_mode(monkeypatch, caplog):
    """The embedded dispatcher must spawn nothing when the slot owns the queue."""
    import asyncio

    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_mode": "persistent"}},
    )

    class _Dummy:
        _running = True

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        # If the early return is missing, this coroutine blocks in
        # asyncio.sleep(5) and the wait_for() below times out — which is
        # exactly the failure we want to surface.
        asyncio.run(asyncio.wait_for(
            GatewayRunner._kanban_dispatcher_watcher(_Dummy()), timeout=2.0,
        ))

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("idle (worker_mode=persistent)" in m for m in messages)


def test_gateway_dispatcher_still_spawns_in_dispatcher_mode(monkeypatch, caplog):
    import asyncio

    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_mode": "dispatcher", "dispatch_in_gateway": False}},
    )

    class _Dummy:
        _running = True

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(asyncio.wait_for(
            GatewayRunner._kanban_dispatcher_watcher(_Dummy()), timeout=2.0,
        ))

    messages = [rec.getMessage() for rec in caplog.records]
    assert not any("idle (worker_mode=persistent)" in m for m in messages)
    assert any("dispatch_in_gateway=false" in m for m in messages)


# ---------------------------------------------------------------------------
# AC2 — one card at a time, one run per card
# ---------------------------------------------------------------------------

def test_slot_runs_three_cards_sequentially(kanban_home):
    with kb.connect() as conn:
        task_ids = [
            kb.create_task(conn, title=f"card {i}", assignee="alice",
                           body=f"write file card{i}.txt and mark the card done")
            for i in range(3)
        ]

    started: list[float] = []
    ended: list[float] = []

    def executor(task, workspace, conn, *, resumed=False):
        assert resumed is False
        Path(workspace, f"{task.id}.txt").write_text(task.id, encoding="utf-8")
        started.append(time.time())
        time.sleep(2.0)  # AC2: stub executor with 2 s runtime
        kb.complete_task(conn, task.id, summary=f"card {task.id} done")
        ended.append(time.time())
        return None

    slot = _make_slot(executor)
    ticks = [slot.run_once() for _ in range(3)]
    idle = slot.run_once()

    assert [t.task_id for t in ticks] == task_ids
    assert all(t.outcome == "done" for t in ticks), [t.outcome for t in ticks]
    assert idle.idle is True

    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT task_id, started_at, ended_at, outcome FROM task_runs "
            "ORDER BY started_at, id"
        ).fetchall()
        assert len(rows) == 3, "one run per card, no retries"
        assert {r["task_id"] for r in rows} == set(task_ids)
        assert all(r["ended_at"] is not None for r in rows)
        assert all(r["outcome"] == "completed" for r in rows)
        assert not any(r["outcome"] == "crashed" for r in rows)
        for prev, nxt in zip(rows, rows[1:]):
            assert prev["ended_at"] <= nxt["started_at"], (
                f"runs overlap: {prev['task_id']} ended {prev['ended_at']} "
                f"after {nxt['task_id']} started {nxt['started_at']}"
            )
        tasks = [kb.get_task(conn, tid) for tid in task_ids]
        assert all(t.status == "done" for t in tasks)
        for tid in task_ids:
            assert _workspace_has(conn, tid, f"{tid}.txt")
    finally:
        conn.close()

    assert all(b > a for a, b in zip(started, ended))
    for prev_end, next_start in zip(ended, started[1:]):
        assert prev_end <= next_start


def _workspace_has(conn, task_id: str, name: str) -> bool:
    task = kb.get_task(conn, task_id)
    assert task is not None and task.workspace_path
    return (Path(task.workspace_path) / name).is_file()


def test_slot_reports_idle_on_empty_board(kanban_home):
    slot = _make_slot(lambda *a, **k: None)
    tick = slot.run_once()
    assert tick.idle is True
    assert tick.task_id is None


def test_slot_skips_unassigned_cards(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="nobody owns me")
    called = []
    slot = _make_slot(lambda *a, **k: called.append(1))
    assert slot.run_once().idle is True
    assert called == []


def test_slot_blocks_on_protocol_violation(kanban_home):
    """Executor that neither completes nor blocks must not leave the card running."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="silent executor", assignee="alice")

    slot = _make_slot(lambda *a, **k: None)
    tick = slot.run_once()
    assert tick.task_id == tid
    assert tick.outcome == "blocked"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
    events = kb.list_events(kb.connect(), tid)
    assert any("protocol-violation" in str(e.payload or {}) for e in events)


def test_slot_blocks_when_executor_raises(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="boom", assignee="alice")

    def executor(*a, **k):
        raise RuntimeError("model exploded")

    slot = _make_slot(executor)
    tick = slot.run_once()
    assert tick.outcome == "blocked"
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        assert runs[0].ended_at is not None


def test_slot_completes_when_executor_returns_result(kanban_home):
    """An executor that hands back a result lets the slot finalise the card."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="result only", assignee="alice")

    def executor(task, workspace, conn, *, resumed=False):
        return ks.ExecutorResult(outcome="completed", summary="did the thing",
                                 metadata={"tests_run": 1})

    slot = _make_slot(executor)
    tick = slot.run_once()
    assert tick.outcome == "completed"
    with kb.connect() as conn:
        run = kb.list_runs(conn, tid)[-1]
        assert run.outcome == "completed"
        assert run.summary == "did the thing"
        assert run.metadata == {"tests_run": 1}


# ---------------------------------------------------------------------------
# Resume instead of re-run
# ---------------------------------------------------------------------------

def _orphan_running_card(
    conn,
    title: str,
    *,
    claimer: str = "dead:424242",
    worker_pid: int | None = 999999,
    expired: bool = True,
) -> tuple[str, int]:
    """Create a card and leave it ``running`` with an open, orphaned run."""
    tid = kb.create_task(conn, title=title, assignee="alice", body="half-finished work")
    claimed = kb.claim_task(conn, tid, claimer=claimer)
    assert claimed is not None and claimed.current_run_id is not None
    if worker_pid is not None:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (worker_pid, tid))
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (worker_pid, int(claimed.current_run_id)),
        )
    if expired:
        past = int(time.time()) - 600
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (past, tid))
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (past, int(claimed.current_run_id)),
        )
    return tid, int(claimed.current_run_id)


def test_resume_keeps_run_id_and_run_count(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        tid, run_id = _orphan_running_card(conn, "orphaned card")
        kb.add_comment(conn, tid, author="alice", body="got as far as step 3")

    seen: dict = {}

    def executor(task, workspace, conn, *, resumed=False):
        seen["resumed"] = resumed
        seen["run_id"] = task.current_run_id
        seen["lock"] = task.claim_lock
        kb.complete_task(conn, task.id, summary="finished after resume")
        return None

    slot = _make_slot(executor)
    tick = slot.run_once()

    assert tick.resumed is True
    assert tick.task_id == tid
    assert seen["resumed"] is True
    assert seen["run_id"] == run_id, "resume must reuse the existing run row"
    assert seen["lock"] == slot.claimer

    conn = kb.connect()
    try:
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1, "resume must not create a second run"
        assert runs[0].id == run_id
        assert runs[0].outcome == "completed"
        assert runs[0].summary == "finished after resume"
        events = kb.list_events(conn, tid)
        resumed_events = [e for e in events if e.kind == "resumed"]
        assert len(resumed_events) == 1
        assert resumed_events[0].payload["run_id"] == run_id
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_resume_prompt_carries_task_context(kanban_home):
    with kb.connect() as conn:
        tid, _ = _orphan_running_card(conn, "context card")
        kb.add_comment(conn, tid, author="ceo", body="please read the spec first")
        task = kb.get_task(conn, tid)
        prompt = ks.build_slot_prompt(task, conn, resumed=True)

    assert f"resume kanban task {tid}" in prompt
    assert "RESUME, not a fresh start" in prompt
    assert "please read the spec first" in prompt
    assert "half-finished work" in prompt


def test_fresh_prompt_matches_dispatcher_shape(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="alice")
        task = kb.get_task(conn, tid)
        assert ks.build_slot_prompt(task, conn, resumed=False) == f"work kanban task {tid}"


def test_running_card_with_live_pid_is_not_stolen(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == 4242)
    with kb.connect() as conn:
        tid, _ = _orphan_running_card(conn, "still owned", worker_pid=4242)
        assert kb.resumable_running_tasks(conn) == []

    called = []
    slot = _make_slot(lambda *a, **k: called.append(1))
    tick = slot.run_once()
    assert tick.idle is True
    assert called == []


def test_expired_pidless_claim_is_resumable(kanban_home):
    with kb.connect() as conn:
        tid, _ = _orphan_running_card(conn, "operator claim", claimer="operator",
                                      worker_pid=None)
        assert kb.resumable_running_tasks(conn) == [tid]


def test_live_pidless_claim_is_left_alone(kanban_home):
    with kb.connect() as conn:
        _orphan_running_card(conn, "operator claim", claimer="operator",
                             worker_pid=None, expired=False)
        assert kb.resumable_running_tasks(conn) == []


def test_adopt_running_task_refuses_closed_run(kanban_home):
    with kb.connect() as conn:
        tid, run_id = _orphan_running_card(conn, "closed run")
        conn.execute(
            "UPDATE task_runs SET ended_at = ?, outcome = 'reclaimed' WHERE id = ?",
            (int(time.time()), run_id),
        )
        assert kb.adopt_running_task(conn, tid) is None


# ---------------------------------------------------------------------------
# Single-slot guarantees
# ---------------------------------------------------------------------------

def test_pidfile_lock_rejects_second_slot(kanban_home):
    first = _make_slot(lambda *a, **k: None)
    second = _make_slot(lambda *a, **k: None)
    first.acquire_pidfile()
    try:
        assert first._pidfile_path.read_text().strip().isdigit()
        with pytest.raises(RuntimeError, match="already holds"):
            second.acquire_pidfile()
    finally:
        first.release_pidfile()
    # Lease released → a new slot may take over.
    second.acquire_pidfile()
    second.release_pidfile()


def test_no_concurrent_execution_in_one_process(kanban_home):
    with kb.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        kb.create_task(conn, title="b", assignee="alice")

    nested: list[str] = []

    def executor(task, workspace, conn, *, resumed=False):
        # Re-entrant run_once must be refused, not interleaved.
        with pytest.raises(RuntimeError, match="concurrency is capped at 1"):
            slot.run_once()
        nested.append(task.id)
        kb.complete_task(conn, task.id, summary="ok")
        return None

    slot = _make_slot(executor)
    assert slot.run_once().outcome == "done"
    assert slot.run_once().outcome == "done"
    assert len(nested) == 2


# ---------------------------------------------------------------------------
# AC8 — no board schema change
# ---------------------------------------------------------------------------

# Measured on a fresh board DB at f384a9fe (the delivery base commit).
BASE_USER_VERSION = 0
BASE_TABLES = [
    "kanban_notify_subs",
    "sqlite_sequence",
    "task_comments",
    "task_events",
    "task_links",
    "task_runs",
    "tasks",
]


def test_schema_fingerprint_unchanged(kanban_home):
    with kb.connect() as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
    assert user_version == BASE_USER_VERSION
    assert tables == BASE_TABLES


def test_slot_adds_no_columns_to_tasks(kanban_home):
    with kb.connect() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    # Frozen at the base commit; the slot stores no schema-level state.
    assert "worker_mode" not in cols
    assert "slot_pid" not in cols
