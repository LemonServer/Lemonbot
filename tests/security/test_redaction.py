from __future__ import annotations

import logging
import sys

from lemonbot.security.redaction import RedactingFilter, SecretRedactor


def test_redacts_known_and_structured_credentials() -> None:
    redactor = SecretRedactor(["super-secret-value"])
    result = redactor.redact(
        "Authorization: "
        + "Bearer abcdefghijkl api_key=super-secret-value "
        + "sk-"
        + "abcdefghijklmnop"
    )
    assert "super-secret-value" not in result
    assert "abcdefghijklmnop" not in result
    assert result.count("[REDACTED]") >= 3


def test_redacts_json_quoted_values_basic_auth_and_private_keys() -> None:
    redactor = SecretRedactor()
    private_key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\nnot-a-real-key\n-----END "
        + "PRIVATE KEY-----"
    )
    value = (
        '{"client_secret": "secret with spaces", "access_token":"token-value"} '
        + "Authorization: "
        + "Basic dXNlcjpwYXNz "
        + private_key
    )

    result = redactor.redact(value)

    assert "secret with spaces" not in result
    assert "token-value" not in result
    assert "dXNlcjpwYXNz" not in result
    assert "not-a-real-key" not in result


def test_logging_filter_preserves_percent_formatting_before_redaction() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="attempt=%d api_key=%s",
        args=(3, "sensitive-value"),
        exc_info=None,
    )

    assert RedactingFilter().filter(record)
    assert record.getMessage() == "attempt=3 api_key=[REDACTED]"


def test_logging_filter_redacts_exception_traceback() -> None:
    try:
        raise RuntimeError("api_key=exception-secret-value")
    except RuntimeError:
        exception_info = sys.exc_info()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed",
        args=(),
        exc_info=exception_info,
    )

    assert RedactingFilter().filter(record)
    rendered = logging.Formatter("%(message)s").format(record)
    assert "exception-secret-value" not in rendered
    assert "[REDACTED]" in rendered
