"""OpenAI-compatible text gateway with DeepSeek-first defaults."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from lemonbot.domain.models import (
    MessageRole,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolManifest,
    Unsupported,
)

from .budget import BudgetManager
from .config import DeterministicRouter, ProviderConfig
from .schema import ToolCallValidationError, ToolSchemaRegistry
from .secrets import SecretStore, require_secret


class ModelGatewayError(RuntimeError):
    """Base class for a safe, provider-neutral model error."""


class ModelTransportError(ModelGatewayError):
    """The provider could not be reached or its delivery state is uncertain."""


class ModelProtocolError(ModelGatewayError):
    """The provider returned an invalid or unsupported payload."""


def _safe_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


class OpenAICompatibleBackend:
    """A non-streaming, auditable backend for DeepSeek and compatible servers.

    No automatic provider fallback occurs here: after a request is put on the
    wire, its monetary state may be uncertain.  The orchestrator may retry only
    a proven pre-connect failure as a new, separately budgeted operation.
    """

    def __init__(
        self,
        *,
        config: ProviderConfig,
        secret_store: SecretStore,
        budget: BudgetManager,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._secret_store = secret_store
        self._budget = budget
        self._router = DeterministicRouter(config)
        # Cloud credentials must never be forwarded to an ambient HTTP(S)
        # proxy configured through process environment variables. Callers
        # that intentionally need a proxy must pass an explicitly configured
        # client after reviewing that trust boundary.
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

    @classmethod
    def deepseek(
        cls,
        *,
        secret_store: SecretStore,
        budget: BudgetManager,
        client: httpx.AsyncClient | None = None,
        config: ProviderConfig | None = None,
    ) -> OpenAICompatibleBackend:
        selected = config or ProviderConfig.deepseek()
        if selected.provider != "deepseek":
            raise ValueError("DeepSeek factory requires provider='deepseek'")
        return cls(
            config=selected,
            secret_store=secret_store,
            budget=budget,
            client=client,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tools=True,
            json_output=True,
            thinking=self._config.enable_thinking_on_pro,
            vision=False,
            embeddings=False,
            context_tokens=self._config.context_tokens,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise Unsupported("this OpenAI-compatible text backend has no configured embedding model")

    def count_tokens(self, messages: Sequence[object]) -> int:
        """Return a tokenizer-independent upper estimate for a wire payload.

        Counting every serialized UTF-8 byte is deliberately more
        conservative than character/4 heuristics. It also includes assistant
        tool calls, JSON arguments, names and transient thinking-continuation
        fields rather than only visible message content.
        """

        total = 256  # provider chat framing and special-token reserve
        for message in messages:
            if isinstance(message, ModelMessage):
                try:
                    payload: object = self._message_payload(message)
                except ModelProtocolError:
                    # Counting remains fail-safe while the malformed message
                    # is on its way to the protocol validator.
                    payload = message.model_dump(mode="json", exclude_none=False)
            elif hasattr(message, "model_dump"):
                payload = message.model_dump(mode="json", exclude_none=False)
            else:
                payload = {"value": repr(message)}
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            total += len(encoded) + 64
        return total

    async def verify_models(self) -> tuple[str, ...]:
        """Authenticate and confirm both deterministically routed model IDs.

        This uses the OpenAI-compatible model catalogue rather than spending
        tokens on a synthetic chat.  It intentionally returns only model IDs;
        response bodies and credentials must never reach logs.
        """

        headers: dict[str, str] = {}
        if self._config.secret_name is not None:
            api_key = await require_secret(self._secret_store, self._config.secret_name)
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = await self._client.get(
                f"{self._config.base_url}/models",
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise ModelTransportError("model catalogue health check failed") from exc
        if response.is_error:
            raise ModelTransportError(
                f"model catalogue health check returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ModelProtocolError("model catalogue returned invalid JSON") from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
            raise ModelProtocolError("model catalogue returned an invalid object")
        identifiers = tuple(
            item["id"]
            for item in body["data"]
            if isinstance(item, Mapping)
            and isinstance(item.get("id"), str)
            and bool(item["id"].strip())
        )
        required = {self._config.flash_model, self._config.pro_model}
        missing = required.difference(identifiers)
        if missing:
            raise ModelProtocolError(
                "configured model catalogue is missing: " + ", ".join(sorted(missing))
            )
        return identifiers

    def _estimate_prompt_tokens(self, request: ModelRequest) -> int:
        total = self.count_tokens(request.messages)
        if request.tools:
            raw = json.dumps(
                [manifest.model_dump(mode="json") for manifest in request.tools],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            total += len(raw.encode("utf-8")) + 64 * len(request.tools) + 256
        return total

    @staticmethod
    def _message_payload(message: ModelMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.name is not None:
            payload["name"] = message.name
        if message.role is MessageRole.TOOL:
            if not message.tool_call_id:
                raise ModelProtocolError("tool messages require tool_call_id")
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            if message.role is not MessageRole.ASSISTANT:
                raise ModelProtocolError("only assistant messages may contain tool calls")
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
            # DeepSeek requires this value only inside the live thinking/tool
            # interaction.  The domain storage adapter intentionally drops it.
            if message.reasoning_content is not None:
                payload["reasoning_content"] = message.reasoning_content
        return payload

    @staticmethod
    def _tool_payload(registry_manifest: ToolManifest) -> dict[str, Any]:
        manifest = registry_manifest
        return {
            "type": "function",
            "function": {
                "name": manifest.name,
                "description": manifest.description,
                "parameters": manifest.input_schema,
            },
        }

    def _request_payload(self, request: ModelRequest, model: str, thinking: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._message_payload(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [self._tool_payload(manifest) for manifest in request.tools]
            payload["tool_choice"] = "auto"
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if thinking:
            payload["thinking"] = {"type": "enabled"}
        return payload

    @staticmethod
    def _usage(payload: Mapping[str, Any]) -> tuple[int, int, int]:
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return 0, 0, 0
        prompt = _safe_int(usage.get("prompt_tokens"))
        completion = _safe_int(usage.get("completion_tokens"))
        cached = _safe_int(usage.get("prompt_cache_hit_tokens"))
        details = usage.get("prompt_tokens_details")
        if not cached and isinstance(details, Mapping):
            cached = _safe_int(details.get("cached_tokens"))
        return prompt, completion, min(prompt, cached)

    @staticmethod
    def _parse_tool_calls(message: Mapping[str, Any]) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls", [])
        if raw_calls is None:
            return ()
        if not isinstance(raw_calls, list):
            raise ModelProtocolError("provider tool_calls must be a list")
        calls: list[ToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                raise ModelProtocolError("provider returned an invalid tool call")
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                raise ModelProtocolError("provider tool call has no function object")
            call_id = raw_call.get("id")
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ModelProtocolError("provider tool call identity is invalid")
            if not isinstance(arguments, str):
                raise ModelProtocolError("provider tool arguments must be encoded JSON")
            try:
                calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=ToolSchemaRegistry.parse_arguments(arguments),
                    )
                )
            except ToolCallValidationError:
                raise
            except ValueError as exc:
                raise ModelProtocolError("provider returned an invalid tool call") from exc
        return tuple(calls)

    @staticmethod
    def _parse_choice(
        payload: Mapping[str, Any],
    ) -> tuple[str | None, tuple[ToolCall, ...], str | None, str | None]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelProtocolError("provider response has no valid choice")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ModelProtocolError("provider response has no valid message")

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ModelProtocolError("provider message content must be text or null")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            finish_reason = None
        reasoning_content = message.get("reasoning_content")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            reasoning_content = None
        return (
            content,
            OpenAICompatibleBackend._parse_tool_calls(message),
            finish_reason,
            reasoning_content,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ModelProtocolError("model request requires at least one message")
        registry = ToolSchemaRegistry(request.tools)
        routed = self._router.route(request)
        estimated_prompt = self._estimate_prompt_tokens(request)
        if estimated_prompt + request.max_tokens > self._config.context_tokens:
            raise ModelProtocolError("request exceeds the configured model context window")

        headers = {"Content-Type": "application/json"}
        if self._config.secret_name is not None:
            api_key = await require_secret(self._secret_store, self._config.secret_name)
            headers["Authorization"] = f"Bearer {api_key}"
        reservation = await self._budget.reserve(
            provider=routed.provider,
            model=routed.model,
            prompt_tokens=estimated_prompt,
            maximum_completion_tokens=request.max_tokens,
        )
        endpoint = f"{self._config.base_url}/chat/completions"
        payload = self._request_payload(request, routed.model, routed.thinking)

        try:
            response = await self._client.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=self._config.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            await self._budget.release(reservation.reservation_id)
            raise ModelTransportError("model provider connection failed before delivery") from exc
        except httpx.TransportError as exc:
            await self._budget.settle_unknown(reservation.reservation_id)
            raise ModelTransportError("model provider delivery state is unknown") from exc

        if response.is_error:
            await self._budget.settle_unknown(reservation.reservation_id)
            raise ModelTransportError(f"model provider returned HTTP {response.status_code}")

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            await self._budget.settle_unknown(reservation.reservation_id)
            raise ModelProtocolError("model provider returned invalid JSON") from exc
        if not isinstance(body, Mapping):
            await self._budget.settle_unknown(reservation.reservation_id)
            raise ModelProtocolError("model provider returned a non-object response")

        prompt_tokens, completion_tokens, cached_prompt_tokens = self._usage(body)
        try:
            content, calls, finish_reason, reasoning_content = self._parse_choice(body)
        except (ModelProtocolError, ToolCallValidationError):
            if prompt_tokens or completion_tokens:
                await self._budget.settle(
                    reservation.reservation_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_prompt_tokens=cached_prompt_tokens,
                )
            else:
                await self._budget.settle_unknown(reservation.reservation_id)
            raise

        await self._budget.settle(
            reservation.reservation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
        )
        validated_calls = registry.validate_many(calls)
        if request.response_format == "json" and content is not None:
            try:
                parsed_content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("provider did not honor JSON response mode") from exc
            if not isinstance(parsed_content, dict):
                raise ModelProtocolError("JSON response mode requires a JSON object")
        return ModelResponse(
            content=content,
            tool_calls=validated_calls,
            reasoning_content=reasoning_content,
            model=routed.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )


DeepSeekBackend = OpenAICompatibleBackend
