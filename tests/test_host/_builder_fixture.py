"""The configured graph family both processes of the builder tests build.

The point of a builder registry is that TWO processes agree on what a key
means while only ONE of them ever chose the arguments. So the graph lives
here, importable from both, and everything that varies — the factor, and
therefore the Definition NAME — arrives as data.

The naming rule is the one that made the registry necessary in the first
place: ``structural_hash`` deliberately ignores bound values, so a family
whose configuration matters has to put the configuration in the NAME, or two
different recipes would share one durable identity. That is exactly why the
name a submission pins cannot be reversed back into the arguments that built
it, and why the arguments have to travel on the row.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hypergraph import AsyncRunner, Graph, node

#: The address the tests register and submissions carry.
BUILDER_KEY = "tests.scaler"

#: Env var naming an append-only file the built graph writes one line to per
#: execution. It is how a parent process proves a CHILD process really ran
#: the work: the ledger outlives the child, the RunResult does not.
LEDGER_ENV = "HYPERGRAPH_BUILDER_LEDGER"

#: Env var counting how many times the builder itself was CALLED in this
#: process — the memoization evidence. One line per construction.
BUILD_LOG_ENV = "HYPERGRAPH_BUILDER_BUILDS"


def _append(env_var: str, line: str) -> None:
    path = os.environ.get(env_var)
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")


def scaling_graph(args: Mapping[str, Any]) -> Graph:
    """Build the ``scale-x<factor>`` Definition from arguments alone."""
    factor = int(args["factor"])
    _append(BUILD_LOG_ENV, f"factor={factor}")

    @node(output_name="out")
    async def scale(x: int) -> int:
        _append(LEDGER_ENV, f"{os.getpid()}:{x}:{x * factor}")
        return x * factor

    return Graph([scale], name=f"scale-x{factor}").with_runner(AsyncRunner())


def read_ledger(path: str | Path) -> list[str]:
    """Every recorded execution, in the order it was written."""
    file = Path(path)
    return file.read_text(encoding="utf-8").splitlines() if file.exists() else []
