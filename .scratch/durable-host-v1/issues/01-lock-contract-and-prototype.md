# 01 — Lock the Durable Host contract and publish the decision prototype

**What to build:** Turn the accepted durable-host amendment package into the
project's durable vocabulary and decisions, then publish one inspectable
prototype of the Panda-shaped end state. The prototype must let the maintainer
follow submission, kill and restart, cursor reconnection, Batch tolerance,
version refusal, unknown effect, rerun, and fork without relying on
implementation code.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] The accepted ADRs, intent specs, and domain vocabulary agree on Definition identity, RunRef, BatchRef, RunHomeClient, rerun, fork, Batch, recovery exhaustion, and unknown effect
- [ ] The prototype shows realistic before and after states for all required Run and Batch flows
- [ ] The prototype makes the Host versus RunHomeClient ownership seam and the no-app-less-submit rule unambiguous
- [ ] No runtime implementation begins, and the artifact is ready for explicit maintainer approval
