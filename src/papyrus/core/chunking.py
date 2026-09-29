import hashlib
import json
import re
from pathlib import Path

from papyrus.core.constants import CHUNK_INDEX_FILE, FULLTEXT_MD, SCHEMA_VERSION


_NON_CONTENT_PATTERNS = [
    r"(?i)^#+\s*(open access|edited by|reviewed by|citation|received|revised|accepted|published|correspondence)[\s:#]*$",
    r"(?i)^#+\s*type\s+\w+\s+review",
    r"(?i)^#+\s*(references|bibliography|acknowledg?ments|author contributions|funding|conflict[s]? of interest|competing interest|disclosure|supplementary|data availability|code availability|ethics|copyright|license|permissions|consent)[\s:#]*$",
    r"^#+\s*doi\s*:",
    r"^#+\s*https?://",
]


def _is_content_section(title: str) -> bool:
    for pat in _NON_CONTENT_PATTERNS:
        if re.search(pat, title):
            return False
    return True


def chunk_markdown(
    text: str,
    target_bytes: int = 2048,
    overlap_bytes: int = 256,
    max_chunks: int = 80,
) -> list[dict]:
    """Split markdown into section-aligned chunks.

    Every chunk records the exact character span ("start"/"end") of its text
    within the source string: slicing text[start:end] returns the chunk text
    verbatim. Offsets are Python str indices (characters), never bytes, so
    multi-byte UTF-8 content cannot shift them (issue #3-D).
    """
    sections = _split_sections(text)
    content_sections = [(t, b, s) for t, b, s in sections if _is_content_section(t)]
    if not content_sections:
        content_sections = sections
    chunks: list[dict] = []
    chunk_id = 0

    for section_title, section_body, section_start in content_sections:
        if not section_body.strip():
            continue
        pos = 0
        body_len = len(section_body)
        while pos < body_len and len(chunks) < max_chunks:
            end = min(pos + target_bytes, body_len)
            window = section_body[pos:end]
            chunk_text = window.strip()
            if not chunk_text:
                pos = end
                continue
            # Locate the stripped text exactly inside the source string.
            start_in_text = section_start + pos + (len(window) - len(window.lstrip()))
            chunks.append({
                "chunk_id": f"{chunk_id:05d}",
                "text": chunk_text,
                "section": section_title,
                "start": start_in_text,
                "end": start_in_text + len(chunk_text),
            })
            chunk_id += 1
            pos = end - overlap_bytes
            if pos <= 0:
                pos = end

    return chunks


def _split_sections(text: str) -> list[tuple[str, str, int]]:
    """Return (title, body, body_start) with body_start the absolute
    character offset of the body within text."""
    heading_pat = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)
    matches = list(heading_pat.finditer(text))
    if not matches:
        return [("", text, 0)]

    sections: list[tuple[str, str, int]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        sections.append((title, body, start))
    return sections


def write_chunks(
    pmid: str,
    fulltext_path: Path,
    record_dir: Path,
    target_bytes: int = 2048,
    overlap_bytes: int = 256,
    max_chunks: int = 80,
):
    with open(fulltext_path) as f:
        text = f.read()

    chunks = chunk_markdown(text, target_bytes, overlap_bytes, max_chunks)
    source_hash = hashlib.sha256(text.encode()).hexdigest()

    chunk_index = {
        "schema_version": SCHEMA_VERSION,
        "source_file": FULLTEXT_MD,
        "source_hash": f"sha256:{source_hash}",
        "chunk_method": "section",
        "chunks": [],
    }

    for chunk in chunks:
        chunk_index["chunks"].append({
            "chunk_id": chunk["chunk_id"],
            "start_char": chunk["start"],
            "end_char": chunk["end"],
            "section": chunk["section"],
        })

    with open(record_dir / "chunk_index.json", "w") as f:
        json.dump(chunk_index, f, indent=2)

    return chunks
