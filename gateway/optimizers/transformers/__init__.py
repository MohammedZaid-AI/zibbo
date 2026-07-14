"""Concrete transformers, one module per content type.

Adding a format — PDF, DOCX, CSV, XML, email — means adding one module here and
one line in ``build_transformer_registry``. Nothing else in the gateway changes.
"""

from gateway.optimizers.transformers.html import HtmlTransformer
from gateway.optimizers.transformers.json import JsonTransformer
from gateway.optimizers.transformers.prompt import PromptTransformer
from gateway.optimizers.transformers.text import TextTransformer, normalize_text

__all__ = [
    "HtmlTransformer",
    "JsonTransformer",
    "PromptTransformer",
    "TextTransformer",
    "normalize_text",
]
