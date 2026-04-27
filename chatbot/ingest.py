"""Chatbot RAG ingestion pipeline.

Reads PDFs from ``chatbot/corpus/``, uses an LLM to split each document into
semantically coherent chunks with ``section_title`` + ``source_label``
metadata, embeds each chunk via OpenAI ``text-embedding-3-small``, and persists
to ``chatbot/index/`` (``chunks.jsonl``, ``vectors.npy``, ``manifest.json``).

Idempotent: a manifest tracks sha256 per file; unchanged PDFs are skipped,
removed PDFs have their chunks dropped, changed PDFs are re-chunked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

logger = logging.getLogger("crtv.chatbot.ingest")

ROOT = Path(__file__).parent
CORPUS_DIR = ROOT / "corpus"
INDEX_DIR = ROOT / "index"
CHUNKS_PATH = INDEX_DIR / "chunks.jsonl"
VECTORS_PATH = INDEX_DIR / "vectors.npy"
MANIFEST_PATH = INDEX_DIR / "manifest.json"

EMBED_MODEL = os.environ.get("CRTV_EMBED_MODEL", "text-embedding-3-small")
CHUNK_MODEL = os.environ.get("CRTV_CHUNK_MODEL", "gpt-5.4-mini")
CHUNK_WINDOW_CHARS = 14000  # ~3.5k tokens per LLM chunking call
CHUNK_CONCURRENCY = int(os.environ.get("CRTV_CHUNK_CONCURRENCY", "8"))
EMBED_BATCH = 64

# Human-friendly source labels surfaced in chatbot citations.
SOURCE_LABELS = {
    "ASA_Post_Stroke_Depression.pdf": "ASA",
    "who-guide.pdf": "WHO",
    "National-Clinical-Guideline-for-Stroke-2023.pdf": "UK National Clinical Guideline for Stroke",
    "alt-murphy-et-al-2025-european-stroke-organisation-(eso)-guideline-on-motor-rehabilitation.pdf": "ESO motor rehabilitation guideline",
    "brady-et-al-2025-european-stroke-organisation-(eso)-guideline-on-aphasia-rehabilitation.pdf": "ESO aphasia rehabilitation guideline",
    "a_carers_guide_to_stroke.pdf": "Stroke Association carers guide",
}


@dataclass
class Chunk:
    id: str
    source_file: str
    source_label: str
    page_start: int
    page_end: int
    section_title: str
    summary: str
    key_questions: list[str]
    topics: list[str]
    text: str


# --- io helpers ----------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _source_label(filename: str) -> str:
    if filename in SOURCE_LABELS:
        return SOURCE_LABELS[filename]
    return Path(filename).stem.replace("-", " ").replace("_", " ")


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"files": {}, "embed_model": EMBED_MODEL}


def _save_manifest(manifest: dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _load_chunks() -> list[dict]:
    if not CHUNKS_PATH.exists():
        return []
    out = []
    for line in CHUNKS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _save_chunks_and_vectors(chunks: list[dict], vectors: np.ndarray) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with CHUNKS_PATH.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    np.save(VECTORS_PATH, vectors)


# --- pdf extraction ------------------------------------------------------


def _extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.warning("pdf extract failed %s p%d: %s", pdf_path.name, i, e)
            text = ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            pages.append((i, text))
    return pages


def _group_pages(pages: list[tuple[int, str]], target_chars: int) -> list[tuple[int, int, str]]:
    """Group pages into (page_start, page_end, joined_text) windows below target_chars."""
    windows: list[tuple[int, int, str]] = []
    buf: list[tuple[int, str]] = []
    size = 0
    for pg, txt in pages:
        if size and size + len(txt) > target_chars:
            windows.append((buf[0][0], buf[-1][0], "\n\n".join(t for _, t in buf)))
            buf = []
            size = 0
        buf.append((pg, txt))
        size += len(txt)
    if buf:
        windows.append((buf[0][0], buf[-1][0], "\n\n".join(t for _, t in buf)))
    return windows


# --- llm chunking --------------------------------------------------------

CHUNK_SYSTEM_PROMPT = """You are an expert medical librarian preparing a stroke-care knowledge base for a patient-and-caregiver chatbot.

Given a window of text from a clinical guideline or patient-facing handbook, split it into SEMANTICALLY COHERENT chunks. Each chunk should be one complete idea, recommendation, or explanation — NOT split by arbitrary word count. A chunk boundary is a topic shift: a new recommendation, a new symptom/condition, a new caregiver scenario, a new subsection of guidance. Chunks can be short (a single recommendation) or long (a full explanation with examples), whatever preserves the idea intact.

For each chunk produce enrichment metadata that makes it easy to retrieve later when a caregiver or patient asks a natural-language question:

- section_title: a short, descriptive title (5-10 words) naming the idea.
- summary: 1-2 plain-language sentences capturing what this chunk tells the reader. Written for a caregiver, not a clinician.
- key_questions: 3-6 natural questions a caregiver or stroke survivor might ask that this chunk would answer. Phrase them the way a real person types into a search bar ("how do I help my dad stand up safely?", "what is post-stroke depression?"). These are retrieval bait — make them diverse.
- topics: 3-8 short tag-style keywords (e.g., "depression", "transfers", "swallowing", "caregiver burnout").
- text: the source text for this chunk, VERBATIM. Do NOT paraphrase, summarize, translate, or rewrite. You may strip headers, footers, page numbers, running titles, and obvious OCR noise (repeated garbage characters, hyphenation artifacts).

Skip purely boilerplate sections entirely (table of contents, reference lists, author affiliations, copyright pages, legal disclaimers). Return nothing for those.

Respond with JSON only, shape:
{"chunks": [
  {
    "section_title": "...",
    "summary": "...",
    "key_questions": ["...", "..."],
    "topics": ["...", "..."],
    "text": "..."
  }
]}
"""


def _llm_chunk(window_text: str, source_label: str, model: str) -> list[dict]:
    from openai import OpenAI

    api_key = os.environ.get("CRTV_OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("CRTV_OPENAI_API_KEY not set")
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("CRTV_OPENAI_BASE_URL") or None,
        timeout=float(os.environ.get("CRTV_CHUNK_TIMEOUT", "120")),
        max_retries=2,
    )
    user = f"Source: {source_label}\n\n---BEGIN WINDOW---\n{window_text}\n---END WINDOW---"
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=8192,
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("chunk llm returned non-json; skipping window")
        return []
    return parsed.get("chunks") or []


def _window_to_chunks(
    w_idx: int,
    p_start: int,
    p_end: int,
    text: str,
    pdf_path: Path,
    source_label: str,
    model: str,
) -> list[Chunk]:
    try:
        raw_chunks = _llm_chunk(text, source_label, model)
    except Exception as e:
        logger.warning("chunking failed %s w%d: %s", pdf_path.name, w_idx, e)
        return []
    out: list[Chunk] = []
    for c_idx, rc in enumerate(raw_chunks):
        t = (rc.get("text") or "").strip()
        if len(t) < 80:
            continue
        kq = [q.strip() for q in (rc.get("key_questions") or []) if isinstance(q, str) and q.strip()]
        tags = [s.strip().lower() for s in (rc.get("topics") or []) if isinstance(s, str) and s.strip()]
        cid = f"{pdf_path.stem}__w{w_idx}_{c_idx}"
        out.append(
            Chunk(
                id=cid,
                source_file=pdf_path.name,
                source_label=source_label,
                page_start=p_start,
                page_end=p_end,
                section_title=(rc.get("section_title") or "Section").strip()[:120],
                summary=(rc.get("summary") or "").strip(),
                key_questions=kq,
                topics=tags,
                text=t,
            )
        )
    return out


def _chunk_pdf(pdf_path: Path, source_label: str, model: str) -> list[Chunk]:
    pages = _extract_pages(pdf_path)
    if not pages:
        logger.warning("no extractable text in %s", pdf_path.name)
        return []
    windows = _group_pages(pages, CHUNK_WINDOW_CHARS)
    logger.info(
        "  extracted %d pages, %d llm windows (concurrency=%d)",
        len(pages), len(windows), CHUNK_CONCURRENCY,
    )
    # Launch all windows concurrently; collect in order so chunk ids are stable.
    results: dict[int, list[Chunk]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, CHUNK_CONCURRENCY)) as ex:
        futures = {
            ex.submit(_window_to_chunks, i, ps, pe, txt, pdf_path, source_label, model): i
            for i, (ps, pe, txt) in enumerate(windows)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                logger.warning("window %d failed: %s", i, e)
                results[i] = []
            done += 1
            logger.info("  window %d/%d → %d chunks", done, len(windows), len(results[i]))
    chunks: list[Chunk] = []
    for i in range(len(windows)):
        chunks.extend(results.get(i, []))
    return chunks


# --- embedding -----------------------------------------------------------


def _embed_payload(c: Chunk) -> str:
    """Compose what gets embedded: enrichment first, then source text.

    Putting section_title, summary, key_questions, and topics up front lets
    caregiver-phrased queries ("how do I help dad with swallowing?") hit the
    right chunk even when the source prose uses clinical language.
    """
    parts: list[str] = [f"Title: {c.section_title}"]
    if c.summary:
        parts.append(f"Summary: {c.summary}")
    if c.key_questions:
        parts.append("Questions this answers:\n- " + "\n- ".join(c.key_questions))
    if c.topics:
        parts.append("Topics: " + ", ".join(c.topics))
    parts.append(f"Source: {c.source_label}")
    parts.append("Content:\n" + c.text)
    return "\n\n".join(parts)


def _embed_texts(texts: list[str]) -> np.ndarray:
    from openai import OpenAI

    api_key = os.environ.get("CRTV_OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("CRTV_OPENAI_API_KEY not set")
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("CRTV_OPENAI_BASE_URL") or None,
        timeout=60.0,
        max_retries=3,
    )
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        for attempt in range(3):
            try:
                resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
                out.extend(d.embedding for d in resp.data)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning("embed batch failed (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        logger.info("  embedded %d/%d", min(i + EMBED_BATCH, len(texts)), len(texts))
    return np.asarray(out, dtype=np.float32)


# --- main orchestration --------------------------------------------------


def reindex(force: bool = False, progress_cb=None) -> dict:
    """Rebuild index incrementally from ``CORPUS_DIR``. Returns a summary dict."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    existing_chunks = _load_chunks()
    existing_vectors = (
        np.load(VECTORS_PATH) if VECTORS_PATH.exists() and existing_chunks else None
    )

    file_to_chunk_idx: dict[str, list[int]] = {}
    for i, c in enumerate(existing_chunks):
        file_to_chunk_idx.setdefault(c["source_file"], []).append(i)

    pdf_paths = sorted(CORPUS_DIR.glob("*.pdf"))
    corpus_names = {p.name for p in pdf_paths}

    def _log(msg: str) -> None:
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    # 1) drop chunks for removed files
    removed = [name for name in manifest.get("files", {}) if name not in corpus_names]
    drop_idxs: set[int] = set()
    for name in removed:
        drop_idxs.update(file_to_chunk_idx.get(name, []))
        manifest["files"].pop(name, None)
        _log(f"removed: {name}")

    # 2) determine which files need re-chunking
    to_reindex: list[Path] = []
    for p in pdf_paths:
        sha = _sha256(p)
        prev = manifest["files"].get(p.name)
        if force or prev is None or prev.get("sha256") != sha:
            to_reindex.append(p)
            if prev is not None:
                drop_idxs.update(file_to_chunk_idx.get(p.name, []))
            manifest["files"][p.name] = {"sha256": sha}
        else:
            _log(f"unchanged: {p.name}")

    # 3) keep surviving chunks + vectors
    kept_chunks: list[dict] = []
    kept_vectors: list[np.ndarray] = []
    for i, c in enumerate(existing_chunks):
        if i in drop_idxs:
            continue
        kept_chunks.append(c)
        if existing_vectors is not None:
            kept_vectors.append(existing_vectors[i])

    # 4) chunk + embed new/changed files
    new_chunks: list[Chunk] = []
    for p in to_reindex:
        label = _source_label(p.name)
        _log(f"chunking: {p.name} ({label})")
        new_chunks.extend(_chunk_pdf(p, label, CHUNK_MODEL))

    new_vecs: np.ndarray | None = None
    if new_chunks:
        _log(f"embedding {len(new_chunks)} new chunks")
        new_vecs = _embed_texts([_embed_payload(c) for c in new_chunks])

    # 5) assemble + persist
    all_chunks = kept_chunks + [asdict(c) for c in new_chunks]
    if kept_vectors and new_vecs is not None:
        all_vecs = np.vstack([np.asarray(kept_vectors, dtype=np.float32), new_vecs])
    elif kept_vectors:
        all_vecs = np.asarray(kept_vectors, dtype=np.float32)
    elif new_vecs is not None:
        all_vecs = new_vecs
    else:
        all_vecs = np.zeros((0, 1536), dtype=np.float32)

    manifest["embed_model"] = EMBED_MODEL
    manifest["chunk_count"] = len(all_chunks)
    _save_chunks_and_vectors(all_chunks, all_vecs)
    _save_manifest(manifest)

    summary = {
        "total_chunks": len(all_chunks),
        "new_chunks": len(new_chunks),
        "files_indexed": [p.name for p in to_reindex],
        "files_removed": removed,
        "files_unchanged": len(corpus_names) - len(to_reindex),
    }
    _log(f"done: {summary}")
    return summary


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-chunk all files")
    args = parser.parse_args()
    result = reindex(force=args.force)
    print(json.dumps(result, indent=2))
