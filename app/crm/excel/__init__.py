"""Excel CRM workbook: a generated, signed snapshot of PostgreSQL for the sales team.

PostgreSQL stays the source of truth. This package renders it as a workbook
(``exporter``), and - in a later step - validates and merges the sales team's
edits back (``importer``). Nothing here is imported by the AI assistant, the
RAG pipeline or the channel handlers, so a failure in this package can never
interrupt a conversation.

Layering (the first group imports no database or web framework, so it is unit
testable on its own):

* pure:     schema, text, baseline, signing, identity, data, exporter
* database: queries
"""
