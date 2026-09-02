"""Stable, recoverable errors exposed by Funnel Studio storage."""
from __future__ import annotations


class StudioStorageError(Exception):
    """Base class for local storage failures a caller may diagnose."""


class InvalidSlugError(StudioStorageError, ValueError):
    """A project or funnel slug is not a single safe path component."""


class PathEscapeError(StudioStorageError):
    """A resolved path leaves the configured projects boundary."""


class PathCollisionError(StudioStorageError):
    """An existing filesystem object has an incompatible owner or type."""


class CorruptStateError(StudioStorageError):
    """STATE.json cannot be decoded or does not satisfy its contract."""


class InvalidEventHistoryError(StudioStorageError):
    """The append-only history cannot prove a recoverable snapshot."""


class RevisionConflictError(StudioStorageError):
    """Another writer committed the expected revision first."""

    recoverable = True


class LockUnavailableError(StudioStorageError):
    """The single-writer lock was not available before the timeout."""

    recoverable = True
