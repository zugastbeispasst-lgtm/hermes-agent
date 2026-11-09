# Persistent worker slot (`kanban.worker_mode`)

Status: **implemented, opt-in, off by default**. This change ships the
mechanism only — flipping the productive `codeforge` profiles over to it
is a separate step (WS2 / live probe) and is **not** part of this PR.

## What it is

Today every card is executed by a freshly spawned `hermes -p <profile>
chat -q "work kanban task <id>"` subprocess, started from the gateway's
embedded dispatcher (`kanban_db.dispatch_once` → `_default_spawn`).

The persistent slot is an alternative consumer of the same queue:

| | dispatcher (default) | persistent worker slot |
|---|---|---|
| consumer | gateway watcher, one subprocess per card | one long-lived process |
| execution | `hermes` subprocess per card | in-process `AIAgent` (`hermes_cli.oneshot.run_prompt_inprocess`) |
| concurrency | `kanban.max_spawn` (2 on the codeforge board) | exactly 1 |
| crashed worker | claim reclaimed → card re-queued → **new run from zero** | claim adopted → **same run resumed** |
| startup cost per card | interpreter + config + credential pool | amortised across cards |

Both consumers must never run at the same time: each claim is an atomic
`ready → running` CAS, so a double consumer does not corrupt the board,
but it does spawn work behind a card that is already running. That is why
the mode is a config key and not a flag on either side.

## Configuration

```yaml
# ~/.hermes/config.yaml  (or ~/.hermes/profiles/<name>/config.yaml)
kanban:
  worker_mode: persistent   # default: dispatcher
```

Read **once at boot** by both consumers, so the flip needs a gateway
restart. Resolution rules (`hermes_cli/kanban_slot.py::parse_worker_mode`):

| value | effective mode | source string logged |
|---|---|---|
| key absent | `dispatcher` | `kanban.worker_mode not set (default)` |
| `dispatcher` | `dispatcher` | `config.yaml kanban.worker_mode` |
| `persistent` | `persistent` | `config.yaml kanban.worker_mode` |
| anything else (`garbage`, `true`, `"Persistent "` is fine, but `1` is not) | `dispatcher` + `WARNING` | `fallback (invalid kanban.worker_mode=...)` |

`persistent` makes the gateway watcher exit immediately after logging:

```
kanban dispatcher: idle (worker_mode=persistent) — the queue is owned by `hermes kanban worker-slot`
```

## Runbook

1. Set `kanban.worker_mode: persistent` in the config **of the profile
   whose gateway runs the dispatcher** (today: the `ceo`/`default`
   gateway, not the worker profiles).
2. Restart the gateway so the watcher picks the mode up:
   `hermes gateway restart`.
3. Start one slot per board:

   ```bash
   hermes kanban worker-slot --max-ticks 1        # smoke: one pass
   hermes kanban worker-slot --dry-run --json     # what would be claimed
   hermes kanban worker-slot                      # the real loop
   ```

4. Stop it with `SIGTERM` (or `systemctl stop`). The in-flight card is
   **not** interrupted: the loop finishes the current card, then exits
   and removes the PID file. Interrupting mid-card would leave a
   half-done card that has to be resumed anyway.

Example unit (not installed by this PR):

```ini
[Unit]
Description=Hermes kanban persistent worker slot
After=network-online.target

[Service]
Type=simple
Environment=HERMES_HOME=/home/adam/.hermes/profiles/coder-a
ExecStart=/home/adam/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main kanban worker-slot
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=3600

[Install]
WantedBy=multi-user.target
```

`Restart=on-failure` works because `hermes kanban worker-slot` now
propagates its exit code (2 = refused, see below) through
`cmd_kanban` instead of always exiting 0.

### Exit codes

| code | meaning |
|---|---|
| 0 | loop ended normally (SIGTERM) or `--once`/`--dry-run` succeeded |
| 1 | `--once` ran a card that ended `blocked` |
| 2 | refused to start: `kanban.worker_mode` is not `persistent` (unless `--force`) |

### One slot per board

`serve()` takes an exclusive `flock` on `<board>/worker-slot.pid`. A
second slot on the same board fails with
`RuntimeError: another worker-slot already holds … (pid N)` instead of
silently double-claiming. `run_once()` additionally refuses re-entrant
calls inside one process (`concurrency is capped at 1 by design`).

## Resume instead of re-run

`kanban_db.resumable_running_tasks(conn)` returns `running` cards whose
owner is gone:

* `worker_pid` recorded and **not alive** → orphaned (crashed, OOM-killed,
  machine rebooted), or
* no `worker_pid` (operator `hermes kanban claim`) and the lease expired.

A `running` card whose PID is alive is never touched — that is a live
gateway-spawned subprocess, and stealing it would double-run the card.

`kanban_db.adopt_running_task(conn, task_id)` then re-issues **only the
claim lease**: `current_run_id` stays, the open `task_runs` row keeps its
`started_at`, `summary` history and `metadata`, and a `resumed` event is
recorded. The slot then executes with

```python
build_slot_prompt(task, conn, resumed=True)
```

which includes the full `build_worker_context` rendering (body, prior
attempts, parent handoffs, comments) and an explicit "this is a RESUME,
not a fresh start" instruction. Bit-identical LLM history is **not**
reproduced — that is not the contract; the durable context in the board
is.

**Ordering matters.** `dispatch_once` calls `release_stale_claims()`
*before* claiming, which flips an expired `running` card back to `ready`
— i.e. it would turn the resume into exactly the re-run this feature
removes. The slot therefore never calls `dispatch_once` or
`release_stale_claims`; its tick is
`recompute_ready → resumable_running_tasks → adopt → claim next ready`.

The slot writes **its own PID** as `worker_pid`. That has two effects:
the existing live-PID protection in `release_stale_claims` extends the
lease while the slot lives, and the card becomes resumable the instant
the slot process dies.

## Process reuse, not transcript reuse

`default_executor()` builds a fresh `AIAgent` per card but does so
**inside the slot process**: same interpreter, same loaded config, same
credential pool, no `sys.executable` spawn. Conversation state is
deliberately not carried across cards — bleeding a finished task's
transcript into the next unrelated card is a correctness bug, not a
feature. Continuity comes from the board (`build_worker_context`).

If you need a different execution model (stub, remote runner, another
agent), pass `executor=` to `WorkerSlot`; the contract is
`executor(task, workspace, conn, *, resumed) -> ExecutorResult | None`.
If the executor returns without finalising the card, the slot blocks it
with `protocol-violation: …` rather than leaving it `running`.

## Scope boundaries

* `kanban.max_spawn` is unchanged (2 in production) and irrelevant in
  slot mode: the slot is single-threaded by construction.
* **`max_runtime_seconds` is not enforced in slot mode.** The old
  watchdog lives in `kanban_db.enforce_max_runtime()`, which only runs
  inside `dispatch_once`/the daemon — neither of which the slot calls.
  A hung executor therefore hangs the slot rather than being SIGTERMed
  and re-queued. In-process execution cannot be killed cleanly without
  abandoning a thread mid-conversation, so this is documented rather than
  papered over; the per-task iteration budget plus the model's own
  timeouts are the practical bound. Decide in WS2/step 3 whether the slot
  needs an external watchdog.
* No board schema change — see the AC8 fingerprint below.
* The live installation is not modified by this PR: no hot-patch, no
  config edit, no gateway restart.

---

# Verification evidence (WS1, card `t_4c4c0bbc`)

All commands were run with the repository venv
`/home/adam/.hermes/hermes-agent/venv/bin/python` (a git worktree has no
`venv/`; `venv/bin/python` exits 127 there).

## AC1 — flag exists, read at boot, default + fallback

```
$ HERMES_HOME=<empty> … -m hermes_cli.main kanban worker-slot --dry-run --json
kanban worker-slot: worker_mode=dispatcher (source: kanban.worker_mode not set (default))
kanban worker-slot: refusing to start — kanban.worker_mode is 'dispatcher'. …
exit=2

$ # config: kanban.worker_mode: persistent
kanban worker-slot: worker_mode=persistent (source: config.yaml kanban.worker_mode)
{ "worker_mode": "persistent", "worker_mode_source": "config.yaml kanban.worker_mode",
  "resumable": [], "next_ready": null }
exit=0

$ # config: kanban.worker_mode: garbage
kanban worker-slot: worker_mode=dispatcher (source: fallback (invalid kanban.worker_mode='garbage'))
kanban worker-slot: refusing to start — kanban.worker_mode is 'dispatcher'. …
exit=2
```

Asserted in `tests/hermes_cli/test_kanban_worker_slot.py`:
`test_gateway_dispatcher_idles_in_persistent_mode` drives the real
`GatewayRunner._kanban_dispatcher_watcher` with a persistent config and
asserts the `idle (worker_mode=persistent)` log + immediate return;
`test_gateway_dispatcher_still_spawns_in_dispatcher_mode` asserts the
opposite for `dispatcher`.

## AC2 — exactly one card at a time

`tests/hermes_cli/test_kanban_worker_slot.py::test_slot_runs_three_cards_sequentially`
creates three ready cards, runs them through `WorkerSlot.run_once()` with
a stub executor that writes a file, sleeps **2 s**, and completes the
card. It then asserts on SQL:

* `SELECT COUNT(*) FROM task_runs` == 3 (one run per card, no retries)
* every `ended_at` non-NULL and every `outcome = 'completed'`, none
  `'crashed'`
* for each consecutive pair, `ended_at(i) <= started_at(i+1)` — no
  overlapping runs
* the file written by the stub exists in the card's workspace

## AC7 — null regression against the red baseline

```
$ /home/adam/.hermes/hermes-agent/venv/bin/python -m pytest \
    tests/hermes_cli/test_kanban_db.py \
    tests/hermes_cli/test_kanban_core_functionality.py \
    tests/hermes_cli/test_kanban_cli.py \
    tests/tools/test_kanban_tools.py -q -p no:cacheprovider
14 failed, 323 passed, 2 warnings in 15.91s
```

The failing set is byte-identical to the baseline list (14 nodes, all in
`tests/tools/test_kanban_tools.py`); `diff` against the baseline node
list is empty. No baseline failure was healed and none was added.

## AC8 — no board schema change

`scripts/kanban_schema_fingerprint.py` opens a DB with
`?mode=ro` and prints `PRAGMA user_version`, the sorted table list, every
table's columns and a `schema_sha256` over `{user_version, tables,
columns}`.

```
python scripts/kanban_schema_fingerprint.py /tmp/kb_copy.db
```

| DB | user_version | tables | schema_sha256 |
|---|---|---|---|
| fresh DB at base `f384a9fe` | 0 | 7 (identical list) | `516dab648b2599c62aab37c54f6704f4ee7da756351c664aac2ef5b0a3747683` |
| fresh DB at PR head (this branch) | 0 | 7 (identical list) | `516dab648b2599c62aab37c54f6704f4ee7da756351c664aac2ef5b0a3747683` |
| copy of the production board DB | 0 | 7 (identical list) | `8d24c6ee716672b6…` |

`base == head` ⇒ this change adds no schema, no migration, no column.

**Known pre-existing divergence, not caused by this PR:** the
production DB copy has one extra column, `tasks.max_iterations`. It comes
from an **uncommitted** change in the live installation
(`git -C ~/.hermes/hermes-agent diff hermes_cli/kanban_db.py`), not from
this branch: the PR's `kanban_db.py` diff adds only
`adopt_running_task()` and `resumable_running_tasks()` and touches no
`CREATE TABLE` / `_migrate_add_optional_columns`. The two properties that
AC8 names — `PRAGMA user_version` and the table list — are identical.

## Test commands

```bash
# new suite (32 tests)
/home/adam/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/hermes_cli/test_kanban_worker_slot.py -q -p no:cacheprovider

# AC7 baseline
/home/adam/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/tools/test_kanban_tools.py -q -p no:cacheprovider
```
