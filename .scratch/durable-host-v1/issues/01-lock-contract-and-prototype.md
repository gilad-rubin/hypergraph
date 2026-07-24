# 01 — Lock the Durable Host contract and publish the decision prototype

**What to build:** Turn the accepted durable-host amendment package into the
project's durable vocabulary and decisions, then publish one inspectable
prototype of the Panda-shaped end state. The prototype must let the maintainer
follow submission, kill and restart, cursor reconnection, Batch tolerance,
version refusal, unknown effect, rerun, and fork without relying on
implementation code.

**Blocked by:** None — can start immediately.

**Status:** awaiting-maintainer-approval — prototype published at
`docs/prd/durable-host-v1-decision-prototype.md`; ticket 02 stays
blocked until the maintainer explicitly approves it.

- [x] The accepted ADRs, intent specs, and domain vocabulary agree on Definition identity, RunRef, BatchRef, RunHomeClient, rerun, fork, Batch, recovery exhaustion, and unknown effect
- [x] The prototype shows realistic before and after states for all required Run and Batch flows
- [x] The prototype makes the Host versus RunHomeClient ownership seam and the no-app-less-submit rule unambiguous
- [x] No runtime implementation begins, and the artifact is ready for explicit maintainer approval
