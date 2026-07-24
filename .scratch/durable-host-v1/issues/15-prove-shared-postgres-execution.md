# 15 — Prove shared Postgres execution with lease epochs

**What to build:** When a real multi-worker deployment is approved, run the
same Host and RunHomeClient contract on Postgres with lease-epoch authority
fenced inside every mutation. Prove takeover under real process and
connection failures without changing local-tier semantics.

**Blocked by:** 09 — Reserve effect identity and surface unknown outcomes; 11 — Complete Panda durable-host adoption and remove product guards; 14 — Schedule pause answers and record source; explicit maintainer confirmation of a real multi-worker need.

**Status:** not-authorized — maintainer withdrew the multi-worker confirmation on 2026-07-24; local tier only. The Durable Host V1 program ends at ticket 14. Do not build shared Postgres execution unless the maintainer re-authorizes with a confirmed real multi-worker need.

- [ ] Claim, renewal, takeover, and every execution or command mutation use one monotonic fenced epoch
- [ ] Heartbeat stays independent of blocking node execution
- [ ] The full kill matrix proves stale workers cannot commit after takeover
- [ ] Unknown physical effects remain unknown even when stale writes are fenced
- [ ] Public Host, RunHomeClient, RunRef, BatchRef, watch, and lineage behavior match the SQLite tier
