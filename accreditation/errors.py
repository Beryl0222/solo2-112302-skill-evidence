"""Errors raised by the accreditation workflow."""


class AccreditationError(Exception):
    """Base class for predictable domain errors."""


class ValidationError(ValueError, AccreditationError):
    """Input data does not satisfy the supported schema."""


class PermissionDeniedError(PermissionError, AccreditationError):
    """The caller's role cannot make this kind of decision."""


class NotFoundError(KeyError, AccreditationError):
    """A referenced case object does not exist."""


class QualificationError(AccreditationError):
    """A requested sign-off is not supported by the active evidence."""
