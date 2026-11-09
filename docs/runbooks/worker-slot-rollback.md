# Worker-Slot Rollback Runbook (`persistent` → `dispatcher`)

Scope: switch a Hermes kanban board back from the persistent worker slot
(`kanban.worker_mode: persistent`) to the classic gateway dispatcher
(`worker_mode: dispatcher` + `dispatch_in_gateway: true` + `max_spawn: 2`)
without losing card, run, comment or event rows.

Measured on the isolated `slot-test` board on 2026-11-09 — see
`evidence/step2/rollback.txt`: **29 s** end-to-end (t0 1794217859 → t1
1794217888), including one real dispatcher-spawned worker run of a card.

## Preconditions

* The slot runs as a separate process: `hermes kanban --board <slug> worker-slot`.
  Its PID file is `<board-dir>/worker-slot.pid`
  (`<root>/kanban/boards/<slug>/worker-slot.pid`; board `default`:
  `<root>/kanban/worker-slot.pid`).
* The slot claims with lock `slot:<pid>`; card rows keep their
  `current_run_id` while the slot is alive.
* Config is read **once at process start** (gateway boot and slot start), so a
  production rollback needs a gateway restart. Flipping YAML alone changes
  nothing until the next boot.

## Steps

### 1. Stop the slot (SIGTERM — the in-flight card finishes)

```bash
PIDFILE=~/.hermes/kanban/boards/<slug>/worker-slot.pid
kill -TERM "$(cat "$PIDFILE")"
# wait until the process is gone and the pidfile is empty
while ps -p "$(cat "$PIDFILE")" >/dev/null 2>&1; do sleep 1; done
cat "$PIDFILE"   # -> empty line: clean release
```

Expected log (slot stdout): `worker-slot: stopped after N card(s)`.
Do **not** use `kill -9` here: SIGTERM lets the current card finish, SIGKILL
forces a resume on the next start.

### 2. Flip the config

Edit the config the gateway boots with (`~/.hermes/profiles/<profile>/config.yaml`,
or `~/.hermes/config.yaml` for the default profile):

```yaml
kanban:
  worker_mode: dispatcher        # was: persistent
  dispatch_in_gateway: true
  max_spawn: 2
```

Verify:

```bash
python3 - <<'EOF'
import yaml, pathlib
k = yaml.safe_load(pathlib.Path("<config.yaml>").read_text()).get("kanban", {})
print(k.get("worker_mode"), k.get("dispatch_in_gateway"), k.get("max_spawn"))
EOF
# -> dispatcher True 2
```

### 3. Restart the gateway

```bash
hermes gateway restart
```

Expected log line (`hermes logs -g`, gateway log):

```
kanban dispatcher: worker_mode=dispatcher (source: config.yaml kanban.worker_mode)
kanban dispatcher: embedded in gateway (interval=60.0s)
```

If the log still says `idle (worker_mode=persistent)`, the restart did not pick
up the config — re-check which profile the gateway runs as.

### 4. Verify that the dispatcher pulls the next card

```bash
hermes kanban --board <slug> show <next-card-id>
# task_events: ('claimed', <ts>, '{"lock": "hermes:<gateway-pid>", "run_id": N}')
#              ('spawned', <ts>, '{"pid": <worker-pid>}')
```

No slot process may be running at this point — two consumers on one board
would double-claim.

## Expected output of the probe (2026-11-09, slot-test)

```
rollback t0: 1794217859
rollback t1: 1794217888
duration_s: 29
Spawned: 1
  - t_48fd0c9f  ->  coder-d  @ .../slot-test/workspaces/t_48fd0c9f
card3 runs: (3, 't_48fd0c9f', 'done', 1794217859, 1794217887, None)
```

## Test-board caveat (found during the probe)

The gateway's embedded dispatcher enumerates **every non-archived board** on
each tick (`gateway/run.py::_kanban_dispatcher_watcher` →
`list_boards(include_archived=False)` → one `dispatch_once` per board).
A freshly created test board is therefore *not* isolated from the production
gateway even though the gateway never had its DB path in its config — see
`evidence/step2/attemptA-finding-gateway-enumerates-test-board.txt`.

For probes on a live gateway without stopping it, mark the test board archived
in `board.json` (`"archived": true`). `list_boards(include_archived=False)`
then skips it, while explicit `--board <slug>` calls (slot, rollback dispatch)
still resolve its directory. In `worker_mode: persistent` the gateway watcher
returns early anyway, so both consumers cannot race.
