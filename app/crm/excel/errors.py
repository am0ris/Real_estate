"""Exceptions of the CRM workbook feature.

Kept in a module that imports nothing heavy (no openpyxl, no SQLAlchemy) so the
API routes can catch them while the packages that do the real work are only
loaded when a CRM request arrives. That is what lets the application start, and
the assistant keep answering, on a server where the workbook dependencies are
missing or broken.
"""

from __future__ import annotations


class ImportRejected(ValueError):
    """The upload cannot be used at all (the message is safe to show the user)."""


class WorkbookRejected(ValueError):
    """The file is not a workbook this service exported, or it was altered."""


class WorkbookBusy(RuntimeError):
    """Another export or import is still running."""
