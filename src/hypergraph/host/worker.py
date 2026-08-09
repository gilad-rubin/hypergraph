"""Worker internals for the durable host: the bounded drain.

There used to be an exclusive OS lock here — one ``fcntl.flock`` on
``<db path>.lock``, taken at ``work_forever()`` startup, refusing a second
worker on the same Run Home by name. It is gone. The lock did not answer
"is this half-finished Run dead, or is another worker holding it?"; it made
the question unaskable, and paid for that with a rule nobody wanted: a
notebook or a maintenance script could configure durable work but never
execute any, even work only it could build.

The lease answers it instead. Each claim is a compare-and-set that stamps
``claimed_by`` and ``lease_until`` on the row (``RunHome._claim_eligible``),
the holder renews while it works (``_renew_leases``), and a claim whose
lease ran out is adopted by whoever is still polling
(``_reclaim_expired``). Expiry proves nothing about the old worker, so the
safety property is not the lease but ``claim_seq``: an adopted submission
carries a new claim, and the presumed-dead worker's release matches no row.
That is the standard shape — SQS's visibility timeout, Kafka's session
timeout, Oban's ``locked_by`` plus rescue sweep — and it is why several
workers may now share one Home.
"""

from __future__ import annotations

import asyncio


async def _drain(tasks: set[asyncio.Task], drain_timeout: float) -> None:
    """Bounded drain: await active runs, then cancel what outlives the bound."""
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    _, still_pending = await asyncio.wait(pending, timeout=drain_timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
