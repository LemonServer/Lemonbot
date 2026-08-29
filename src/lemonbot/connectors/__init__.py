"""Safe connector implementations shipped with Lemonbot."""

from .base import BaseConnector, Connector
from .errors import (
    ConnectorDependencyError,
    ConnectorDisabledError,
    ConnectorError,
    ConnectorSafetyError,
)
from .fake import FakeConnector
from .wechat_atspi import AtspiEnrollment, AtspiObserveConnector, AtspiTranscriptItem

__all__ = [
    "AtspiEnrollment",
    "AtspiObserveConnector",
    "AtspiTranscriptItem",
    "BaseConnector",
    "Connector",
    "ConnectorDependencyError",
    "ConnectorDisabledError",
    "ConnectorError",
    "ConnectorSafetyError",
    "FakeConnector",
]
