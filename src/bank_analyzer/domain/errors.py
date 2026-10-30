"""Typed exceptions shared across layers.

Messages must never contain statement content (descriptions, amounts, account data) so they
are safe to show in the UI and to log.
"""

from __future__ import annotations


class BankAnalyzerError(Exception):
    """Base class for all application errors."""


class ConfigError(BankAnalyzerError):
    """Invalid or unsafe configuration."""


class AmountParseError(BankAnalyzerError, ValueError):
    """A monetary value could not be interpreted unambiguously."""


class PrecisionError(AmountParseError):
    """A value has more decimal places than its currency supports (no silent rounding)."""


class CurrencyMismatch(BankAnalyzerError, ValueError):
    """Two amounts in different currencies were combined."""


class DateParseError(BankAnalyzerError, ValueError):
    """A date could not be interpreted with the chosen format."""


class FileValidationError(BankAnalyzerError, ValueError):
    """An uploaded file was rejected (type, size, signature or structure)."""


class ImportStructureError(BankAnalyzerError, ValueError):
    """A valid file could not be read as a statement table (no header, no delimiter...)."""


class NeedsOcrError(ImportStructureError):
    """A PDF has no text layer (a scan). The UI may offer the optional OCR path."""


class PasswordRequiredError(ImportStructureError):
    """The PDF is password-protected and no password was supplied."""


class WrongPasswordError(ImportStructureError):
    """The supplied password does not open the PDF."""


class MappingError(BankAnalyzerError, ValueError):
    """The column mapping or import choices are inconsistent."""


class SchemaVersionError(BankAnalyzerError):
    """The database schema version is not supported by this build."""


class NotFoundError(BankAnalyzerError):
    """A requested record does not exist."""


class InvalidStateError(BankAnalyzerError):
    """The requested operation is not allowed in the record's current state."""
