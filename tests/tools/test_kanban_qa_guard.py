"""Tests for the QA verdict-binding gate (§2.4 / D-051).

``kanban_complete`` must mechanically reject a ``qa`` task whose
``metadata`` does not carry both binding fields as 40-hex commit SHAs:

    reviewed_commit  — the commit the verdict was issued against
    head_at_finish   — the branch head when the QA run ended

A rejection is **not terminal**: no task state is written, so the worker
can retry with a complete handoff (or block with ``escalation-required:``
if it cannot bind a SHA). ``force=True`` is a CLI-only human/CEO escape
hatch that demands a ``force_reason`` and writes a
``completion_override`` event.

Covered here:
  * qa + ``metadata={}`` → blocked, still ``running``, event recorded
  * qa + only ``reviewed_commit`` (partial binding) → blocked
  * qa + both 40-hex → completes, metadata persisted on the run
  * qa + 39/41 chars or non-hex → blocked, field listed as invalid
  * qa + uppercase hex → accepted
  * non-qa + ``metadata={}`` → unchanged behaviour
  * ``force=True`` without ``force_reason`` → ValueError
  * ``force=True`` with reason → completes + ``completion_override``
  * tool path (``_handle_complete``) → tool_error, task still running
  * CLI path (``_cmd_complete``) → exit 1, task still running, retry OK
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_cli import kanban_db as kb

QA_SHA = "0123456789abcdef0123456789abcdef01234567"
QA_SHA_HEAD = "89abcdef0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def board(monkeypatch, tmp_path):
    """Isolated DB via HERMES_KANBAN_DB; returns a factory for tasks."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    def make(assignee: str, status: str = "running") -> str:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title=f"task for {assignee}", assignee=assignee)
            if status == "running":
                kb.claim_task(conn, tid, claimer="coder-a")
        finally:
            conn.close()
        return tid

    yield make
    kb._INITIALIZED_PATHS.clear()


def _events(tid: str) -> list:
    conn = kb.connect()
    try:
        return kb.list_events(conn, tid)
    finally:
        conn.close()


def _kinds(tid: str) -> list[str]:
    return [e.kind for e in _events(tid)]


def _payload(tid: str, kind: str) -> dict:
    for ev in _events(tid):
        if ev.kind == kind:
            raw = ev.payload
            if not raw:
                return {}
            return raw if isinstance(raw, dict) else json.loads(raw)
    raise AssertionError(f"event {kind!r} not found in {_kinds(tid)}")


def _status(tid: str) -> str:
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task is not None
        return task.status
    finally:
        conn.close()


def _complete(tid: str, **kwargs) -> bool:
    conn = kb.connect()
    try:
        return kb.complete_task(conn, tid, **kwargs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------

def test_qa_empty_metadata_is_rejected_and_task_stays_running(board):
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError) as exc:
        _complete(tid, summary="x", metadata={})

    err = exc.value
    assert err.completing_task_id == tid
    assert set(err.missing) == {"reviewed_commit", "head_at_finish"}
    assert err.invalid == []
    assert _status(tid) == "running"
    assert "completion_blocked_unbound_verdict" in _kinds(tid)

    payload = _payload(tid, "completion_blocked_unbound_verdict")
    assert payload["task_id"] == tid
    assert set(payload["missing"]) == {"reviewed_commit", "head_at_finish"}
    assert payload["summary_preview"] == "x"


def test_qa_missing_metadata_argument_is_rejected(board):
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError):
        _complete(tid, summary="verdict with no metadata at all")
    assert _status(tid) == "running"


def test_qa_partial_binding_is_not_a_verdict(board):
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError) as exc:
        _complete(
            tid,
            summary="partial",
            metadata={"reviewed_commit": QA_SHA},
        )
    assert exc.value.missing == ["head_at_finish"]
    assert _status(tid) == "running"


@pytest.mark.parametrize(
    "bad_value",
    [
        QA_SHA[:39],          # 39 chars
        QA_SHA + "0",         # 41 chars
        "z" * 40,             # right length, not hex
        "HEAD",               # symbolic ref
        "refs/heads/main",    # branch name
    ],
)
def test_qa_malformed_sha_is_invalid(board, bad_value):
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError) as exc:
        _complete(
            tid,
            summary="malformed",
            metadata={"reviewed_commit": bad_value, "head_at_finish": QA_SHA_HEAD},
        )
    assert exc.value.invalid == ["reviewed_commit"]
    assert _status(tid) == "running"
    payload = _payload(tid, "completion_blocked_unbound_verdict")
    assert payload["invalid"] == ["reviewed_commit"]


def test_qa_non_string_sha_is_invalid(board):
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError) as exc:
        _complete(
            tid,
            summary="int sha",
            metadata={"reviewed_commit": 12345, "head_at_finish": QA_SHA_HEAD},
        )
    assert exc.value.invalid == ["reviewed_commit"]


# ---------------------------------------------------------------------------
# Accepting paths
# ---------------------------------------------------------------------------

def test_qa_with_both_shas_completes(board):
    tid = board("qa")
    metadata = {
        "verdict": "PASS",
        "reviewed_commit": QA_SHA,
        "head_at_finish": QA_SHA_HEAD,
    }
    assert _complete(tid, summary="QA PASS", metadata=metadata) is True
    assert _status(tid) == "done"

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    assert run is not None
    assert run.outcome == "completed"
    assert run.metadata is not None
    assert run.metadata["reviewed_commit"] == QA_SHA
    assert run.metadata["head_at_finish"] == QA_SHA_HEAD
    assert "completion_blocked_unbound_verdict" not in _kinds(tid)


def test_qa_uppercase_hex_is_accepted(board):
    tid = board("qa")
    assert _complete(
        tid,
        summary="upper",
        metadata={
            "reviewed_commit": QA_SHA.upper(),
            "head_at_finish": QA_SHA_HEAD.upper(),
        },
    ) is True
    assert _status(tid) == "done"


def test_qa_sha_with_surrounding_whitespace_is_accepted(board):
    tid = board("qa")
    assert _complete(
        tid,
        summary="padded",
        metadata={
            "reviewed_commit": f"  {QA_SHA}\n",
            "head_at_finish": f"\t{QA_SHA_HEAD} ",
        },
    ) is True


def test_retry_after_rejection_with_full_metadata_succeeds(board):
    """AC2: rejection is not terminal — the retry lands."""
    tid = board("qa")
    with pytest.raises(kb.UnboundVerdictError):
        _complete(tid, summary="too early", metadata={})
    assert _status(tid) == "running"

    assert _complete(
        tid,
        summary="now bound",
        metadata={
            "reviewed_commit": QA_SHA,
            "head_at_finish": QA_SHA_HEAD,
            "verdict": "PASS",
        },
    ) is True
    assert _status(tid) == "done"


# ---------------------------------------------------------------------------
# Non-qa tasks keep their old behaviour (AC4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("assignee", ["coder-a", "ceo", "hr", None])
def test_non_qa_completion_is_unchanged(board, assignee):
    tid = board(assignee)
    assert _complete(tid, summary="shipped", metadata={}) is True
    assert _status(tid) == "done"
    assert "completion_blocked_unbound_verdict" not in _kinds(tid)


# ---------------------------------------------------------------------------
# Force override
# ---------------------------------------------------------------------------

def test_force_without_reason_raises(board):
    tid = board("qa")
    with pytest.raises(ValueError) as exc:
        _complete(tid, summary="forced", metadata={}, force=True)
    assert "force_reason" in str(exc.value)
    assert _status(tid) == "running"
    assert "completion_override" not in _kinds(tid)


def test_force_with_blank_reason_raises(board):
    tid = board("qa")
    with pytest.raises(ValueError):
        _complete(tid, summary="forced", metadata={}, force=True, force_reason="   ")
    assert _status(tid) == "running"


def test_force_with_reason_completes_and_audits(board):
    tid = board("qa")
    assert _complete(
        tid,
        summary="forced close",
        metadata={},
        force=True,
        force_reason="board triage: QA worker died, CEO verified manually",
    ) is True
    assert _status(tid) == "done"

    payload = _payload(tid, "completion_override")
    assert payload["task_id"] == tid
    assert payload["assignee"] == "qa"
    assert "board triage" in payload["reason"]
    assert set(payload["missing"]) == {"reviewed_commit", "head_at_finish"}


# ---------------------------------------------------------------------------
# Tool path (AC1)
# ---------------------------------------------------------------------------

def test_tool_path_rejects_unbound_qa_verdict(board, monkeypatch):
    from tools import kanban_tools

    tid = board("qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    raw = kanban_tools._handle_complete(
        {"summary": "QA done", "metadata": {}}
    )
    payload = json.loads(raw)
    assert "error" in payload
    err = payload["error"]
    assert "blocked" in err
    assert "reviewed_commit" in err
    assert "head_at_finish" in err
    assert "still in-flight" in err
    assert "escalation-required" in err
    assert _status(tid) == "running"

    # And the same tool accepts the bound handoff.
    raw_ok = kanban_tools._handle_complete(
        {
            "summary": "QA done",
            "metadata": {
                "reviewed_commit": QA_SHA,
                "head_at_finish": QA_SHA_HEAD,
            },
        }
    )
    assert json.loads(raw_ok)["ok"] is True
    assert _status(tid) == "done"


def test_tool_schema_has_no_force_argument():
    """The agent-facing tool must never be able to bypass the gate."""
    from tools.kanban_tools import KANBAN_COMPLETE_SCHEMA

    props = KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert "force" not in props
    assert "force_reason" not in props


# ---------------------------------------------------------------------------
# CLI path (AC1 / AC3)
# ---------------------------------------------------------------------------

def _cli_args(tid: str, **overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        task_ids=[tid],
        result=None,
        summary="cli completion",
        metadata="{}",
        force=False,
        force_reason=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_cli_path_rejects_unbound_qa_verdict(board, capsys):
    from hermes_cli import kanban as kanban_cli

    tid = board("qa")
    rc = kanban_cli._cmd_complete(_cli_args(tid))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not bound to a commit" in err
    assert "still in-flight" in err
    assert _status(tid) == "running"

    # Retry with a bound verdict through the CLI succeeds.
    rc = kanban_cli._cmd_complete(
        _cli_args(
            tid,
            metadata=json.dumps(
                {"reviewed_commit": QA_SHA, "head_at_finish": QA_SHA_HEAD}
            ),
        )
    )
    assert rc == 0
    assert _status(tid) == "done"


def test_cli_force_without_reason_exits_2(board, capsys):
    from hermes_cli import kanban as kanban_cli

    tid = board("qa")
    rc = kanban_cli._cmd_complete(_cli_args(tid, force=True))
    err = capsys.readouterr().err
    assert rc == 2
    assert "--force-reason" in err
    assert _status(tid) == "running"


def test_cli_force_with_reason_completes(board, capsys):
    from hermes_cli import kanban as kanban_cli

    tid = board("qa")
    rc = kanban_cli._cmd_complete(
        _cli_args(tid, force=True, force_reason="manual CEO decision, see #1234")
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "completion_override" in captured.err
    assert _status(tid) == "done"
    assert "completion_override" in _kinds(tid)


def test_cli_force_flag_is_parsed():
    from hermes_cli import kanban as kanban_cli

    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    ns = root.parse_args(
        ["kanban", "complete", "t_abc123", "--force", "--force-reason", "ceo decision"]
    )
    assert ns.force is True
    assert ns.force_reason == "ceo decision"

    ns_plain = root.parse_args(["kanban", "complete", "t_abc123"])
    assert ns_plain.force is False
    assert ns_plain.force_reason is None


# ---------------------------------------------------------------------------
# Dashboard path: PATCH /tasks/{id} (plugins/kanban/dashboard/plugin_api.py)
#
# Rework #1 (QA-FAIL t_f9cc8179, MAJOR): the endpoint did not catch
# UnboundVerdictError, so the exception escaped the handler -> HTTP 500
# plus traceback instead of a clean 4xx with the guard text. It must be a
# 400, the task must stay `running` (rejection is not terminal), and
# non-qa cards must behave exactly as before.
# ---------------------------------------------------------------------------

def _patch_task(tid: str, **fields):
    """Call the dashboard ``PATCH /tasks/{id}`` handler in-process.

    The FastAPI ``TestClient`` harness is unusable in this environment
    (starlette 0.27 vs httpx 0.28), and the endpoint body is a plain
    sync function anyway: calling it directly exercises exactly the
    code path that used to leak ``UnboundVerdictError`` as an
    unhandled 500.
    """
    from plugins.kanban.dashboard import plugin_api as pa

    try:
        return pa.update_task(tid, pa.UpdateTaskBody(**fields), None)
    except HTTPException as exc:
        return exc


def test_dashboard_update_task_unbound_verdict_is_400_not_500(board):
    tid = board("qa")
    result = _patch_task(tid, status="done", summary="verdict without a binding")

    assert isinstance(result, HTTPException), result
    assert result.status_code == 400, result.detail
    detail = result.detail
    assert set(detail["missing"]) == {"reviewed_commit", "head_at_finish"}
    assert detail["invalid"] == []
    assert detail["summary_preview"] == "verdict without a binding"
    assert "completion blocked" in detail["message"]

    # Rejection is not terminal — no state change, retry stays possible.
    assert _status(tid) == "running"
    assert "completion_blocked_unbound_verdict" in _kinds(tid)


def test_dashboard_update_task_malformed_sha_is_400(board):
    tid = board("qa")
    result = _patch_task(
        tid,
        status="done",
        summary="bad sha",
        metadata={"reviewed_commit": "HEAD", "head_at_finish": QA_SHA_HEAD},
    )
    assert result.status_code == 400, result.detail
    assert result.detail["invalid"] == ["reviewed_commit"]
    assert _status(tid) == "running"


def test_dashboard_update_task_accepts_bound_verdict(board):
    tid = board("qa")
    result = _patch_task(
        tid,
        status="done",
        summary="QA PASS",
        metadata={
            "verdict": "PASS",
            "reviewed_commit": QA_SHA,
            "head_at_finish": QA_SHA_HEAD,
        },
    )
    assert not hasattr(result, "status_code"), result
    assert result["task"]["status"] == "done"
    assert _status(tid) == "done"


def test_dashboard_update_task_non_qa_unchanged(board):
    tid = board("coder-a")
    result = _patch_task(tid, status="done", summary="coder handoff")
    assert not hasattr(result, "status_code"), result
    assert result["task"]["status"] == "done"
    assert _status(tid) == "done"


# ---------------------------------------------------------------------------
# Forced-completion audit event is written atomically with the completion
# (QA minor finding: the override event used to be written up-front, so a
# subsequent completion failure left an orphan event behind).
# ---------------------------------------------------------------------------

def test_force_override_event_is_not_orphaned_by_failed_completion(board):
    tid = board("qa")
    # First completion (properly bound) makes the task terminal.
    assert _complete(
        tid,
        summary="first",
        metadata={"reviewed_commit": QA_SHA, "head_at_finish": QA_SHA_HEAD},
    ) is True
    assert _status(tid) == "done"

    # A forced completion on the now-terminal task must fail cleanly and
    # must NOT leave a completion_override event without a completion.
    assert _complete(
        tid,
        summary="forced again",
        metadata={},
        force=True,
        force_reason="audit ordering check",
    ) is False
    assert "completion_override" not in _kinds(tid)
