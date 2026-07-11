"""Pure text -> Markdown converters, shared by two callers.

A format like XML or CSV can reach the gateway two ways: pasted as text into a
prompt (a text ``Segment``, handled by a ``Transformer``), or uploaded as a file
(decoded bytes, handled by a ``DocumentExtractor``). Both must produce the *same*
Markdown, so the conversion lives here, once, and both callers import it. Anything
else would let the two paths drift and break idempotency across them.
"""

from gateway.documents.convert.csv_table import csv_to_markdown
from gateway.documents.convert.xml_tree import xml_to_markdown

__all__ = ["csv_to_markdown", "xml_to_markdown"]
