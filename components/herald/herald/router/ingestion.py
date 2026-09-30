"""Universal file/picture ingestion for Herald -- roadmap #9a.

Accepts as many file formats as reasonably possible and normalizes each into
one consistent, LLM-usable output shape: extracted text (structured where
the format has structure), a short auto-summary, and metadata.

The image/PDF extraction logic below is adapted (vendored, not imported --
FIMS's `scripts/vision/engines/*.py` use a relative-import package layout
that isn't cleanly importable from another project's process) from the
user's own FIMS project at `Fireworks Store/fims/scripts/vision/engines/`:
  - ocr.py    -> RapidOCR-first, pytesseract-fallback text extraction
  - vlm.py    -> Ollama qwen2.5vl image description
  - codes.py  -> zxing-cpp / pyzbar / OpenCV barcode-QR decoding
  - pdf.py    -> PyMuPDF page rasterization + native text extraction
Each dependency is fully optional (wrapped in try/except at import time) so
ingest_file degrades gracefully rather than requiring the whole FIMS vision
stack to be installed.

Read-only: no file modification, so this doesn't need approval-gate
integration (see roadmap #4).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 512 * 1024  # cap extracted text per file, matches coding_tools.py's truncation posture


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    format: str
    extracted_text: list[str]  # one entry per page/section/row-group; join for a flat view
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list["IngestResult"] = field(default_factory=list)  # populated for archives

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "extracted_text": self.extracted_text,
            "summary": self.summary,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }

    def flat_text(self) -> str:
        return "\n\n".join(self.extracted_text)


def _trunc(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace") + f"\n\n[...truncated -- {len(encoded) - limit} bytes omitted]"


# ---------------------------------------------------------------------------
# Format sniffing
# ---------------------------------------------------------------------------

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),  # also docx/xlsx/pptx (zip containers), disambiguated below
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # RIFF....WEBP, checked more precisely below
    (b"BM", "bmp"),
    (b"\x1f\x8b", "tar.gz"),
]

_ZIP_OFFICE_MARKERS = {
    "word/": "docx",
    "xl/": "xlsx",
    "ppt/": "pptx",
}


def sniff_format(data: bytes, filename_hint: str = "") -> str:
    """Detect actual content format from magic bytes, falling back to the
    filename extension only when the bytes are inconclusive (plain text has
    no reliable magic number)."""
    for sig, fmt in _SIGNATURES:
        if data.startswith(sig):
            if fmt == "zip":
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        names = zf.namelist()
                        for marker, office_fmt in _ZIP_OFFICE_MARKERS.items():
                            if any(n.startswith(marker) for n in names):
                                return office_fmt
                except zipfile.BadZipFile:
                    pass
                return "zip"
            if fmt == "webp" and data[8:12] != b"WEBP":
                continue
            return fmt
        if fmt == "tar.gz":
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"):
                    return "tar.gz"
            except Exception:
                pass

    ext = Path(filename_hint).suffix.lower().lstrip(".")
    if ext:
        return {
            "txt": "text", "md": "text", "py": "text", "json": "json", "csv": "csv",
            "tar": "tar", "jpeg": "jpg",
        }.get(ext, ext)

    # Fall back to a text/binary heuristic.
    sample = data[:8192]
    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    if sample and non_text / len(sample) < 0.05:
        return "text"
    return "unknown"


# ---------------------------------------------------------------------------
# Image extraction (vendored from FIMS's ocr.py / vlm.py / codes.py)
# ---------------------------------------------------------------------------

def _load_pil_image(data: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(data)).convert("RGB")


def _ocr_read_text(pil_image) -> list[dict[str, Any]]:
    """Adapted from FIMS ocr.py's read_text(). RapidOCR first, pytesseract
    fallback; both fully optional."""
    import numpy as np
    regions: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    try:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
        result, _ = engine(np.asarray(pil_image))
        for item in result or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1]).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            confidence = float(item[2]) if len(item) >= 3 else None
            regions.append({"text": text, "confidence": confidence})
    except Exception:
        pass

    if not regions:
        try:
            import shutil
            import pytesseract
            if shutil.which("tesseract"):
                text = pytesseract.image_to_string(pil_image)
                for line in text.splitlines():
                    clean = line.strip()
                    if clean and clean not in seen:
                        seen.add(clean)
                        regions.append({"text": clean, "confidence": None})
        except Exception:
            pass

    return regions


def _vlm_describe(image_bytes: bytes) -> dict[str, Any] | None:
    """Adapted from FIMS vlm.py's describe(). Talks to a local/LAN Ollama
    instance; fully optional -- returns None if unreachable."""
    import base64
    import json as _json
    import os
    from urllib.request import Request, urlopen

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("HERALD_VISION_MODEL", "qwen2.5vl:7b")
    prompt = (
        "Return JSON only describing the image. Include concise keys like "
        "label, and notable text if present."
    )
    payload = {
        "model": model, "prompt": prompt, "format": "json", "stream": False,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
    }
    try:
        req = Request(f"{host}/api/generate", data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=20) as resp:
            body = _json.loads(resp.read().decode("utf-8", errors="replace"))
        response = body.get("response")
        if isinstance(response, str):
            try:
                return _json.loads(response)
            except Exception:
                return {"response": response}
        if isinstance(response, dict):
            return response
    except Exception:
        return None
    return None


def _read_codes(pil_image) -> list[dict[str, Any]]:
    """Adapted from FIMS codes.py's read_codes(). zxing-cpp -> pyzbar ->
    OpenCV QR fallback chain, each fully optional."""
    codes: list[dict[str, Any]] = []

    try:
        import zxingcpp
        for item in zxingcpp.read_barcodes(pil_image) or []:
            data = getattr(item, "text", None)
            kind = getattr(getattr(item, "format", None), "name", "UNKNOWN")
            if data:
                codes.append({"kind": str(kind), "data": str(data)})
        if codes:
            return codes
    except Exception:
        pass

    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        for item in pyzbar_decode(pil_image) or []:
            data = item.data.decode("utf-8", errors="replace") if isinstance(item.data, bytes) else str(item.data)
            codes.append({"kind": str(item.type), "data": data})
        if codes:
            return codes
    except Exception:
        pass

    try:
        import cv2
        import numpy as np
        arr = np.asarray(pil_image)
        detector = cv2.QRCodeDetector()
        text, _points, _ = detector.detectAndDecode(arr)
        if text:
            codes.append({"kind": "QR_CODE", "data": text})
    except Exception:
        pass

    return codes


def ingest_image(data: bytes, fmt: str) -> IngestResult:
    try:
        pil = _load_pil_image(data)
        width, height = pil.size
    except Exception as exc:
        return IngestResult(format=fmt, extracted_text=[], summary=f"[error] could not decode image: {exc}", metadata={})

    ocr_regions = _ocr_read_text(pil)
    codes = _read_codes(pil)
    ocr_text = " ".join(r["text"] for r in ocr_regions)

    vlm_result: dict[str, Any] | None = None
    # Only pay for VLM inference when OCR found little/nothing -- an
    # image that's mostly text doesn't need a separate description pass.
    if len(ocr_text.strip()) < 20:
        vlm_result = _vlm_describe(data)

    summary_parts = []
    if vlm_result:
        label = vlm_result.get("label") or vlm_result.get("response")
        if label:
            summary_parts.append(str(label))
    if ocr_text.strip():
        summary_parts.append(f"contains text: {ocr_text[:200]}")
    if codes:
        summary_parts.append(f"{len(codes)} barcode(s)/QR code(s) detected")
    summary = "; ".join(summary_parts) or "image with no detected text, codes, or description available"

    return IngestResult(
        format=fmt,
        extracted_text=[ocr_text] if ocr_text.strip() else [],
        summary=_trunc(summary, 2000),
        metadata={
            "width": width, "height": height,
            "ocr_region_count": len(ocr_regions),
            "codes": codes,
            "vlm": vlm_result,
        },
    )


# ---------------------------------------------------------------------------
# PDF extraction (vendored from FIMS's pdf.py)
# ---------------------------------------------------------------------------

def ingest_pdf(data: bytes) -> IngestResult:
    try:
        import fitz
    except ImportError:
        return IngestResult(format="pdf", extracted_text=[], summary="[error] PyMuPDF not installed -- cannot extract PDF content", metadata={})

    pages_text: list[str] = []
    scanned_pages: list[int] = []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            page_count = doc.page_count
            for i in range(page_count):
                page = doc.load_page(i)
                text = page.get_text("text")
                if len(text.strip()) < 10:
                    # Near-zero extractable text -- likely a scanned/image page.
                    # Rasterize and route through the image pipeline.
                    pix = page.get_pixmap(dpi=150, alpha=False)
                    mode = "RGB" if pix.alpha == 0 else "RGBA"
                    from PIL import Image
                    pil = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                    buf = io.BytesIO()
                    pil.convert("RGB").save(buf, format="PNG")
                    image_result = ingest_image(buf.getvalue(), "png")
                    pages_text.append(image_result.flat_text())
                    scanned_pages.append(i + 1)
                else:
                    pages_text.append(text)
    except Exception as exc:
        return IngestResult(format="pdf", extracted_text=[], summary=f"[error] PDF extraction failed: {exc}", metadata={})

    combined = "\n".join(t for t in pages_text if t.strip())
    summary = f"PDF, {page_count} page(s)"
    if scanned_pages:
        summary += f", {len(scanned_pages)} rasterized/OCR'd (scanned pages: {scanned_pages[:10]})"

    return IngestResult(
        format="pdf",
        extracted_text=[_trunc(t) for t in pages_text],
        summary=summary,
        metadata={"page_count": page_count, "scanned_pages": scanned_pages},
    )


# ---------------------------------------------------------------------------
# Office documents
# ---------------------------------------------------------------------------

def ingest_docx(data: bytes) -> IngestResult:
    try:
        import docx
    except ImportError:
        return IngestResult(format="docx", extracted_text=[], summary="[error] python-docx not installed", metadata={})
    doc = docx.Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    tables = []
    for table in doc.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        tables.append(rows)
    text_sections = paragraphs + [json.dumps(t) for t in tables]
    return IngestResult(
        format="docx",
        extracted_text=[_trunc(t) for t in text_sections],
        summary=f"Word document, {len(paragraphs)} paragraph(s), {len(tables)} table(s)",
        metadata={"paragraph_count": len(paragraphs), "table_count": len(tables)},
    )


def ingest_xlsx(data: bytes) -> IngestResult:
    try:
        import openpyxl
    except ImportError:
        return IngestResult(format="xlsx", extracted_text=[], summary="[error] openpyxl not installed", metadata={})
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sections = []
    for sheet in wb.worksheets:
        rows = [[str(c) if c is not None else "" for c in row] for row in sheet.iter_rows(values_only=True)]
        sections.append(f"[sheet: {sheet.title}]\n" + "\n".join(",".join(r) for r in rows))
    return IngestResult(
        format="xlsx",
        extracted_text=[_trunc(t) for t in sections],
        summary=f"Excel workbook, {len(wb.worksheets)} sheet(s)",
        metadata={"sheet_names": wb.sheetnames},
    )


def ingest_pptx(data: bytes) -> IngestResult:
    try:
        from pptx import Presentation
    except ImportError:
        return IngestResult(format="pptx", extracted_text=[], summary="[error] python-pptx not installed", metadata={})
    prs = Presentation(io.BytesIO(data))
    sections = []
    for i, slide in enumerate(prs.slides, start=1):
        texts = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
        sections.append(f"[slide {i}]\n" + "\n".join(texts))
    return IngestResult(
        format="pptx",
        extracted_text=[_trunc(t) for t in sections],
        summary=f"PowerPoint presentation, {len(sections)} slide(s)",
        metadata={"slide_count": len(sections)},
    )


# ---------------------------------------------------------------------------
# Plain text / code / CSV / JSON
# ---------------------------------------------------------------------------

def ingest_text(data: bytes, fmt: str) -> IngestResult:
    text = data.decode("utf-8", errors="replace")
    return IngestResult(
        format=fmt, extracted_text=[_trunc(text)],
        summary=f"Plain text/code ({fmt}), {len(text)} characters",
        metadata={"char_count": len(text)},
    )


def ingest_csv(data: bytes) -> IngestResult:
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    return IngestResult(
        format="csv",
        extracted_text=[json.dumps(row) for row in rows[:1000]],
        summary=f"CSV, {len(rows)} row(s)" + (f", header: {rows[0]}" if rows else ""),
        metadata={"row_count": len(rows)},
    )


def ingest_json(data: bytes) -> IngestResult:
    text = data.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        kind = type(parsed).__name__
        summary = f"JSON ({kind})" + (f", {len(parsed)} item(s)" if isinstance(parsed, (list, dict)) else "")
    except Exception:
        summary = "JSON (parse failed -- treated as text)"
    return IngestResult(format="json", extracted_text=[_trunc(text)], summary=summary, metadata={})


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------

def ingest_archive(data: bytes, fmt: str) -> IngestResult:
    children: list[IngestResult] = []
    try:
        if fmt == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                for name in names[:50]:  # cap recursion breadth
                    try:
                        member_bytes = zf.read(name)
                        children.append(ingest_bytes(member_bytes, filename_hint=name))
                    except Exception as exc:
                        children.append(IngestResult(format="unknown", extracted_text=[], summary=f"[error] failed to ingest archive member '{name}': {exc}", metadata={}))
        else:  # tar / tar.gz
            mode = "r:gz" if fmt == "tar.gz" else "r"
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                members = [m for m in tf.getmembers() if m.isfile()][:50]
                for member in members:
                    try:
                        extracted = tf.extractfile(member)
                        if extracted is None:
                            continue
                        children.append(ingest_bytes(extracted.read(), filename_hint=member.name))
                    except Exception as exc:
                        children.append(IngestResult(format="unknown", extracted_text=[], summary=f"[error] failed to ingest archive member '{member.name}': {exc}", metadata={}))
    except Exception as exc:
        return IngestResult(format=fmt, extracted_text=[], summary=f"[error] archive extraction failed: {exc}", metadata={})

    return IngestResult(
        format=fmt, extracted_text=[],
        summary=f"Archive ({fmt}), {len(children)} member(s) ingested",
        metadata={"member_count": len(children)},
        children=children,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_IMAGE_FORMATS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def ingest_bytes(data: bytes, filename_hint: str = "") -> IngestResult:
    """Core entry point: sniff format, dispatch to the matching extractor.
    Never raises -- unknown/unsupported formats return a clear result
    instead of crashing the calling agentic session."""
    try:
        fmt = sniff_format(data, filename_hint)

        if fmt in _IMAGE_FORMATS:
            return ingest_image(data, fmt)
        if fmt == "pdf":
            return ingest_pdf(data)
        if fmt == "docx":
            return ingest_docx(data)
        if fmt == "xlsx":
            return ingest_xlsx(data)
        if fmt == "pptx":
            return ingest_pptx(data)
        if fmt == "csv":
            return ingest_csv(data)
        if fmt == "json":
            return ingest_json(data)
        if fmt in ("zip", "tar", "tar.gz"):
            return ingest_archive(data, fmt)
        if fmt == "text":
            return ingest_text(data, fmt)

        return IngestResult(
            format=fmt or "unknown", extracted_text=[],
            summary=f"Unsupported format '{fmt}' -- no extractor available. File size: {len(data)} bytes.",
            metadata={"size_bytes": len(data), "filename_hint": filename_hint},
        )
    except Exception as exc:  # noqa: BLE001 -- ingestion must never crash the caller
        logger.exception("ingestion: unexpected failure ingesting %r", filename_hint)
        return IngestResult(format="unknown", extracted_text=[], summary=f"[error] ingestion failed: {exc}", metadata={"filename_hint": filename_hint})


def ingest_path(path: str | Path) -> IngestResult:
    p = Path(path)
    data = p.read_bytes()
    return ingest_bytes(data, filename_hint=p.name)
