"""Second-process worker for the builder-registry round trip.

Run as ``python -m tests.test_host._builder_worker <db> <ledger> <builds>``.

It is deliberately a REAL separate interpreter, and deliberately ignorant:
it opens the same SQLite Run Home the parent opened, registers only the
BUILDER — no graph, no factor, no ``serve()`` argument of any kind — and
drains whatever it finds. Everything it needs to construct the Definition
arrives on the submission row.

That ignorance is the assertion. A test that served the same configured
graph in both processes would pass whether or not the builder registry
existed; this one cannot even name the Definition it is about to run.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hypergraph import RunHome, RunQuery, serve  # noqa: E402
from hypergraph.host.views import TERMINAL_WORKFLOW_STATUSES  # noqa: E402
from tests.test_host._builder_fixture import BUILD_LOG_ENV, BUILDER_KEY, LEDGER_ENV, scaling_graph  # noqa: E402

#: How many Runs the parent accepted before handing over the file. The child
#: waits for exactly that many terminal Runs rather than for "nothing queued",
#: which is true a moment before the first claim as well as after the last.
EXPECTED = int(os.environ.get("HYPERGRAPH_BUILDER_EXPECTED", "1"))


async def main() -> int:
    db, ledger, builds = sys.argv[1], sys.argv[2], sys.argv[3]
    os.environ[LEDGER_ENV] = ledger
    os.environ[BUILD_LOG_ENV] = builds
    home = RunHome.open(f"file:{db}")
    host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
    client = host.client
    worker = asyncio.create_task(host.work_forever("builder-worker", poll_interval=0.01))
    print("worker-ready", flush=True)
    try:
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            views = await client.list(RunQuery(limit=200))
            settled = [view for view in views if view.status in TERMINAL_WORKFLOW_STATUSES]
            if len(settled) >= EXPECTED:
                break
            await asyncio.sleep(0.05)
    finally:
        host.shutdown()
        await asyncio.wait_for(worker, timeout=30)
        await home.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
