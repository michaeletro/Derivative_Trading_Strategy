"""Offline, provenance-preserving order-book research. No broker/order interface."""
VERSION = '0.1.0'


class LabError(ValueError):
    """Sanitized analysis errors that contain no input data or credentials."""
