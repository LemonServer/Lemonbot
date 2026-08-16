"""Connector-specific exceptions with operator-facing diagnostics."""

from __future__ import annotations


class ConnectorError(RuntimeError):
    """Base class for connector failures."""


class ConnectorDependencyError(ConnectorError):
    """An optional connector dependency is unavailable or incompatible."""


class ConnectorDisabledError(ConnectorError):
    """A connector was intentionally disabled by configuration."""


class ConnectorSafetyError(ConnectorError):
    """A fail-closed safety precondition was not satisfied."""

