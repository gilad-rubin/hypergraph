"""DerivedTable end-to-end demo with real Subtext data and live API calls.

Pipeline:
  Audio (.m4a) ──[ElevenLabs STT]──▶ TranscribedTurn
       └─────────[OpenAI embed]──────▶ EmbeddedTurn (chained)

Run:
  cd /Users/giladrubin/python_workspace/hypergraph
  uv run python examples/subtext_materialization.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv

from hypergraph.materialization import (
    ContentKey,
    DerivedTable,
    Identity,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioClip:
    clip_id: Annotated[str, Identity]
    file_path: Annotated[str, ContentKey]
    show: str
    channel: str


@dataclass(frozen=True)
class TranscribedTurn:
    turn_id: Annotated[str, Identity]
    clip_id: str
    speaker: str
    text: Annotated[str, ContentKey]
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class EmbeddedTurn:
    turn_id: Annotated[str, Identity]
    text: Annotated[str, ContentKey]
    speaker: str
    vector: list[float]


# ---------------------------------------------------------------------------
# Components (real API clients with _config())
# ---------------------------------------------------------------------------


class ElevenLabsTranscriber:
    def __init__(self, api_key: str, language: str = "he"):
        from elevenlabs import ElevenLabs

        self._client = ElevenLabs(api_key=api_key, timeout=600.0)
        self._language = language

    def _config(self):
        return {"provider": "elevenlabs", "language": self._language}

    def transcribe(self, path: str, *, keyterms: list[str] | None = None) -> list[dict]:
        with open(path, "rb") as f:
            result = self._client.speech_to_text.convert(
                file=f,
                model_id="scribe_v2",
                language_code=self._language,
                diarize=True,
                timestamps_granularity="word",
                tag_audio_events=False,
                temperature=0.0,
                seed=42,
                keyterms=keyterms or [],
            )
        raw = result.model_dump(mode="json", exclude_none=True)
        return self._words_to_turns(raw.get("words", []))

    @staticmethod
    def _words_to_turns(words: list[dict]) -> list[dict]:
        turns: list[dict] = []
        current: dict | None = None
        for w in words:
            if w.get("type") not in ("word", "spacing", "punctuation"):
                continue
            speaker = w.get("speaker_id") or "unknown"
            text = w.get("text", "")
            start = w.get("start") or 0.0
            end = w.get("end") or start
            if current is None or current["speaker"] != speaker:
                if current and current["text"].strip():
                    turns.append(current)
                current = {"speaker": speaker, "start": start, "end": end, "text": text}
            else:
                current["text"] += text
                current["end"] = end
        if current and current["text"].strip():
            turns.append(current)
        return turns


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def _config(self):
        return {"model": self._model}

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(input=text, model=self._model)
        return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Derive functions
# ---------------------------------------------------------------------------


def transcribe_clip(clip: AudioClip, transcriber: ElevenLabsTranscriber) -> list[TranscribedTurn]:
    keyterms_path = Path(__file__).resolve().parents[1] / ".." / "subtext" / "data" / "stt" / "elevenlabs_global_keyterms.json"
    keyterms: list[str] = []
    if keyterms_path.exists():
        data = json.loads(keyterms_path.read_text())
        categories = data.get("categories", {})
        keyterms = [t for terms in categories.values() for t in terms]

    turns = transcriber.transcribe(clip.file_path, keyterms=keyterms)
    return [
        TranscribedTurn(
            turn_id=f"{clip.clip_id}/t{i}",
            clip_id=clip.clip_id,
            speaker=t["speaker"],
            text=t["text"].strip(),
            start_seconds=t["start"],
            end_seconds=t["end"],
        )
        for i, t in enumerate(turns)
        if t["text"].strip()
    ]


def embed_turn(turn: TranscribedTurn, embedder: OpenAIEmbedder) -> EmbeddedTurn:
    return EmbeddedTurn(
        turn_id=turn.turn_id,
        text=turn.text,
        speaker=turn.speaker,
        vector=embedder.embed(turn.text),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def trim_audio(src: str, dst: str, seconds: int = 90):
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", str(seconds), "-c", "copy", dst],
        capture_output=True,
        check=True,
    )


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".." / "superposition" / ".env")

    el_key = os.environ.get("ELEVENLABS_API_KEY")
    oai_key = os.environ.get("OPENAI_API_KEY")
    if not el_key or not oai_key:
        raise SystemExit("Set ELEVENLABS_API_KEY and OPENAI_API_KEY in superposition/.env")

    audio_dir = Path(__file__).resolve().parents[1] / ".." / "subtext" / "data" / "audio"
    audio_files = sorted(audio_dir.glob("*.m4a"))
    if not audio_files:
        raise SystemExit(f"No .m4a files in {audio_dir}")

    src_audio = audio_files[0]
    print(f"Source audio: {src_audio.name}")

    # Trim to 90 seconds for a cheap demo
    tmp = tempfile.mkdtemp(prefix="subtext_mat_")
    trimmed = os.path.join(tmp, "clip.m4a")
    print("Trimming to 90 seconds...")
    trim_audio(str(src_audio), trimmed, seconds=90)

    store_path = os.path.join(tmp, "lance_store")
    print(f"LanceDB store: {store_path}\n")

    # Components
    transcriber = ElevenLabsTranscriber(api_key=el_key)
    embedder = OpenAIEmbedder(api_key=oai_key)

    # DerivedTable chain: Audio → Turns → Embeddings
    turns_table = DerivedTable(
        source=AudioClip,
        output=TranscribedTurn,
        derive=transcribe_clip,
        components={"transcriber": transcriber},
        store=store_path,
    )

    embeddings_table = DerivedTable(
        source=turns_table,  # chained — cascading is always on
        output=EmbeddedTurn,
        derive=embed_turn,
        components={"embedder": embedder},
        store=store_path,
    )

    # ── Insert ────────────────────────────────────────────────────────────
    clip = AudioClip(
        clip_id="demo_clip_1",
        file_path=trimmed,
        show="הפטריוטים" if "הפטריוטים" in src_audio.name else "המהדורה המרכזית",
        channel="ערוץ 14",
    )

    print("═" * 60)
    print("STEP 1: Insert — ElevenLabs STT + OpenAI embed")
    print("═" * 60)
    turns_table.insert([clip], on_error="ignore")

    n_turns = turns_table.count()
    n_embeds = embeddings_table.count()
    print(f"  Transcribed turns:  {n_turns}")
    print(f"  Embedded turns:     {n_embeds}  (cascade)")

    # Show a few turns
    if n_turns > 0:
        print("\n  Sample turns:")
        for i in range(min(3, n_turns)):
            tid = f"{clip.clip_id}/t{i}"
            t = turns_table.get(turn_id=tid)
            if t:
                print(f"    [{t.start_seconds:.1f}s-{t.end_seconds:.1f}s] {t.speaker}: {t.text[:80]}")

    # Show embedding dims
    first_emb = embeddings_table.get(turn_id=f"{clip.clip_id}/t0")
    if first_emb:
        print(f"\n  Embedding dim: {len(first_emb.vector)}")

    # ── Content-key skip ──────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("STEP 2: Re-insert same clip — should skip (content key hit)")
    print("═" * 60)
    v_before = turns_table.version
    turns_table.insert([clip], on_error="ignore")
    v_after = turns_table.version
    print(f"  Version before: {v_before}  after: {v_after}  (no change = skip worked)")
    print(f"  Turns still:    {turns_table.count()}")

    # ── Errors ────────────────────────────────────────────────────────────
    errors = turns_table.errors()
    embed_errors = embeddings_table.errors()
    if errors or embed_errors:
        print(f"\n  Turn errors: {len(errors)}")
        print(f"  Embed errors: {len(embed_errors)}")

    # ── Similarity search ─────────────────────────────────────────────────
    if n_embeds >= 2 and first_emb:
        print("\n" + "═" * 60)
        print("STEP 3: Cosine similarity between turns")
        print("═" * 60)
        all_turns_ids = [f"{clip.clip_id}/t{i}" for i in range(n_embeds)]
        scored = []
        for tid in all_turns_ids[1:]:
            e = embeddings_table.get(turn_id=tid)
            if e:
                sim = cosine_sim(first_emb.vector, e.vector)
                scored.append((sim, e))
        scored.sort(key=lambda x: -x[0])
        print(f"  Query: {first_emb.text[:60]}...")
        for sim, e in scored[:3]:
            print(f"    {sim:.3f}  {e.text[:60]}...")

    # ── Versioning snapshot ───────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("STEP 4: Versioning — snapshot at v1 vs current")
    print("═" * 60)
    v_current = turns_table.version
    v1 = 1
    snap = turns_table.at(v1)
    print(f"  Snapshot at v{v1}: {snap.count()} rows")
    print(f"  Current   at v{v_current}: {turns_table.count()} rows")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("DONE")
    print("═" * 60)
    print(f"  Audio:      {src_audio.name} (first 90s)")
    print("  STT:        ElevenLabs scribe_v2 (Hebrew, diarized)")
    print("  Embeddings: OpenAI text-embedding-3-small")
    print(f"  Turns:      {turns_table.count()} rows  (1:N explosion from 1 clip)")
    print(f"  Embeddings: {embeddings_table.count()} rows  (chained cascade)")
    print(f"  Store:      {store_path}")
    print(f"\n  To clean up: rm -rf {tmp}")


if __name__ == "__main__":
    main()
