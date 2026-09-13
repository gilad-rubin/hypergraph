"""Bytes cross a checkpointed node boundary through a BlobSerializer: stored once, restored on resume."""

import pytest
import pytest_asyncio

from hypergraph import Graph, node
from hypergraph.checkpointers import BlobSerializer, FileBlobStore, SqliteCheckpointer
from hypergraph.runners import AsyncRunner

aiosqlite = pytest.importorskip("aiosqlite")


@node(output_name="pdf")
def read_pdf(name: str) -> bytes:
    return b"%PDF-1.4 " + name.encode()


@node(output_name="size")
def measure(pdf: bytes) -> int:
    return len(pdf)


@pytest_asyncio.fixture
async def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "runs.db"), serializer=BlobSerializer(FileBlobStore(tmp_path / "blobs")))
    yield cp
    await cp.close()


@pytest.mark.asyncio
async def test_bytes_flow_between_nodes_and_are_stored_by_content(checkpointer, tmp_path):
    graph = Graph([read_pdf, measure], name="pdfs")
    runner = AsyncRunner(checkpointer=checkpointer)
    result = await runner.run(graph, name="a", workflow_id="wf-bytes")
    assert result["size"] == len(b"%PDF-1.4 a")
    blobs = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 1 and blobs[0].read_bytes() == b"%PDF-1.4 a"
    checkpoint = await checkpointer.get_checkpoint("wf-bytes")
    assert checkpoint.values["pdf"] == b"%PDF-1.4 a"
