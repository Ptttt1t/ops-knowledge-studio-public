from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from knowledge_platform.documents import read_document


ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = ROOT / "tmp" / "pdfs"
ARTIFACT_DIR = ROOT / "artifacts"


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    raise RuntimeError("没有找到可用于 OCR 测试的系统字体")


def build_scanned_pdf() -> tuple[Path, Path, Path]:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    draw.text((120, 110), "NE-A 补丁升级操作单", font=_font(64), fill="black")
    body = (
        "适用版本：V3.1 升级到 V3.1-P2\n\n"
        "执行前检查：\n"
        "1. 确认主备状态正常，当前无严重告警。\n"
        "2. 完成配置备份并校验备份文件。\n\n"
        "操作步骤：\n"
        "1. 先升级备用节点。\n"
        "2. 验证正常后执行主备切换。\n"
        "3. 升级原主节点。\n\n"
        "回退方案：卸载补丁并恢复升级前配置。\n"
        "验证要求：连续观察十五分钟无新增严重告警。"
    )
    draw.multiline_text((120, 260), body, font=_font(40), fill="black", spacing=22)

    source_png = TEMP_DIR / "ocr_scan_source.png"
    pdf_path = TEMP_DIR / "ocr_scan_test.pdf"
    rendered_png = TEMP_DIR / "ocr_scan_rendered.png"
    image.save(source_png)
    image.save(pdf_path, "PDF", resolution=150.0)

    with fitz.open(str(pdf_path)) as document:
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(1.0, 1.0), alpha=False)
        pixmap.save(str(rendered_png))
    return pdf_path, rendered_png, source_png


def build_text_layer_pdf() -> Path:
    pdf_path = TEMP_DIR / "text_layer_test.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_textbox(
            fitz.Rect(72, 72, 520, 320),
            "NE-B Maintenance Procedure\n"
            "Before upgrade, verify the standby node and create a configuration backup.\n"
            "Rollback: uninstall the patch and restore the previous configuration.\n"
            "Validation: observe alarms for fifteen minutes after the change.",
            fontsize=14,
        )
        document.save(str(pdf_path))
    return pdf_path


def main() -> int:
    pdf_path, rendered_png, source_png = build_scanned_pdf()
    text_pdf_path = build_text_layer_pdf()
    document = read_document(pdf_path)
    image_document = read_document(source_png)
    text_document = read_document(text_pdf_path)
    expected = ["NE-A", "V3.1-P2", "配置备份", "回退"]
    matched = [term for term in expected if term in document.content]
    image_matched = [term for term in expected if term in image_document.content]
    text_layer_ok = (
        "[第 1 页 | PDF 文本层]" in text_document.content
        and "configuration" in text_document.content
        and "backup" in text_document.content
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    extracted_path = ARTIFACT_DIR / "ocr_smoke_test.txt"
    extracted_path.write_text(
        "=== scanned PDF ===\n"
        + document.content
        + "\n\n=== image ===\n"
        + image_document.content
        + "\n\n=== text-layer PDF ===\n"
        + text_document.content,
        encoding="utf-8",
    )
    print(f"pdf={pdf_path}")
    print(f"rendered={rendered_png}")
    print(f"extracted={extracted_path}")
    print(f"pdf_ocr_matched={len(matched)}/{len(expected)}:{','.join(matched)}")
    print(
        f"image_ocr_matched={len(image_matched)}/{len(expected)}:"
        f"{','.join(image_matched)}"
    )
    print(f"text_layer_pdf={text_layer_ok}")
    print(document.content)
    return 0 if len(matched) >= 3 and len(image_matched) >= 3 and text_layer_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
