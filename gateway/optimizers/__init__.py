"""Deterministic content optimizers.

Each optimizer is a self-contained module implementing a single ``Optimizer``
protocol and registering itself for the content types it handles. Adding a
format must never require editing the pipeline. Optimizers strip structural
noise only: they never summarize, rewrite, or otherwise alter meaning.

Phase 3 adds HTML, JSON, and text. Phase 7 adds PDF, DOCX, and CSV.
"""
