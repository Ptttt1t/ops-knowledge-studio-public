# 知识来源目录

可以把待加工的 TXT、Markdown、CSV、JSON、YAML、DOCX 文档放到这里，再通过 CLI 的 `ingest --file` 导入。

网页上传的原始文件保存在 `uploads/`，用于证据追溯；该目录默认不提交到 Git。

PDF 文本层使用 `pypdf` 提取，扫描 PDF 和 PNG/JPG/TIFF 等图片使用 PaddleOCR 识别。
