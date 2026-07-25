# 14 — Schedule pause answers and record command provenance

**What to build:** Let a caller schedule one typed answer for the current
PauseSlot and record an opaque source reference on commands for audit. A human
answer must void its timer, stale timers must not settle later pauses, and
ordinary reminders must remain outside Hypergraph.

**Blocked by:** 12 — Tune active-Run admission and delay start; 13 — Persist typed pause slots and settle answers.

**Status:** done — landed in commit cbd3ed8f

- [x] A scheduled answer persists with pause id, due time, and one typed value
- [x] Human-answer and timer races resolve by atomic commit order
- [x] An answered or replaced pause makes its scheduled answer inapplicable
- [x] Source reference is visible as audit data but never affects authentication or deduplication
- [x] No reminder, assignment, recurring schedule, or generic scheduled-command surface is introduced
