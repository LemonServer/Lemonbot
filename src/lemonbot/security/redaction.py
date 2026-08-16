from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable

_DEFAULT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|client[_-]?secret|access[_-]?token|"
        r"refresh[_-]?token|secret|token|aeskey|response_url)[\"']?\s*[=:]\s*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
)


class SecretRedactor:
    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        self._known = tuple(value for value in known_secrets if len(value) >= 6)

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in self._known:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _DEFAULT_PATTERNS:
            text = pattern.sub(
                lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]",
                text,
            )
        return text


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or SecretRedactor()

    def filter(self, record: logging.LogRecord) -> bool:
        # Render first so numeric/structured %-format arguments keep their
        # original types, then replace the record with one inert redacted string.
        # Redacting each argument independently can leak separators/quotes and
        # also breaks formatters such as ``%d`` by converting integers to text.
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = self._redactor.redact(rendered)
        record.args = ()
        if record.exc_info is not None:
            rendered_exception = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redactor.redact(rendered_exception)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True


def configure_logging(level: str, known_secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(SecretRedactor(known_secrets)))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
