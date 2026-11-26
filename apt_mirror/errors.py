"""Errors raised for invalid repository input."""


class ParseError(ValueError):
    """Input cannot be interpreted safely as APT metadata or configuration."""
