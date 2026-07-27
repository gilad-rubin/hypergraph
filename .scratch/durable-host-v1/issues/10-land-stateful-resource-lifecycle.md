# 10 — Land stateful resource lifecycle for consumer cleanup

**What to build:** Finish the existing stateful-resource lifecycle work so a
consumer can bind scoped resources without reflective cleanup code. Async
cleanup must be recognized, invalid lazy runtime inputs must fail loudly, and
one cleanup failure must not prevent later cleanup.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Async close methods are awaited as asynchronous cleanup
- [ ] Passing a lazy resource handle as an ordinary runtime input raises a clear configuration error
- [ ] One failing cleanup does not prevent remaining resources from closing
- [ ] The branch passes its focused and warning-as-error suites
- [ ] Panda can replace its reflective graph closer with the library lifecycle
