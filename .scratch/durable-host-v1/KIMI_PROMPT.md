Work the Durable Host V1 ticket tree in this Hypergraph checkout.

Workspace:
- Repository: `/Users/giladrubin/python_workspace/hypergraph`
- Expected branch: `feat/durable-host-v1`
- Parent spec: `docs/prd/0017-durable-host-v1-program.md`
- Decision package: `docs/research/2026-07-23-durable-host-amendments.md`
- Ticket tree: `.scratch/durable-host-v1/issues/`

Read before acting:
1. `AGENTS.md`, `CONTEXT.md`, and `docs/README.md`
2. The parent spec in full
3. The amendment package in full
4. ADRs 0004–0008 and PRDs 0010–0011
5. Every ticket in the tree, so you understand its blocking edges
6. Deeper local `AGENTS.md` files before changing an owned area

Operating contract:
- Work the lowest-numbered frontier ticket only.
- A frontier ticket has every listed ticket blocker complete.
- Keep one ticket per clean conventional commit and include its ticket number.
- Preserve unrelated untracked and modified files. Never sweep them into a commit.
- Use `uv`; run focused tests first and the warning-as-error CI-equivalent suite before each code ticket is complete.
- Preserve sync/async parity, nested-graph behavior, and direct Tier-0 runner behavior.
- Update public docs and contract tests with any public API change.
- Never add a broker, second journal, Hypergraph server, reconnectable handle, app-less submit catalog, or OutputLog.
- Do not touch Panda before ticket 07. Do not delete Panda guards or make a production claim before ticket 11.
- Do not start ticket 15 without an explicit maintainer message confirming a real multi-worker need.
- Do not push, merge, or publish a release.

Start now with ticket 01 only.

Ticket 01 is a prototype gate:
- Fold the accepted A1–A16 decisions into the relevant ADRs, intent specs, wayfinding, and domain vocabulary without implementing runtime code.
- Produce the inspectable decision-grade prototype required by the ticket. It must show a realistic Panda-shaped Batch and all listed before/after states.
- Validate links and document consistency.
- Commit ticket 01 cleanly.
- Then STOP and report the prototype artifact, before/after summary, changed files, validation, and commit.

Do not begin ticket 02 until the maintainer explicitly approves the ticket-01 prototype in this session. That approval gate overrides the fact that later ticket files are marked ready-for-agent.

Use this status format when reporting:

**✅ Done** — completed work and proof, including commit and tests.

**🛝 Proof/Playground** — the ready-to-inspect prototype or later user-facing proof.

**⏳ Running** — only work still in flight.

**❓ Dilemmas** — only a real maintainer decision; otherwise “No decisions needed.”
