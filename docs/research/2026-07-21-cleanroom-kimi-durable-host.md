• # Design exercise — answer

  ## 1. The atoms

  I introduce exactly **two durable records**, both living in the relational database Ari already runs. No lock service, no inbox table, no outbox table, no event log separate from the journal. Tom's environment uses the same two concepts backed by the existing `FileJournal` — the interface is the seam, the storage is construction detail.

  **Atom A — the run record.** One row per `workflow_id`.

  | Field | Purpose |
  |---|---|
  | `workflow_id` (pk) | the identity everything already uses |
  | `intake_key` (unique, nullable) | dedup for N1 — webhook request-id, dashboard click-id |
  | `graph_name`, `graph_version` | what may claim this run (N10) |
  | `status` | `runnable / claimed / paused / stopped / completed / failed / stranded` |
  | `question` / `answer` | single-slot pause payload and the pending human answer |
  | `stop_requested`, `stop_reason` | the cross-process stop flag (N8) |
  | `holder_id`, `fencing_token`, `lease_expires_at` | the lease triple (N3, N4) |

  - **Comes to exist:** at first intake, in the same transaction that dedups the intake key.
  - **Refers to it:** intake handlers (by key), workers (claim scan, lease renewal, fencing check), the answer/stop/watch endpoints, and every journal write.

  **Atom B — the journal history, network-backed.** Append-only rows: `workflow_id, seq, kind, node, payload, attempt_no, writer_token, ts`. This is not a new concept — it is the existing pluggable journal interface implemented against the shared database (`PgJournal`), sitting next to `FileJournal` and `InMemoryJournal`. The one thing I add: every row carries the `fencing_token` it was written under, and the insert happens inside a transaction that re-checks the run row:

  ```sql
  -- the fencing gate: authority is checked at the moment of writing (N4)
  INSERT INTO journal (...)
  SELECT ... FROM runs
  WHERE workflow_id = $1 AND holder_id = $2 AND fencing_token = $3;
  -- zero rows inserted → the writer has lost authority → raise LostLease
  ```

  - **Comes to exist:** row by row, as the run progresses (given engine behavior).
  - **Refers to it:** the strategy on resume, watchers (N9), `fork_from`, and the claim scan (to distinguish parked from dead).

  Why this covers every need with nothing else:

  - **N1** — `INSERT INTO runs ... ON CONFLICT (intake_key) DO NOTHING RETURNING *`. A retried webhook returns the existing run; the effect happened once. Answers dedup the same way: the answer is accepted only by a compare-and-swap on `status = paused AND answer IS NULL`.
  - **N3** — claiming is one atomic update: `UPDATE runs SET holder=$me, fencing_token=fencing_token+1, lease_expires_at=now()+ttl WHERE workflow_id=$1 AND status IN ('runnable','paused-ready') AND lease_expires_at < now()`. Two processes cannot both win one row.
  - **N4** — the fencing gate above. A partitioned worker wakes up mid-write, its token no longer matches, the insert writes nothing and raises. Impossible at the moment of writing, not discouraged.
  - **N5** — parked = one row with `status='paused'`. Nothing in memory, no timer, no cost. The answer flips it to `runnable`; any worker picks it up.
  - **N6** — leases expire (seconds, heartbeated). After a crash or deploy the row simply becomes claimable again; the surviving worker's normal claim scan finds it. No sweeper daemon, no human.
  - **N8** — stop and continue both linearize on the single run row. Whichever compare-and-swap lands first wins; the loser gets a defined error (`RunStopped` / `AlreadyClaimed`). A stop that lands after a claim is honored at the next node boundary, because the strategy checks `stop_requested` at each journal write it already performs.
  - **N10** — the claim predicate includes `graph_version = my_version`. New code never touches old runs; a run no live worker can serve becomes `stranded` (loud, queryable), and the operator either keeps one old worker up or forks it forward explicitly.

  Deliberately not atoms: answers (folded into the run row's single slot — a run waits on one question at a time), dedup records (a unique column, not a table), events (journal rows *are* the event history), locks (the lease columns on the run row are the lock).

  ## 2. The code

  File layout the two teams share. Everything below is user-facing; the only library code implied is `Host`, `PgJournal`, `worker.serve()`, `host.watch()`.

  **`graphs.py` — user code, identical in both products (N11):**

  ```python
  from engine import build_graph, pause, NodeContext

  # ---- refund desk ----
  def validate_claim(claim_id: str) -> dict: ...
  def assess_risk(claim: dict) -> dict: ...

  def maybe_ask_dana(claim: dict, risk: dict) -> bool:
      if claim["amount"] <= 80 and risk["score"] < 0.3:
          return True
      # parks the run; costs nothing; survives restarts (N5)
      return pause(to="dana", claim=claim["claim_id"], amount=claim["amount"])

  def issue_refund(claim: dict, approved: bool, ctx: NodeContext) -> str:
      if not approved:
          return "declined"
      # ctx.idempotency_key == "refund-c-42:issue_refund" — stable across
      # re-execution after a crash (N7). At-least-once execution, plus this
      # key, plus the provider's idempotency = the money moves once.
      return payments.refund(claim["claim_id"], claim["amount"],
                             idempotency_key=ctx.idempotency_key)

  def notify_customer(claim: dict, receipt: str) -> None: ...

  refund_graph = build_graph(
      [validate_claim, assess_risk, maybe_ask_dana, issue_refund, notify_customer],
      name="refund", version=3,          # version pins the run to code (N10)
  )

  # ---- literature triage ----
  triage_graph = build_graph(
      [fetch, parse, extract_entities, summarize, index],
      name="triage", version=12,
  )
  ```

  **`host_setup.py` — construction is the only place the two environments differ (N2, N11):**

  ```python
  from engine import AsyncStrategy, SyncStrategy
  from control import Host, PgJournal, FileJournal

  def build_host(env: str) -> Host:
      # one wiring, many graphs; registering a new graph never rebuilds this (N2)
      journal = PgJournal(dsn=env("JOURNAL_DSN")) if env == "vms" \
                else FileJournal("runs.db")                       # notebook, nightly box
      host = Host(journal=journal, lease_ttl_seconds=30)

      host.register(refund_graph, make_strategy=AsyncStrategy)   # per-graph strategy choice
      host.register(triage_graph, make_strategy=AsyncStrategy)
      return host
  ```

  No user-facing concept is named after a storage technology: the code says *journal*, *host*, *run*. `PgJournal` appears once, at construction.

  **`api.py` — the intake service, runs on both VMs behind the load balancer:**

  ```python
  host = build_host("vms")

  @app.post("/webhooks/refund")
  async def refund_webhook(req):
      # Storefront retries aggressively; X-Request-Id is stable across retries.
      # First delivery inserts the run row; every retry returns the same row (N1).
      run = await host.start("refund", inputs={"claim_id": req.claim_id},
                             intake_key=req.headers["X-Request-Id"])
      return {"workflow_id": run.workflow_id, "status": run.status}

  @app.post("/dashboard/refunds/{workflow_id}/answer")
  async def dana_answers(workflow_id: str, req):
      # Dana answers a week later. Her impatient double-click / dashboard retry
      # dedups on intake_key the same way (N1, N5).
      run = await host.answer(workflow_id, value=req.approved,
                              intake_key=req.request_id)
      return {"status": run.status}   # 'runnable' — a worker will claim it now
  ```

  **`worker.py` — the executor, identical file on both VMs (N3, N6):**

  ```python
  host = build_host("vms")

  async def main():
      worker = host.worker(worker_id=socket.gethostname())
      # serve() loops: claim runs that are runnable or whose lease expired,
      # re-invoke strategy.run(graph, inputs_or_answer, workflow_id=...) —
      # the given resume primitive — renew the lease every 10s, check the
      # stop flag at every node boundary. A LostLease from the fencing gate
      # abandons the run loudly instead of writing (N4).
      await worker.serve()

  asyncio.run(main())
  ```

  The full lifecycle, narrated over the code above:

  1. **Started from a retried webhook.** Storefront POSTs twice; `intake_key` makes the second a read. Run `refund-c-42` is claimed by vm-1 and executes until `maybe_ask_dana` pauses it. The process now holds nothing for this run — one row in the DB (N5).
  2. **Crash.** vm-1 dies mid-`assess_risk` on another run, `refund-c-99`. Its lease expires 30s later; vm-2's claim scan picks it up; `strategy.run(..., workflow_id="refund-c-99")` resumes from the journal, skipping completed nodes (N6). Nobody re-submits anything.
  3. **Deploy.** Ari stops vm-2, deploys refund graph v4, starts it. Runs pinned to v3 are *not* claimed by the v4 worker (N10). Parked runs on Dana stay safely parked; running v3 runs surface as `stranded` in one query, and the operator either leaves one v3 worker alive for a week or forks:

  ```python
  # explicit migration, using the given fork primitive — never silent pickup
  new_run = await host.fork("refund-c-77", to_version=4)
  ```

  4. **Continued with Dana's answer.** Six days later Dana clicks "approve". The dashboard POSTs; the answer CAS lands; the run is `runnable`; whichever VM is alive claims it and `issue_refund` runs with `idempotency_key="refund-c-42:issue_refund"` — if vm-1 had died *after* charging but *before* journaling, the retry carries the same key and the provider returns the original receipt instead of charging twice (N7).
  5. **Stopped from elsewhere.** Ari, from his laptop, on a run currently claimed by vm-1:

  ```python
  # any process, any machine (N8)
  await host.stop("refund-c-42", reason="customer withdrew")
  ```

  If his CAS lands before a "continue" claim, the run is `stopped` and the continue attempt gets `RunStopped`. If the claim landed first, the stop is honored at the next node boundary and the continue's result reads `stopped`. Both orders defined, neither lucky.
  6. **Watched from a process that didn't start it.** Maya (or Noa, or a coding agent) opens a CLI anywhere with database access:

  ```python
  host = build_host("vms")           # read-only use; starts no worker
  async for event in host.watch("refund-c-42"):   # polls the journal + run row
      print(event.ts, event.kind, event.node, event.payload)
  ```

  She sees the full history — including the stretch where vm-2 auto-continued the run after vm-1's crash — and new events as they land (N9). Live tail is polling or Postgres `LISTEN/NOTIFY`; both are database features, not new software.

  **Tom's side, same user code, zero infrastructure (N11):**

  ```python
  # notebook — a Python process and a file on disk
  host = build_host("notebook")
  result = await host.run_local("triage", {"doc": "paper.pdf"},
                                workflow_id=f"triage-{doc_id}")

  # nightly.py — run by a cron entry on the lab box
  host = build_host("notebook")
  await host.run_local("triage", {"corpus": "/data/nightly"},
                       workflow_id="triage-nightly")
  ```

  The box reboots for patches; cron (or Tom) reruns `nightly.py`; the same `workflow_id` resumes from `runs.db` where it stopped — that resume is the *given* journal primitive, so Tom's side genuinely needs nothing new.

  ## 3. The boundary

  What I deliberately did **not** build, because a category already owns it:

  - **The database and its HA.** Storage, durability, replication, backup of the journal and run rows: the RDBMS Ari already operates. If the DB is down, nothing is durably down-able around that — accept it, don't paper over it.
  - **HTTP, webhooks, TLS, load balancing, the dashboard.** Web framework + the existing LB. My design only requires handlers to pass a stable `intake_key`.
  - **Process supervision, deploys, scheduling.** systemd/docker restarts workers; cron fires the nightly run. A worker that restarts is the recovery mechanism — supervision of it is a solved category.
  - **Live event fanout.** Watchers read the journal; `LISTEN/NOTIFY` or 1s polling is plenty at this scale. I did not build a broker.
  - **Payment idempotency.** The provider's API owns it; my job ends at handing the node a stable key.
  - **A distributed lock service.** Not adopted either — leases and fencing live in the same database as the journal, one authority, one failure mode to reason about.

  Where I was tempted, and said no:

  - **A work queue for dispatch** ("workers pull from a durable queue"). Tempting, but the claim scan over `runnable AND lease_expired` rows *is* the queue, with exactly the semantics needed (visibility timeout = lease). A queue would add a second system to keep consistent with the run row.
  - **Leader election / a coordinator process.** Every need here is per-run mutual exclusion, not global leadership. A coordinator is a single point of failure that then needs its own fencing — recursion I don't need.
  - **Exactly-once execution machinery** (two-phase commit with the payment API, transactional outbox to the provider). The brief says the engine reserves attempts at-least-once and *none is wanted*; the honest answer is at-least-once + stable identity + provider idempotency, and never writing "exactly-once" in any doc.
  - **Event-sourced replay / deterministic re-execution.** Explicitly excluded by the primitives. The journal is a resume log, not a replay log.
  - **A UI.** Code first; Dana's dashboard and the watch CLI are consumers of four functions (`start`, `answer`, `stop`, `watch`).

  ## 4. One sentence

  The missing software is a thin control plane: a network-reachable journal for the existing engine — backed by the relational database the teams already run — in which one row per run carries a lease and a fencing token that gates every journal write, plus a small host/registry shell that wires graphs, per-graph strategies, intake dedup, stop, and watch to it entirely at construction time.

