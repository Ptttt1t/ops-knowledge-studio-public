from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
import zipfile
from xml.etree import ElementTree


class DocumentError(RuntimeError):
    """Raised when a source document cannot be read safely."""


@dataclass(frozen=True)
class SourceDocument:
    name: str
    source_type: str
    source_ref: str
    content: str


@dataclass(frozen=True)
class DocumentChunk:
    index: int
    char_start: int
    char_end: int
    content: str


@dataclass(frozen=True)
class EvidenceSpan:
    """An exact source span derived from a model-proposed quote."""

    start: int
    end: int
    quote: str
    match_method: str
    similarity: float


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".docx", ".pdf"}
OCR_MIN_TEXT_CHARS = 20
OCR_MIN_CONFIDENCE = 0.45

_OCR_ENGINE = None
_OCR_INIT_LOCK = threading.Lock()
_OCR_RUN_LOCK = threading.Lock()


def document_capabilities() -> dict[str, object]:
    return {
        "supported_extensions": sorted(SUPPORTED_DOCUMENT_EXTENSIONS),
        "pdf_text_extraction": importlib.util.find_spec("pypdf") is not None,
        "pdf_page_rendering": importlib.util.find_spec("fitz") is not None,
        "paddleocr": importlib.util.find_spec("paddleocr") is not None,
        "ocr_languages": ["简体中文", "英文"],
        "ocr_model": "PP-OCRv4 mobile CPU",
    }

_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "…": "...",
    }
)
_MARKDOWN_DIRECTIVE = re.compile(r"\{\{[%<].*?[>%]\}\}", re.DOTALL)


def _normalized_with_source_map(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    source_map: list[int] = []
    for source_index, source_char in enumerate(text):
        expanded = unicodedata.normalize("NFKC", source_char).translate(
            _PUNCTUATION_TRANSLATION
        )
        for char in expanded.casefold():
            if char.isspace():
                if normalized and normalized[-1] != " ":
                    normalized.append(" ")
                    source_map.append(source_index)
            else:
                normalized.append(char)
                source_map.append(source_index)
    while normalized and normalized[-1] == " ":
        normalized.pop()
        source_map.pop()
    return "".join(normalized), source_map


def _normalized(text: str) -> str:
    return _normalized_with_source_map(text)[0]


def ground_evidence_quote(source_text: str, proposed_quote: str) -> EvidenceSpan | None:
    """Resolve an LLM-proposed quote to an exact, contiguous source span.

    Exact and normalization-only matches are preferred. A conservative anchor
    fallback tolerates presentation markup between stable quote boundaries but
    rejects broad summaries and non-contiguous ellipsis joins.
    """

    candidate = proposed_quote.strip()
    if not source_text or not candidate:
        return None

    exact_start = source_text.find(candidate)
    if exact_start >= 0:
        exact_end = exact_start + len(candidate)
        return EvidenceSpan(
            start=exact_start,
            end=exact_end,
            quote=source_text[exact_start:exact_end],
            match_method="exact",
            similarity=1.0,
        )

    source_normalized, source_map = _normalized_with_source_map(source_text)
    candidate_normalized = _normalized(candidate)
    if not source_normalized or not candidate_normalized:
        return None

    normalized_start = source_normalized.find(candidate_normalized)
    if normalized_start >= 0:
        normalized_end = normalized_start + len(candidate_normalized)
        source_start = source_map[normalized_start]
        source_end = source_map[normalized_end - 1] + 1
        return EvidenceSpan(
            start=source_start,
            end=source_end,
            quote=source_text[source_start:source_end],
            match_method="normalized",
            similarity=1.0,
        )

    if len(candidate_normalized) < 32:
        return None

    best: tuple[float, int, int] | None = None
    for anchor_size in (32, 24, 16):
        if len(candidate_normalized) <= anchor_size * 2:
            continue
        prefix = candidate_normalized[:anchor_size]
        suffix = candidate_normalized[-anchor_size:]
        source_start_normalized = source_normalized.find(prefix)
        if source_start_normalized < 0:
            continue
        suffix_start = source_normalized.find(
            suffix, source_start_normalized + anchor_size
        )
        if suffix_start < 0:
            continue
        source_end_normalized = suffix_start + anchor_size
        span_normalized = source_normalized[
            source_start_normalized:source_end_normalized
        ]
        length_ratio = len(span_normalized) / len(candidate_normalized)
        if not 0.6 <= length_ratio <= 2.0:
            continue
        raw_similarity = SequenceMatcher(
            None, candidate_normalized, span_normalized, autojunk=False
        ).ratio()
        without_directives = _MARKDOWN_DIRECTIVE.sub("", span_normalized)
        cleaned_similarity = SequenceMatcher(
            None, candidate_normalized, without_directives, autojunk=False
        ).ratio()
        similarity = max(raw_similarity, cleaned_similarity)
        if similarity < 0.78:
            continue
        if best is None or similarity > best[0]:
            best = (similarity, source_start_normalized, source_end_normalized)

    if best is None:
        return None
    similarity, normalized_start, normalized_end = best
    source_start = source_map[normalized_start]
    source_end = source_map[normalized_end - 1] + 1
    return EvidenceSpan(
        start=source_start,
        end=source_end,
        quote=source_text[source_start:source_end],
        match_method="anchored",
        similarity=similarity,
    )


def _read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError(f"无法识别文本编码: {path.name}")


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentError(f"DOCX 文件损坏或格式不受支持: {path.name}") from exc

    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _get_ocr_engine():
    """Create one lightweight Chinese/English OCR pipeline on first use."""

    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    with _OCR_INIT_LOCK:
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE
        cache_dir = Path(__file__).resolve().parents[1] / "data" / "paddlex_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(cache_dir))
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR  # type: ignore

            _OCR_ENGINE = PaddleOCR(
                lang="ch",
                ocr_version="PP-OCRv4",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="cpu",
                enable_mkldnn=False,
            )
        except Exception as exc:
            raise DocumentError(
                "PaddleOCR 初始化失败，请检查 paddlepaddle、paddleocr 和模型下载状态："
                f"{exc}"
            ) from exc
    return _OCR_ENGINE


def _extract_ocr_lines(results: object) -> list[str]:
    lines: list[str] = []
    for result in results if isinstance(results, list) else []:
        try:
            texts = list(result.get("rec_texts", []))
            scores = list(result.get("rec_scores", []))
        except (AttributeError, TypeError):
            try:
                payload = result.json.get("res", {})
                texts = list(payload.get("rec_texts", []))
                scores = list(payload.get("rec_scores", []))
            except (AttributeError, TypeError):
                continue
        if len(scores) < len(texts):
            scores.extend([1.0] * (len(texts) - len(scores)))
        for text, score in zip(texts, scores):
            value = str(text).strip()
            try:
                confidence = float(score)
            except (TypeError, ValueError):
                confidence = 0.0
            if value and confidence >= OCR_MIN_CONFIDENCE:
                lines.append(value)
    return lines


def _ocr_input(value: object) -> str:
    engine = _get_ocr_engine()
    try:
        with _OCR_RUN_LOCK:
            results = engine.predict(value)
    except Exception as exc:
        raise DocumentError(f"PaddleOCR 识别失败：{exc}") from exc
    return "\n".join(_extract_ocr_lines(results)).strip()


def _ocr_pdf_page(path: Path, page_index: int) -> str:
    try:
        import fitz  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise DocumentError(
            "扫描 PDF OCR 需要 PyMuPDF 和 numpy，请先安装对应依赖。"
        ) from exc
    try:
        with fitz.open(str(path)) as document:
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            return _ocr_input(image)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"PDF 第 {page_index + 1} 页渲染失败：{exc}") from exc


def _read_image(path: Path) -> str:
    text = _ocr_input(str(path))
    if not text:
        raise DocumentError(f"OCR 未从图像中识别到有效文字：{path.name}")
    return f"[OCR 图像：{path.name}]\n{text}"


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise DocumentError(
            "读取 PDF 需要可选依赖 pypdf；可先转为 TXT/Markdown，或安装 pypdf。"
        ) from exc
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise DocumentError(f"PDF 文件损坏、加密或格式不受支持：{path.name}") from exc
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        visible_chars = len(re.sub(r"\s+", "", text))
        if visible_chars >= OCR_MIN_TEXT_CHARS:
            pages.append(f"[第 {index} 页 | PDF 文本层]\n{text}")
            continue
        ocr_text = _ocr_pdf_page(path, index - 1)
        if ocr_text:
            pages.append(f"[第 {index} 页 | PaddleOCR]\n{ocr_text}")
        elif text:
            pages.append(f"[第 {index} 页 | PDF 文本层]\n{text}")
    return "\n\n".join(pages)


def read_document(path: Path) -> SourceDocument:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise DocumentError(f"文件不存在: {resolved}")
    suffix = resolved.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        content = _read_text_with_fallback(resolved)
        if suffix == ".json":
            try:
                content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
    elif suffix == ".docx":
        content = _read_docx(resolved)
    elif suffix == ".pdf":
        content = _read_pdf(resolved)
    elif suffix in IMAGE_EXTENSIONS:
        content = _read_image(resolved)
    else:
        raise DocumentError(
            f"暂不支持 {suffix or '无扩展名'}；支持 TXT、Markdown、CSV、JSON、YAML、"
            "DOCX、PDF 以及 PNG/JPG/TIFF 等常见图片。"
        )
    content = content.strip()
    if not content:
        raise DocumentError(f"文档没有可提取的文本: {resolved.name}")
    return SourceDocument(
        name=resolved.name,
        source_type=suffix.lstrip(".") or "text",
        source_ref=str(resolved),
        content=content,
    )


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[DocumentChunk]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("分片参数无效")
    chunks: list[DocumentChunk] = []
    start = 0
    index = 0
    length = len(text)

    while start < length:
        proposed_end = min(length, start + chunk_size)
        end = proposed_end
        if proposed_end < length:
            boundary = text.rfind("\n", start + chunk_size // 2, proposed_end)
            if boundary > start:
                end = boundary + 1
        raw = text[start:end]
        left_trim = len(raw) - len(raw.lstrip())
        right_trim = len(raw) - len(raw.rstrip())
        content_start = start + left_trim
        content_end = end - right_trim
        content = text[content_start:content_end]
        if content:
            chunks.append(
                DocumentChunk(
                    index=index,
                    char_start=content_start,
                    char_end=content_end,
                    content=content,
                )
            )
            index += 1
        if end >= length:
            break
        next_start = max(0, end - overlap)
        start = next_start if next_start > start else end
    return chunks
