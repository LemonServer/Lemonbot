"""Safe connector implementations shipped with Lemonbot."""

from .base import BaseConnector, Connector
from .errors import (
    ConnectorDependencyError,
    ConnectorDisabledError,
    ConnectorError,
    ConnectorSafetyError,
)
from .fake import FakeConnector
from .personal_wechat import (
    PersonalWeChatConfig,
    PersonalWeChatConnector,
    PersonalWeChatStage,
    PersonalWeChatUIABackend,
    PersonalWeChatUIAConnector,
    UIAPreflightReport,
    UIASendAttempt,
    UIASnapshot,
    personal_wechat_dependency_diagnostic,
)
from .uia_gate import (
    UIAStage,
    effective_uia_stage,
    enrollment_fingerprint,
    promote_uia_stage,
)
from .wechat_uia_win32 import (
    ControlSelector,
    SelectorBundle,
    UIADriverError,
    WindowsWeChatUIABackend,
)
from .wecom import WeComConfig, WeComConnector, map_wecom_frame

__all__ = [
    "BaseConnector",
    "Connector",
    "ConnectorDependencyError",
    "ConnectorDisabledError",
    "ConnectorError",
    "ConnectorSafetyError",
    "ControlSelector",
    "FakeConnector",
    "PersonalWeChatConfig",
    "PersonalWeChatConnector",
    "PersonalWeChatStage",
    "PersonalWeChatUIABackend",
    "PersonalWeChatUIAConnector",
    "SelectorBundle",
    "UIADriverError",
    "UIAPreflightReport",
    "UIASendAttempt",
    "UIASnapshot",
    "UIAStage",
    "WeComConfig",
    "WeComConnector",
    "WindowsWeChatUIABackend",
    "effective_uia_stage",
    "enrollment_fingerprint",
    "map_wecom_frame",
    "personal_wechat_dependency_diagnostic",
    "promote_uia_stage",
]
