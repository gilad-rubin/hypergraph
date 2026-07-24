# 12 — Tune active-Run admission and delay start

**What to build:** Let operators tune local active-Run capacity while the
worker is live and let callers persist one-shot future work that becomes
claimable only when store-authoritative time reaches its requested start.

**Blocked by:** 04 — Stop, restart, and exhaust recovery safely; 05 — Submit and watch an immutable durable Batch.

**Status:** ready-for-agent

- [ ] The active-Run cap changes at runtime and over-limit work waits in claim order
- [ ] Delayed, paused, incompatible, and recovery-exhausted Runs consume no active slot
- [ ] A claimed Run waiting on an injected provider limiter still consumes its Host slot without becoming failed or spending a retry attempt; provider limiters live at graph, node, or component scope with honest scope names, and a provider-quota component acquires at the exact scarce call
- [ ] Future start time persists and fingerprints at submission and past time becomes immediately eligible
- [ ] Stop-before-due prevents execution while reject, cancel, keyed, and expression strategies remain absent
