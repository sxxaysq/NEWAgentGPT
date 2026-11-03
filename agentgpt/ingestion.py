"""Page-preserving extraction and token-bounded chunks."""

import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pypdf import PdfReader


def parse_version(version):
    if version["legacy_text"] is not None:
        return [(version["legacy_text"], {"location_missing": True})]
    path = Path(version["raw_path"])
    if version["title"].lower().endswith(".pdf"):
        reader = PdfReader(path)
        if len(reader.pages) > 500:
            raise ValueError("PDF最多支持500页")
        parts = [(p.extract_text() or "", {"page": i + 1}) for i, p in enumerate(reader.pages)]
    else:
        text = path.read_bytes().decode("utf-8-sig")
        parts = []
        headings, lines, start, offset = [], [], 0, 0
        for line in text.splitlines(keepends=True):
            heading = re.match(r"^(#{1,6})\s+(.+)", line)
            if heading and version["title"].lower().endswith(".md"):
                if lines:
                    parts.append(
                        ("".join(lines), {"section": " / ".join(headings), "file_start": start})
                    )
                headings = headings[: len(heading[1]) - 1] + [heading[2].strip()]
                lines, start = [], offset
            lines.append(line)
            offset += len(line)
        if lines:
            parts.append(("".join(lines), {"section": " / ".join(headings), "file_start": start}))
    if not any(text.strip() for text, _ in parts):
        raise ValueError("文件没有可检索文本；扫描PDF需先OCR")
    if sum(len(text) for text, _ in parts) > 2_000_000:
        raise ValueError("解析文本超过200万字符")
    return parts


def chunk_parts(version_id, parts, tokenizer=None, max_tokens=384, overlap=48):
    chunks = []
    for text, location in parts:
        if not text.strip():
            continue
        # The production tokenizer is cloned with truncation disabled by DocumentIndex.
        offsets = (
            tokenizer.encode(text, add_special_tokens=False).offsets
            if tokenizer
            else [(i, i + 1) for i in range(len(text))]
        )
        for start in range(0, len(offsets), max_tokens - overlap):
            window = offsets[start : start + max_tokens]
            if not window:
                break
            lo, hi = window[0][0], window[-1][1]
            value = text[lo:hi]
            if not value.strip():
                continue
            if tokenizer and len(tokenizer.encode(value).ids) > max_tokens + 8:
                raise ValueError("分块超过Embedding输入预算")
            ordinal = len(chunks)
            chunks.append(
                {
                    "id": uuid5(NAMESPACE_URL, f"{version_id}:{ordinal}").hex,
                    "version_id": version_id,
                    "ordinal": ordinal,
                    "text": value,
                    "location": {**location, "start": lo, "end": hi},
                }
            )
            if start + max_tokens >= len(offsets):
                break
    if not chunks:
        raise ValueError("文件没有可检索的分块")
    return chunks
