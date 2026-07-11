"""Format extractors, one module per family.

Adding PPTX, XLSX, EPUB, RTF or EML means adding an extractor here and one line in
``build_document_registry`` — nothing in the pipeline or the gateway core changes.
"""

from gateway.documents.extractors.docx import DocxExtractor
from gateway.documents.extractors.pdf import PdfExtractor
from gateway.documents.extractors.text_formats import (
    CsvExtractor,
    HtmlExtractor,
    MarkdownExtractor,
    XmlExtractor,
)

__all__ = [
    "CsvExtractor",
    "DocxExtractor",
    "HtmlExtractor",
    "MarkdownExtractor",
    "PdfExtractor",
    "XmlExtractor",
]
