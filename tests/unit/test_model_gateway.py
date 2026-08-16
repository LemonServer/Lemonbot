from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from functools import wraps
from typing import Any

import httpx
import pytest

from lemonbot.domain.models import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolManifest,
)
from lemonbot.models import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_PRO_MODEL,
    ZHIPU_VISION_MODEL,
    BudgetLimits,
    BudgetManager,
    MappingSecretStore,
    ModelPrice,
    OpenAICompatibleBackend,
    PriceNotConfiguredError,
    ProviderConfig,
    SanitizedImage,
    ToolCallValidationError,
    ToolSchemaRegistry,
    VisionProviderConfig,
    VisionRequest,
    VisionService,
    ZhipuVisionAdapter,
)


def async_test(function: Any) -> Any:
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return run


def prices() -> dict[tuple[str, str], ModelPrice]:
    unit = ModelPrice(input_per_million=Decimal("1"), output_per_million=Decimal("2"))
    return {
        ("deepseek", DEEPSEEK_FLASH_MODEL): unit,
        ("deepseek", DEEPSEEK_PRO_MODEL): unit,
        ("zhipu", ZHIPU_VISION_MODEL): unit,
    }


def budget(*, configured_prices: dict[tuple[str, str], ModelPrice] | None = None) -> BudgetManager:
    return BudgetManager(
        limits=BudgetLimits(daily=Decimal("10"), monthly=Decimal("100")),
        prices=prices() if configured_prices is None else configured_prices,
    )


def weather_manifest() -> ToolManifest:
    return ToolManifest(
        name="weather.read",
        description="Read weather without a side effect",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        action_kind="network.read",
    )


@async_test
async def test_token_estimate_covers_unicode_and_complete_tool_call_payload() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    backend = OpenAICompatibleBackend.deepseek(
        secret_store=MappingSecretStore({"deepseek_api_key": "unused"}),
        budget=budget(),
        client=client,
    )
    unicode_text = "中文🙂" * 200
    plain = ModelMessage(role=MessageRole.USER, content=unicode_text)
    plain_estimate = backend.count_tokens((plain,))
    assert plain_estimate >= len(unicode_text.encode("utf-8"))

    arguments = {"query": "工具参数🙂" * 2_000}
    with_tool_call = ModelMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=(ToolCall(call_id="call-large", name="search", arguments=arguments),),
        reasoning_content="temporary continuation",
    )
    assert backend.count_tokens((with_tool_call,)) > len(arguments["query"].encode("utf-8"))
    await client.aclose()


@async_test
async def test_vision_estimate_reserves_image_and_utf8_text() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    adapter = ZhipuVisionAdapter(
        secret_store=MappingSecretStore({"zhipu_api_key": "unused"}),
        budget=budget(),
        config=VisionProviderConfig(image_token_reserve=16_384),
        client=client,
    )
    prompt = "看看🙂"
    ocr = "截图文字" * 100
    request = VisionRequest(
        image=SanitizedImage.from_bytes(
            b"sanitized", media_type="image/png", width=1, height=1
        ),
        prompt=prompt,
        ocr_text=ocr,
    )
    assert adapter._estimate_prompt_tokens(request) == (
        16_384 + len(prompt.encode("utf-8")) + len(ocr.encode("utf-8")) + 512
    )
    await client.aclose()


@pytest.mark.parametrize(
    "raw",
    ['{"city":"a","city":"b"}', '{"temperature":NaN}'],
)
def test_tool_argument_parser_rejects_ambiguous_json(raw: str) -> None:
    with pytest.raises(ToolCallValidationError):
        ToolSchemaRegistry.parse_arguments(raw)


@async_test
async def test_deepseek_defaults_routing_tool_validation_and_thinking_continuation() -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{DEEPSEEK_BASE_URL}/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "ephemeral chain state",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "weather.read",
                                            "arguments": '{"city":"Shanghai"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "sunny"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend.deepseek(
        secret_store=MappingSecretStore({"deepseek_api_key": "test-secret"}),
        budget=budget(),
        client=client,
    )
    first = await backend.generate(
        ModelRequest(
            messages=(ModelMessage(role=MessageRole.USER, content="plan it"),),
            tools=(weather_manifest(),),
            deep=True,
        )
    )
    assert first.model == DEEPSEEK_PRO_MODEL
    assert first.tool_calls[0].arguments == {"city": "Shanghai"}
    assert first.reasoning_content == "ephemeral chain state"
    assert "ephemeral chain state" not in repr(first)
    assert seen[0]["thinking"] == {"type": "enabled"}

    second = await backend.generate(
        ModelRequest(
            messages=(
                ModelMessage(role=MessageRole.USER, content="plan it"),
                ModelMessage(
                    role=MessageRole.ASSISTANT,
                    content=None,
                    tool_calls=first.tool_calls,
                    reasoning_content=first.reasoning_content,
                ),
                ModelMessage(
                    role=MessageRole.TOOL,
                    content='{"temperature":25}',
                    tool_call_id="call-1",
                ),
            ),
            tools=(weather_manifest(),),
            deep=True,
        )
    )
    assert second.content == "sunny"
    assistant_payload = seen[1]["messages"][1]
    assert assistant_payload["reasoning_content"] == "ephemeral chain state"
    await client.aclose()


@async_test
async def test_flash_is_default_and_invalid_tool_arguments_fail_closed() -> None:
    seen_model: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_model.append(body["model"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "bad",
                                    "type": "function",
                                    "function": {
                                        "name": "weather.read",
                                        "arguments": '{"city":"Shanghai","execute":true}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend.deepseek(
        secret_store=MappingSecretStore({"deepseek_api_key": "secret"}),
        budget=budget(),
        client=client,
    )
    with pytest.raises(ToolCallValidationError):
        await backend.generate(
            ModelRequest(
                messages=(ModelMessage(role=MessageRole.USER, content="weather"),),
                tools=(weather_manifest(),),
            )
        )
    assert seen_model == [DEEPSEEK_FLASH_MODEL]
    await client.aclose()


@async_test
async def test_startup_model_catalogue_verifies_both_deterministic_routes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == f"{DEEPSEEK_BASE_URL}/models"
        assert request.headers["authorization"] == "Bearer startup-secret"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": DEEPSEEK_FLASH_MODEL},
                    {"id": DEEPSEEK_PRO_MODEL},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend.deepseek(
        secret_store=MappingSecretStore({"deepseek_api_key": "startup-secret"}),
        budget=budget(),
        client=client,
    )
    assert await backend.verify_models() == (
        DEEPSEEK_FLASH_MODEL,
        DEEPSEEK_PRO_MODEL,
    )
    await client.aclose()


@async_test
async def test_startup_model_catalogue_fails_closed_when_configured_model_is_absent() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": DEEPSEEK_FLASH_MODEL}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend.deepseek(
        secret_store=MappingSecretStore({"deepseek_api_key": "startup-secret"}),
        budget=budget(),
        client=client,
    )
    from lemonbot.models.gateway import ModelProtocolError

    with pytest.raises(ModelProtocolError, match=DEEPSEEK_PRO_MODEL):
        await backend.verify_models()
    await client.aclose()


@async_test
async def test_local_openai_compatible_backend_can_run_without_a_fake_api_key() -> None:
    seen_authorization: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "local-fast"}, {"id": "local-deep"}]},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "local"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local_price = ModelPrice(Decimal("0"), Decimal("0"))
    backend = OpenAICompatibleBackend(
        config=ProviderConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:11434/v1",
            secret_name=None,
            flash_model="local-fast",
            pro_model="local-deep",
        ),
        secret_store=MappingSecretStore({}),
        budget=BudgetManager(
            limits=BudgetLimits(daily=Decimal("1"), monthly=Decimal("1")),
            prices={
                ("openai_compatible", "local-fast"): local_price,
                ("openai_compatible", "local-deep"): local_price,
            },
        ),
        client=client,
    )
    await backend.verify_models()
    result = await backend.generate(
        ModelRequest(messages=(ModelMessage(role=MessageRole.USER, content="hello"),))
    )
    assert result.content == "local"
    assert seen_authorization == [None, None]
    await client.aclose()


@async_test
async def test_unpriced_provider_is_blocked_before_network_call() -> None:
    called = False

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend(
        config=ProviderConfig.deepseek(),
        secret_store=MappingSecretStore({"deepseek_api_key": "secret"}),
        budget=budget(configured_prices={}),
        client=client,
    )
    with pytest.raises(PriceNotConfiguredError):
        await backend.generate(
            ModelRequest(messages=(ModelMessage(role=MessageRole.USER, content="hello"),))
        )
    assert called is False
    await client.aclose()


@async_test
async def test_zhipu_failure_degrades_to_explicit_ocr_only_result() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZhipuVisionAdapter(
        secret_store=MappingSecretStore({"zhipu_api_key": "secret"}),
        budget=budget(),
        client=client,
    )
    service = VisionService(adapter)
    image = SanitizedImage.from_bytes(
        b"already-sanitized-test-image",
        media_type="image/png",
        width=10,
        height=10,
    )
    result = await service.analyze(VisionRequest(image=image, ocr_text="visible text"))
    assert result.semantic_available is False
    assert result.ocr_text == "visible text"
    assert "OCR" in result.description
    assert result.model is None
    await client.aclose()
