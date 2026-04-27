"""Retriever for the chatbot RAG index.

Loads ``chatbot/index/{chunks.jsonl, vectors.npy}`` lazily and ranks chunks by
cosine similarity against a query embedding.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger("crtv.chatbot.retriever")

INDEX_DIR = Path(__file__).parent / "index"
CHUNKS_PATH = INDEX_DIR / "chunks.jsonl"
VECTORS_PATH = INDEX_DIR / "vectors.npy"

EMBED_MODEL = os.environ.get("CRTV_EMBED_MODEL", "text-embedding-3-small")


class Retriever:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[dict] | None = None
        self._vecs: np.ndarray | None = None
        self._mtime: float = 0.0

    def _load(self) -> None:
        if not CHUNKS_PATH.exists() or not VECTORS_PATH.exists():
            self._chunks = []
            self._vecs = np.zeros((0, 1536), dtype=np.float32)
            self._mtime = 0.0
            return
        mtime = max(CHUNKS_PATH.stat().st_mtime, VECTORS_PATH.stat().st_mtime)
        if self._chunks is not None and mtime == self._mtime:
            return
        chunks: list[dict] = []
        for line in CHUNKS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                chunks.append(json.loads(line))
        vecs = np.load(VECTORS_PATH)
        # normalize once for cosine via dot
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._chunks = chunks
        self._vecs = (vecs / norms).astype(np.float32)
        self._mtime = mtime

    def _embed_query(self, query: str) -> np.ndarray:
        from openai import OpenAI

        api_key = os.environ.get("CRTV_OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("CRTV_OPENAI_API_KEY not set")
        client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("CRTV_OPENAI_BASE_URL") or None,
            timeout=30.0,
            max_retries=2,
        )
        resp = client.embeddings.create(model=EMBED_MODEL, input=[query])
        v = np.asarray(resp.data[0].embedding, dtype=np.float32)
        n = np.linalg.norm(v) or 1.0
        return v / n

    def search(self, query: str, k: int = 5) -> list[dict]:
        with self._lock:
            self._load()
            if not self._chunks:
                return []
            q = self._embed_query(query)
            sims = self._vecs @ q
            idx = np.argsort(-sims)[:k]
            out = []
            for i in idx:
                c = dict(self._chunks[int(i)])
                c["score"] = float(sims[int(i)])
                out.append(c)
            return out


_singleton: Retriever | None = None


def get_retriever() -> Retriever:
    global _singleton
    if _singleton is None:
        _singleton = Retriever()
    return _singleton
