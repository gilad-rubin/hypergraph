"""Share unchanged chunks while attaching a new embedding recipe."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from typing import TypedDict

from hypergraph import Graph, node
from hypergraph.materialization import LanceDBStore


class Chunk(TypedDict):
    chunk_id: str
    chunk_text: str


class Embedder:
    def __init__(self, model: str) -> None:
        self.model = model

    def _config(self) -> dict[str, str]:
        return {"model": self.model}

    def embed(self, text: str) -> list[float]:
        return [float(len(self.model)), float(len(text))]


@node(output_name="chunks")
def split_chunks(text: str) -> list[Chunk]:
    return [Chunk(chunk_id=f"c-{index}", chunk_text=part) for index, part in enumerate(text.split())]


@node(output_name="vector")
def embed_chunk(chunk_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(chunk_text)


def search_recipe(model: str) -> Graph:
    child = Graph([embed_chunk], name="embed_chunk")
    mapped = child.as_node(name="embedded_chunks").map_over("chunks", identity="chunk_id")
    return Graph([split_chunks, mapped], name="search_recipe").bind(embedder=Embedder(model))


def main() -> None:
    with TemporaryDirectory() as directory:
        documents = search_recipe("small").as_table(
            identity="document_id",
            store=LanceDBStore(directory),
            name="documents",
        )
        documents.insert(document_id="d-1", text="alpha beta")

        candidate = documents.attach(
            "search-large",
            graph=search_recipe("large"),
            outputs={"text": "chunk_text", "vector": "vector"},
        )
        receipt = candidate.sync()
        text = candidate.output("text")
        vector = candidate.output("vector")
        query_spec = candidate.create_index("large")
        hit = documents.search([5.0, 5.0], index="large", limit=1)[0]

        print(f"sync: updated={receipt.updated} fresh={candidate.status().is_fresh}")
        print(f"text: {text.table}.{text.column} shared={text.shared}")
        print(f"vector: {vector.table}.{vector.column} shared={vector.shared}")
        print(f"query: {query_spec['on']}.{query_spec['vector']}")
        print(f"nearest: {hit['document_id']} {hit['chunk_text']}")


if __name__ == "__main__":
    main()
