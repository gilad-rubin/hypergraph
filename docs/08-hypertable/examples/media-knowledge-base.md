# Media Knowledge Base

Build a searchable knowledge base from video or audio recordings. Extract audio, transcribe, split into utterances, enrich each utterance with metadata, and generate embeddings. When you add new recordings or re-process existing ones, only changed content re-derives.

## When to Use

- Building a searchable archive of meetings, lectures, podcasts, or broadcast content
- Any pipeline where one media file produces many searchable segments
- Incremental processing where new recordings arrive daily and old ones rarely change

## The Pipeline

```text
recording --> extract_audio --> transcribe --> split_utterances
    --> [per utterance: clean_text --> extract_keywords --> embed]
```

One graph handles the recording level (audio extraction, transcription, splitting). A child graph handles each utterance (cleaning, keyword extraction, embedding). HyperTable stores everything and tracks what changed.

## Complete Implementation

```python
from typing import TypedDict

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.runners import SyncRunner


# ═══════════════════════════════════════════════════════════════
# COMPONENTS
# ═══════════════════════════════════════════════════════════════

class TranscriptionModel:
    """Wraps a speech-to-text model (Whisper, Deepgram, etc.)."""

    def __init__(self, model_name: str = "whisper-large-v3"):
        self.model_name = model_name

    def _config(self):
        return {"model": self.model_name}

    def transcribe(self, audio_bytes: bytes) -> dict:
        # Returns {"text": "...", "segments": [...]}
        ...


class LLM:
    """Wraps an LLM for metadata extraction."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self.model = model

    def _config(self):
        return {"model": self.model}

    def extract_keywords(self, text: str) -> list[str]:
        # Call LLM to extract keywords/topics
        ...


class Embedder:
    """Wraps an embedding model."""

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 256):
        self.model = model
        self.dim = dim

    def _config(self):
        return {"model": self.model, "dim": self.dim}

    def embed(self, text: str) -> list[float]:
        ...


# ═══════════════════════════════════════════════════════════════
# RECORDING-LEVEL NODES
# ═══════════════════════════════════════════════════════════════

@node(output_name="audio_bytes")
def extract_audio(file_path: str) -> bytes:
    """Extract audio track from video/audio file."""
    with open(file_path, "rb") as f:
        return f.read()
    # In production: ffmpeg to extract audio from video formats


@node(output_name="transcript")
def transcribe(audio_bytes: bytes, transcription_model: TranscriptionModel) -> dict:
    """Transcribe audio to text with timestamps."""
    return transcription_model.transcribe(audio_bytes)


class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str
    start_seconds: float
    end_seconds: float


@node(output_name="utterances")
def split_utterances(transcript: dict) -> list[Utterance]:
    """Split transcript into speaker-attributed utterances."""
    result = []
    for i, segment in enumerate(transcript.get("segments", [])):
        result.append(Utterance(
            utterance_id=f"u{i}",
            text=segment["text"],
            speaker=segment.get("speaker", "unknown"),
            start_seconds=segment["start"],
            end_seconds=segment["end"],
        ))
    return result


# ═══════════════════════════════════════════════════════════════
# PER-UTTERANCE NODES (child graph)
# ═══════════════════════════════════════════════════════════════

@node(output_name="clean_text")
def clean_utterance(text: str) -> str:
    """Normalize utterance text for indexing."""
    return " ".join(text.split()).strip()


@node(output_name="keywords")
def extract_keywords(clean_text: str, llm: LLM) -> list[str]:
    """Extract keywords and topics from utterance text."""
    return llm.extract_keywords(clean_text)


@node(output_name="embedding")
def embed_utterance(clean_text: str, embedder: Embedder) -> list[float]:
    """Generate embedding for vector search."""
    return embedder.embed(clean_text)


# ═══════════════════════════════════════════════════════════════
# BUILD THE TABLE
# ═══════════════════════════════════════════════════════════════

# Child graph: processes one utterance
process_utterance = Graph(
    [clean_utterance, extract_keywords, embed_utterance],
    name="process_utterance",
)

from hypergraph.materialization._lancedb_store import LanceDBStore

store = LanceDBStore("./recordings_store")

# Parent table: processes recordings, expands into utterances
recordings = HyperTable(
    [
        extract_audio,
        transcribe,
        split_utterances,
        process_utterance.as_node().map_over(
            "utterances", identity="utterance_id"
        ),
    ],
    identity="recording_id",
    store=store,
    on_error="store",
).bind(
    transcription_model=TranscriptionModel(),
    llm=LLM(),
    embedder=Embedder(),
).with_runner(SyncRunner())
```

## Processing Recordings

```python
# Index a batch of recordings
result = recordings.sync([
    {"recording_id": "ep-2024-01-15", "file_path": "/data/episodes/2024-01-15.mp4"},
    {"recording_id": "ep-2024-01-16", "file_path": "/data/episodes/2024-01-16.mp4"},
    {"recording_id": "ep-2024-01-17", "file_path": "/data/episodes/2024-01-17.mp4"},
])

print(f"Inserted: {result.inserted}, Skipped: {result.skipped}, Errored: {result.errored}")
```

## Reading the Knowledge Base

```python
# Get a recording's metadata
recording = recordings.get("ep-2024-01-15")
# {'recording_id': 'ep-2024-01-15', 'file_path': '...', 'audio_bytes': b'...',
#  'transcript': {...}, 'utterances': [...]}

# Get all utterances from a recording
utterances = recordings.children("ep-2024-01-15")
# [{'utterance_id': 'u0', 'text': '...', 'clean_text': '...', 'keywords': [...],
#   'embedding': [0.1, ...]}, ...]

# Count utterances across all recordings
recordings.count("utterance")  # child table name = identity without the _id suffix
```

## What Happens on Re-Run

```python
# Next day: re-sync the same recordings + one new one
result = recordings.sync([
    {"recording_id": "ep-2024-01-15", "file_path": "/data/episodes/2024-01-15.mp4"},
    {"recording_id": "ep-2024-01-16", "file_path": "/data/episodes/2024-01-16.mp4"},
    {"recording_id": "ep-2024-01-17", "file_path": "/data/episodes/2024-01-17.mp4"},
    {"recording_id": "ep-2024-01-18", "file_path": "/data/episodes/2024-01-18.mp4"},
])

# Only ep-2024-01-18 is new — the rest are skipped
print(f"Inserted: {result.inserted}, Skipped: {result.skipped}")
# Inserted: 1, Skipped: 3
```

Fingerprints cover the full derivation plan: source values, node code, and component configs. Swapping the transcription model or embedder changes the fingerprint, triggering re-derivation:

```python
# Upgrade to a better transcription model
recordings_v2 = recordings.bind(
    transcription_model=TranscriptionModel("whisper-large-v3-turbo"),
)

# All recordings re-transcribe and re-derive utterances
result = recordings_v2.sync([...])
```

## Handling Failures

With `on_error="store"`, a failed utterance doesn't block its siblings:

```python
# Embedding API times out on utterance u42
# → u42 gets an error row, all other utterances succeed

# Check which utterances failed
errors = recordings.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
)
for err in errors:
    print(f"{err['utterance_id']}: {err['_error']}")
# u42: TimeoutError: Embedding API timed out

# Re-sync: only u42 re-runs, everything else is skipped
result = recordings.sync([...])
```

## Production Considerations

**Component swaps.** When you upgrade a model, use `recompute()` to re-derive a specific column for all existing rows without re-running the entire pipeline:

```python
# Upgrade embedder, re-embed all utterances
recordings_v2 = recordings.bind(embedder=Embedder("text-embedding-3-large", dim=1024))
recordings_v2.recompute("embedding")
```

**Error monitoring.** Query error rows to build monitoring or alerting:

```python
error_count = len(recordings.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
))
if error_count > 0:
    print(f"Warning: {error_count} utterances failed processing")
```

**Async for throughput.** Switch to `AsyncRunner` for concurrent LLM/embedding calls:

```python
from hypergraph.runners import AsyncRunner

recordings_async = recordings.with_runner(AsyncRunner(max_concurrency=10))
await recordings_async.sync([...])
```

**Metadata columns.** Extra kwargs that don't match graph inputs are stored as metadata — no re-derivation triggered:

```python
recordings.insert(
    recording_id="ep-2024-01-15",
    file_path="/data/episodes/2024-01-15.mp4",
    channel="Channel 12",       # metadata — stored but doesn't trigger re-derive
    air_date="2024-01-15",      # metadata
    tags=["politics", "news"],  # metadata
)
```
