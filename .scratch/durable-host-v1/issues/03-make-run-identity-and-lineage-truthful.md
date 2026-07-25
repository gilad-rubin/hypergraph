# 03 — Make Run identity, deduplication, rerun, and fork truthful

**What to build:** Give each accepted Run a complete pinned Definition
identity and a stable start fingerprint. Make webhook-style repeated
submission use the existing Run only when its meaning is identical, add
explicit rerun for repetition, and keep version migration on explicit fork.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** done — landed in commit `0676849f`; two-axis review findings repaired in `a8836312`.

- [x] Definition name, deployment version, and structural hash are pinned and visible
- [x] Identical nonterminal submission returns the existing Run; terminal reuse and fingerprint mismatch return distinct typed conflicts
- [x] A worker refuses incompatible Definition identity while an explicitly accepted prior identity can drain
- [x] Rerun creates a new workflow id with source inputs and retry lineage and accepts no input override
- [x] Fork changes compatible Definition identity with separate fork lineage and a recorded migration reason
