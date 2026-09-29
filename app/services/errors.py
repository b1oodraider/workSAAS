class NotFound(Exception):
    """Entity doesn't exist or doesn't belong to the user (never leak which)."""


class ValidationFailed(Exception):
    """User input is invalid; message is user-facing."""
