"""Cold boot -> waiting row -> answer update -> routed completion."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import ClassVar

from hypergraph import AsyncRunner, Graph, ifelse, interrupt, node
from hypergraph.materialization import LanceDBStore


@dataclass(frozen=True)
class Choice:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@node(output_name="prepared")
def prepare(text: str) -> str:
    return text.strip().lower()


@interrupt(answer_name="decision")
def review(prepared: str) -> Choice:
    return Choice(
        prompt=f"File {prepared}?",
        options=("publish", "archive"),
        evidence=({"preview": prepared},),
    )


@ifelse(when_true="publish", when_false="archive")
def choose_route(decision: str) -> bool:
    return decision == "publish"


@node(output_name="filed")
def publish(prepared: str) -> str:
    return f"published:{prepared}"


@node(output_name="filed")
def archive(prepared: str) -> str:
    return f"archived:{prepared}"


async def main() -> None:
    graph = Graph([prepare, review, choose_route, publish, archive], name="intake")
    with TemporaryDirectory() as directory:
        table = graph.as_table(
            identity="upload_id",
            store=LanceDBStore(directory),
            runner=AsyncRunner(),
        )

        first = await table.insert(upload_id="u-041", text=" Draft ")
        print(
            "insert:",
            first.outcome.value,
            first.status.value,
            first.pause.value.prompt,
            first.pause.response_key,
        )

        waiting = table.waiting()[0]
        print(
            "waiting:",
            waiting.id,
            waiting.pause.value.options,
            bool(waiting.provenance),
        )

        second = await table.update(
            waiting.id,
            **{waiting.pause.response_key: "publish"},
        )
        print("update:", second.outcome.value, second.status.value)
        print("row:", table.get(waiting.id))


if __name__ == "__main__":
    asyncio.run(main())
