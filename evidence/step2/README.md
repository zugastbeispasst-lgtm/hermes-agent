# WS2 live-probe evidence (`slot-test`, 2026-11-09)

Card: `t_9966d66e` — AC3 (RAM), AC4 (SIGKILL resume), AC5 (rollback), AC6 (integrity).
Code under test: WS1 branch `feat/persistent-worker-slot`.

## Headline numbers

| AC | Value | File |
|---|---|---|
| AC3 | peak slot `VmHWM` 152 552 kB → 163 936 kB (145–160 MiB) over 3 cards; **below** the 0.4–0.6 GB target band → reported as finding | `free-00..03-*.txt` |
| AC4 | `SIGKILL` mid card 2 → same `run_id=2`, `started_at=1794217781` unchanged, `resumed` event, pre-kill comment quoted verbatim by the resumed worker | `card2-run-*.txt`, `card2-resume-proof.txt` |
| AC5 | rollback executed: 1794217859 → 1794217888 = **29 s** (< 300 s), dispatcher spawned card 3 | `rollback*.txt` |
| AC6 | 1 run per test card; prod board 278/270/316/1713 rows + sha256 identical before/after | `slot-test-final-state.txt`, `prod-board-counts-*.txt` |

## File index

* `board-create.txt`, `board-visibility.txt` — board creation + proof the gateway
  does not enumerate it once `board.json` is archived.
* `card1-create.json` … `card3-create.json` — card definitions.
* `free-*.txt` — `free -m` and `/proc/<slot-pid>/status VmHWM` snapshots.
* `slot-run.log`, `slot-resume.log`, `watch-*.log` — slot lifecycle + probe driver output.
* `card2-run-before-kill.txt` / `card2-run-after-kill.txt` — same run row, `ended_at` NULL,
  task still `running` after the kill.
* `card2-resume-proof.txt` — one run row, `resumed` event, both comments.
* `rollback.txt`, `rollback-config.txt`, `rollback-before-dispatch.txt`,
  `rollback-dispatch.txt`, `rollback-after.txt` — AC5 execution.
* `slot-test-final-state.txt` — final tasks/runs/comments/events of the test board.
* `prod-board-counts-before.txt` / `-after.txt` — production board untouched.
* `summary.json` — machine-readable summary (values + findings).
* `attemptA-*` — first, aborted attempt: it exposed finding F1 (the gateway claims
  cards on **any** non-archived board). Not used for the numbers above.

## Findings

1. **F1 (high)** — the gateway dispatcher enumerates all non-archived boards each
   tick, so a new test board is *not* isolated by "the gateway doesn't know its path".
   Probe mitigation: `"archived": true` in `board.json`. Details:
   `attemptA-finding-gateway-enumerates-test-board.txt`.
2. **F2 (info)** — the 0.4–0.6 GB RAM expectation is not reproduced: a single
   in-process worker peaks at ~150–160 MiB.

## Cleanup

The three probe cards were archived and the `slot-test` board directory was moved
to `~/.hermes/kanban/boards/_archived/` after the run (see handoff comment).
