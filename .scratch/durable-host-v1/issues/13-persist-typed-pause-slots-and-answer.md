# 13 — Persist typed pause slots and settle answers

**What to build:** Make each interrupt occurrence a durable typed PauseSlot
and let RunHomeClient answer the observed occurrence atomically. Rejected,
double, and stale answers must leave execution truth intact across restart.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** ready-for-agent

- [ ] PauseSlot persists the graph-derived answer schema, occurrence options, answer port, and unique pause id with the paused transition
- [ ] Human answer validates one typed value before atomic settlement
- [ ] Rejected values leave the current PauseSlot open
- [ ] Double and stale answers receive distinct truthful errors and answer-versus-stop races resolve by commit order
- [ ] Loop, nested graph, sync, async, Memory, and SQLite behavior remain consistent across restart
