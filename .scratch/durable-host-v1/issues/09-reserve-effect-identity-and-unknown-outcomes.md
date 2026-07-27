# 09 — Reserve effect identity and surface unknown outcomes

**What to build:** Let a node declare that it may cause an external effect,
reserve its stable effect identity before provider dispatch, and surface an
unwitnessed settlement as an unknown outcome that recovery cannot spend
again automatically.

**Blocked by:** 08 — Persist pending-node boundaries before sibling execution.

**Status:** ready-for-agent

- [ ] Effectful nodes declare their external-effect boundary explicitly
- [ ] Stable effect identity is committed before provider dispatch for every declared effect
- [ ] Kill tests distinguish pre-dispatch, witnessed settlement, and ambiguous post-dispatch loss
- [ ] Ambiguous settlement becomes unknown outcome and never dispatches again automatically
- [ ] Node-owned RetryPolicy budgets and windows remain unchanged by Host recovery
