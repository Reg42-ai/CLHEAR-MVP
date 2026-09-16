"""Compatibility imports for the former company-specific registry module.

The company-independent declarations and worker seed implementation now live in
``source_registry``. Existing source IDs and historical records are preserved.
"""
from app.clhear.l1.source_registry import *  # noqa: F403
