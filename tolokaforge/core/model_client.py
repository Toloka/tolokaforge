"""LLM client abstraction using LiteLLM"""

import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

import litellm
import yaml
from litellm import completion
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall
from tolokaforge.core.pricing import estimate_cost
from tolokaforge.secrets import SecretManager, get_default


def _should_retry_exception(exc: BaseException) -> bool:
    """Determine if exception should be retried.

    Returns True for all transient errors including rate limits (429).
    Rate limits are retried with the same exponential backoff as other
    errors — the long waits (up to 60s between attempts) give the
    provider quota time to recover.
    """
    # All transient errors should be retried, including rate limits.
    # The exponential backoff wait (up to 60s) provides adequate
    # cooldown for rate-limited endpoints.
    return True


class GenerationResult:
    """Result from LLM generation"""

    def __init__(
        self,
        text: str,
        tool_calls: list[ToolCall] | None = None,
        token_usage: dict[str, int] | None = None,
        latency_s: float = 0.0,
        cost_usd: float | None = None,
        reasoning: str | None = None,
    ):
        self.text = text
        self.tool_calls = tool_calls or []
        self.token_usage = token_usage or {"input": 0, "output": 0}
        self.latency_s = latency_s
        self.cost_usd = cost_usd  # None means pricing unknown for this model
        self.reasoning = reasoning  # Thinking/reasoning blocks for visibility


class LLMClient:
    """Provider-agnostic LLM client using LiteLLM"""

    def __init__(self, config: ModelConfig, secrets: SecretManager | None = None):
        self.config = config
        self.model_name = self._format_model_name()
        self.provider = (config.provider or "").lower()
        self.logger = get_logger("llm_client")
        self._secrets = secrets if secrets is not None else get_default()
        # Load API key chain for rotation on key exhaustion
        self._api_keys = self._load_api_keys()
        self._current_key_index = 0
        if self.provider.startswith("openrouter"):
            self._openrouter_headers = self._configure_openrouter_headers()
            self._configure_openrouter_base_url()
        elif self.provider == "nova":
            self._configure_nova_base_url()
        else:
            pass
        self._openrouter_headers = (
            self._configure_openrouter_headers() if self.provider.startswith("openrouter") else {}
        )
        self._ensure_litellm_env()

    def _load_api_keys(self) -> list[str]:
        """Load API keys for rotation from environment or file.

        Keys are loaded from (in order of priority):
        1. OPENROUTER_API_KEYS env var (comma-separated)
        2. File path in OPENROUTER_KEY_FILE env var (default: keys.txt)
        3. Single key from OPENROUTER_API_KEY env var
        """
        # Try OPENROUTER_API_KEYS (comma-separated)
        keys_str = self._secrets.get_secret("OPENROUTER_API_KEYS") or ""
        if keys_str:
            keys = [k.strip() for k in keys_str.split(",") if k.strip()]
            if keys:
                self.logger.info(
                    "Loaded API keys from OPENROUTER_API_KEYS",
                    key_count=len(keys),
                )
                return keys

        # Try key file
        key_file = self._secrets.get_secret("OPENROUTER_KEY_FILE") or "keys.txt"
        if os.path.exists(key_file):
            keys = []
            with open(key_file) as f:
                for line in f:
                    line = line.split("#")[0].strip()
                    if line:
                        # Take first field (before comma) as OpenRouter key
                        or_key = line.split(",")[0].strip()
                        if or_key:
                            keys.append(or_key)
            if keys:
                self.logger.info(
                    "Loaded API keys from file",
                    key_file=key_file,
                    key_count=len(keys),
                )
                return keys

        # Fall back to single key
        key = self._secrets.get_secret("OPENROUTER_API_KEY") or ""
        if key:
            return [key]
        return []

    def _rotate_key(self) -> bool:
        """Rotate to the next available API key.

        Returns:
            True if rotation succeeded, False if all keys exhausted.
        """
        if self._current_key_index + 1 < len(self._api_keys):
            self._current_key_index += 1
            new_key = self._api_keys[self._current_key_index]
            os.environ["OPENROUTER_API_KEY"] = new_key
            self.logger.info(
                "Rotated to API key",
                key_suffix=new_key[-6:] if len(new_key) >= 6 else "***",
                index=self._current_key_index,
            )
            return True
        return False

    def _configure_openrouter_headers(self) -> dict[str, str]:
        """Ensure OpenRouter requests include the required headers.

        OpenRouter rejects requests for certain models unless callers opt-out of
        data collection. The API also expects either an ``HTTP-Referer`` or
        ``X-Title`` header to identify the application. We set conservative
        defaults so evaluations succeed out of the box while still allowing
        operators to override values via environment variables.
        """

        # ``litellm`` stores headers globally in ``openai_headers``. Copy the
        # current mapping (if any) to avoid mutating shared state in-place.
        existing_headers = dict(getattr(litellm, "openai_headers", {}) or {})

        referer = (
            self._secrets.get_secret("TOLOKAFORGE_OPENROUTER_REFERER")
            or "https://github.com/Toloka-F/tolokaforge"
        )
        title = self._secrets.get_secret("TOLOKAFORGE_OPENROUTER_TITLE") or "Tolokaforge Evaluation"

        existing_headers.setdefault("HTTP-Referer", referer)
        existing_headers.setdefault("X-Title", title)

        opt_out_pref = (
            self._secrets.get_secret("TOLOKAFORGE_OPENROUTER_OPT_OUT") or "true"
        ).lower()
        if opt_out_pref in {"1", "true", "yes", "on"}:
            existing_headers.setdefault("X-Data-Collection-Opt-Out", "true")

        litellm.openai_headers = existing_headers
        return existing_headers

    def _configure_openrouter_base_url(self) -> None:
        """Propagate Tolokaforge OpenRouter base URL overrides to LiteLLM.

        LiteLLM reads the ``OPENROUTER_API_BASE`` environment variable when
        building OpenRouter requests.  Some environments (including the
        Tolokaforge benchmarks) surface an ``OPENROUTER_BASE_URL`` secret
        instead.  Accept either variable and set the OPENROUTER_API_BASE env var
        so downstream completion calls use the allowed endpoint.

        Note: We intentionally do NOT set litellm.api_base here because it's a
        global setting that would affect other providers (e.g., Nova, Anthropic).
        Instead, we rely on LiteLLM's internal routing based on the model prefix.
        """

        # Prioritise an explicit OPENROUTER_BASE_URL override, falling back to
        # OPENROUTER_API_BASE if it was provided already.
        base_url = self._secrets.get_secret("OPENROUTER_BASE_URL") or self._secrets.get_secret(
            "OPENROUTER_API_BASE"
        )

        if not base_url:
            return

        # Ensure LiteLLM sees the correct base URL even if callers only set the
        # Tolokaforge-specific variable.
        os.environ.setdefault("OPENROUTER_API_BASE", base_url)
        # Store for per-request use if needed
        self._openrouter_base_url = base_url

    def _configure_nova_base_url(self) -> None:
        """Configure Nova API base URL for LiteLLM.

        Nova API is OpenAI-compatible and hosted at api.nova.amazon.com/v1.
        The base URL is set per-request in generate() rather than globally
        to avoid affecting other providers like OpenRouter.
        """
        # Set environment variable for LiteLLM Nova routing (informational only)
        os.environ.setdefault("NOVA_API_BASE", "https://api.nova.amazon.com/v1")

        # Note: We intentionally do NOT set litellm.api_base here because it's a
        # global setting that would affect other providers. The api_base is
        # passed per-request in the generate() method instead.

    def _ensure_litellm_env(self) -> None:
        """Export API keys to os.environ so litellm can read them."""
        litellm_keys = [
            "OPENROUTER_API_KEY",
            "OPENROUTER_API_BASE",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "NOVA_API_KEY",
        ]
        self._secrets.export_to_environ(litellm_keys)

    def _format_model_name(self) -> str:
        """Format model name for LiteLLM"""
        # LiteLLM expects format like "openai/gpt-4", "anthropic/claude-3-sonnet", etc.
        # For OpenRouter with subproviders like "google/gemini-...", need "openrouter/google/..."
        if self.config.name.startswith(f"{self.config.provider}/"):
            # Already has provider prefix
            return self.config.name

        # For Nova, we use the model name as-is since it's OpenAI-compatible
        if self.config.provider.lower() == "nova":
            return self.config.name

        # Always prepend provider for other cases
        return f"{self.config.provider}/{self.config.name}"

    @staticmethod
    def _repair_json_like(raw: str) -> str:
        """Apply lightweight repairs to near-JSON argument payloads."""
        repaired = raw.strip()

        # Remove markdown fences if the model wrapped arguments in a code block.
        if repaired.startswith("```"):
            repaired = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", repaired)
            repaired = repaired.replace("```", "").strip()

        # Normalize smart quotes.
        repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

        # Quote unquoted JSON-like keys (e.g. {path: "..."}).
        repaired = re.sub(
            r"([{\[,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            repaired,
        )

        # If braces are unbalanced, add missing closing braces.
        open_braces = repaired.count("{")
        close_braces = repaired.count("}")
        if open_braces > close_braces:
            repaired = repaired + ("}" * (open_braces - close_braces))

        return repaired

    def _parse_tool_arguments(self, tool_name: str, raw_args: Any) -> dict[str, Any]:
        """Parse model-emitted tool arguments with tolerant fallbacks."""

        def _normalize(parsed_args: dict[str, Any]) -> dict[str, Any]:
            normalized = dict(parsed_args)
            if tool_name in {"browser", "mobile"}:
                actions = normalized.get("actions")
                if isinstance(actions, str):
                    cleaned = re.sub(r"</?invoke>", "", actions).strip()
                    for parser in (json.loads, yaml.safe_load):
                        try:
                            decoded = parser(cleaned)
                            if isinstance(decoded, list):
                                normalized["actions"] = decoded
                                self.logger.warning(
                                    "Recovered malformed browser/mobile actions payload",
                                    tool=tool_name,
                                )
                                break
                        except Exception:
                            continue
            return normalized

        if isinstance(raw_args, dict):
            return _normalize(raw_args)
        if raw_args is None or not isinstance(raw_args, str):
            return {}

        args_str = raw_args.strip()
        if not args_str:
            return {}

        # Fast path: strict JSON.
        try:
            parsed = json.loads(args_str)
            return _normalize(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        # YAML can parse many JSON-like variants (unquoted keys, single quotes).
        try:
            parsed = yaml.safe_load(args_str)
            if isinstance(parsed, dict):
                self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                return _normalize(parsed)
        except Exception:
            pass

        # Lightweight repair + retry JSON/YAML.
        repaired = self._repair_json_like(args_str)
        parsers: list[Callable[[str], Any]] = [json.loads, yaml.safe_load]
        for parser in parsers:
            try:
                parsed = parser(repaired)
                if isinstance(parsed, dict):
                    self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                    return _normalize(parsed)
            except Exception:
                continue

        self.logger.warning(
            "Failed to parse tool arguments",
            tool=tool_name,
            error="Unable to parse with JSON/YAML fallbacks",
        )
        return {}

    def _tool_block_format(self) -> str:
        """Determine the content block format for tool results."""
        model = (self.model_name or "").lower()
        if model.startswith("openrouter/"):
            model = model[len("openrouter/") :]
        if model.startswith("anthropic/") or "claude" in model:
            return "anthropic"
        if model.startswith("openai/") or model.startswith("gpt-"):
            return "openai"
        if model.startswith("azure/") or self.provider in {"openai", "azure", "nova"}:
            return "openai"
        # Default to OpenAI-compatible blocks for non-Anthropic providers
        return "openai"

    def supports_tool_image_blocks(self) -> bool:
        """Whether tool-result image blocks are supported by the target model."""
        return self._tool_block_format() == "anthropic"

    def _tool_blocks_to_text(self, blocks: list[dict[str, Any]]) -> str:
        """Flatten content blocks into a text-only summary."""
        texts: list[str] = []
        has_image = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif btype in {"image", "image_url"}:
                has_image = True
        if texts:
            return "\n".join(texts)
        if has_image:
            return "Screenshot captured."
        return ""

    def _adapt_tool_content_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tool content blocks into the provider-appropriate format."""
        target = self._tool_block_format()
        texts: list[str] = []
        images: list[dict[str, str]] = []

        def _push_image(data: str | None, media_type: str | None, url: str | None):
            if data:
                images.append({"data": data, "media_type": media_type or "image/png"})
            elif url:
                images.append({"url": url, "media_type": media_type or "image/png"})

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif btype == "image":
                source = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
                stype = source.get("type")
                if stype == "base64":
                    _push_image(source.get("data"), source.get("media_type"), None)
                elif stype == "url":
                    _push_image(None, source.get("media_type"), source.get("url"))
            elif btype == "image_url":
                image_url = (
                    block.get("image_url", {}) if isinstance(block.get("image_url"), dict) else {}
                )
                url = image_url.get("url")
                if isinstance(url, str) and url.startswith("data:image/"):
                    header, _, data = url.partition(",")
                    media_type = (
                        header.split(";")[0].replace("data:", "") if header else "image/png"
                    )
                    _push_image(data or None, media_type, None)
                elif isinstance(url, str) and url:
                    _push_image(None, None, url)

        if target == "anthropic":
            adapted: list[dict[str, Any]] = []
            for text in texts:
                adapted.append({"type": "text", "text": text})
            for img in images:
                if "data" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img.get("media_type", "image/png"),
                                "data": img["data"],
                            },
                        }
                    )
                elif "url" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "media_type": img.get("media_type", "image/png"),
                                "url": img["url"],
                            },
                        }
                    )
            if adapted:
                return adapted
            return [{"type": "text", "text": ""}]

        # OpenAI-compatible blocks
        adapted: list[dict[str, Any]] = []
        for text in texts:
            adapted.append({"type": "text", "text": text})
        for img in images:
            if "data" in img:
                url = f"data:{img.get('media_type', 'image/png')};base64,{img['data']}"
                adapted.append({"type": "image_url", "image_url": {"url": url}})
            elif "url" in img:
                adapted.append({"type": "image_url", "image_url": {"url": img["url"]}})
        if adapted:
            return adapted
        return [{"type": "text", "text": ""}]

    def _tool_block_format(self) -> str:
        """Determine the content block format for tool results."""
        model = (self.model_name or "").lower()
        if model.startswith("openrouter/"):
            model = model[len("openrouter/") :]
        if model.startswith("anthropic/") or "claude" in model:
            return "anthropic"
        if model.startswith("openai/") or model.startswith("gpt-"):
            return "openai"
        if model.startswith("azure/") or self.provider in {"openai", "azure", "nova"}:
            return "openai"
        # Default to OpenAI-compatible blocks for non-Anthropic providers
        return "openai"

    def _adapt_tool_content_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tool content blocks into the provider-appropriate format."""
        target = self._tool_block_format()
        texts: list[str] = []
        images: list[dict[str, str]] = []

        def _push_image(data: str | None, media_type: str | None, url: str | None):
            if data:
                images.append({"data": data, "media_type": media_type or "image/png"})
            elif url:
                images.append({"url": url, "media_type": media_type or "image/png"})

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif btype == "image":
                source = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
                stype = source.get("type")
                if stype == "base64":
                    _push_image(source.get("data"), source.get("media_type"), None)
                elif stype == "url":
                    _push_image(None, source.get("media_type"), source.get("url"))
            elif btype == "image_url":
                image_url = (
                    block.get("image_url", {}) if isinstance(block.get("image_url"), dict) else {}
                )
                url = image_url.get("url")
                if isinstance(url, str) and url.startswith("data:image/"):
                    header, _, data = url.partition(",")
                    media_type = (
                        header.split(";")[0].replace("data:", "") if header else "image/png"
                    )
                    _push_image(data or None, media_type, None)
                elif isinstance(url, str) and url:
                    _push_image(None, None, url)

        if target == "anthropic":
            adapted: list[dict[str, Any]] = []
            for text in texts:
                adapted.append({"type": "text", "text": text})
            for img in images:
                if "data" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img.get("media_type", "image/png"),
                                "data": img["data"],
                            },
                        }
                    )
                elif "url" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "media_type": img.get("media_type", "image/png"),
                                "url": img["url"],
                            },
                        }
                    )
            if adapted:
                return adapted
            return [{"type": "text", "text": ""}]

        # OpenAI-compatible blocks
        adapted: list[dict[str, Any]] = []
        for text in texts:
            adapted.append({"type": "text", "text": text})
        for img in images:
            if "data" in img:
                url = f"data:{img.get('media_type', 'image/png')};base64,{img['data']}"
                adapted.append({"type": "image_url", "image_url": {"url": url}})
            elif "url" in img:
                adapted.append({"type": "image_url", "image_url": {"url": img["url"]}})
        if adapted:
            return adapted
        return [{"type": "text", "text": ""}]

    def _convert_messages(
        self, system: str | None, messages: list[Message]
    ) -> list[dict[str, Any]]:
        """Convert our Message format to LiteLLM format

        Note: User tool calls are kept in Message objects for ActionEvaluator,
        but stripped here since most LLM APIs don't support tool_use from USER role
        """
        litellm_messages = []

        if system:
            litellm_messages.append({"role": "system", "content": system})

        for msg in messages:
            litellm_msg: dict[str, Any] = {"role": msg.role.value}

            if msg.role == MessageRole.TOOL:
                if msg.content_blocks:
                    # Multimodal tool result (e.g. screenshots from visual_mode)
                    if self.supports_tool_image_blocks():
                        litellm_msg["content"] = self._adapt_tool_content_blocks(msg.content_blocks)
                    else:
                        content = self._tool_blocks_to_text(msg.content_blocks)
                        litellm_msg["content"] = content or "{}"
                else:
                    # Text-only tool result
                    content = msg.content
                    if not content or (isinstance(content, str) and content.strip() == ""):
                        content = "{}"  # Empty JSON object as fallback
                    litellm_msg["content"] = content
                litellm_msg["tool_call_id"] = msg.tool_call_id
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                # Ensure non-empty content for AWS Bedrock/Nova compatibility
                # Bedrock rejects messages with blank text content blocks (empty strings or None)
                content = msg.content
                if not content or content.strip() == "":
                    content = "I'll help you with that."
                litellm_msg["content"] = content
                import json

                litellm_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                json.dumps(tc.arguments)
                                if isinstance(tc.arguments, dict)
                                else tc.arguments
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            else:
                # For USER and regular ASSISTANT messages
                # Ensure non-empty content for AWS Bedrock/Nova compatibility
                content = msg.content
                if not content or (isinstance(content, str) and content.strip() == ""):
                    if msg.role == MessageRole.USER:
                        content = "Please continue."
                    else:
                        content = "I understand."
                litellm_msg["content"] = content

            litellm_messages.append(litellm_msg)

        return litellm_messages

    # Model families whose upstream APIs reject non-standard JSON Schema keys
    # (title, examples, minProperties) and schema-valued additionalProperties.
    _STRICT_SCHEMA_MODEL_PREFIXES: tuple[str, ...] = ("x-ai/",)

    # Keys not in the OpenAI function-calling JSON Schema spec.
    _STRIP_SCHEMA_KEYS = frozenset(
        {
            "title",
            "examples",
            "minProperties",
            "maxProperties",
        }
    )

    def _needs_strict_schema(self) -> bool:
        """Return True if the model requires sanitised tool schemas."""
        name = self.config.name.lower()
        return any(name.startswith(p) for p in self._STRICT_SCHEMA_MODEL_PREFIXES)

    @classmethod
    def _sanitise_schema_strict(cls, schema: Any) -> Any:
        """Recursively sanitise tool parameter schemas for strict providers.

        Applies three transformations:
        1. Strips non-standard keys (``title``, ``examples``, ``minProperties``).
        2. Converts ``additionalProperties: {schema}`` (typed maps) into
           ``additionalProperties: true`` and appends a human-readable
           description of the value schema so the LLM retains the info.
        3. Removes ``description`` at the parameters root level (Pydantic
           artefact — the function-level ``description`` already conveys this).
        """
        if isinstance(schema, list):
            return [cls._sanitise_schema_strict(item) for item in schema]
        if not isinstance(schema, dict):
            return schema

        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key in cls._STRIP_SCHEMA_KEYS:
                continue
            if key == "additionalProperties" and isinstance(value, dict):
                # Convert typed map → boolean + describe value schema in parent
                result[key] = True
                # Enrich the description so the LLM still knows the expected
                # dict structure:  {"key_string": {value_schema}, ...}
                value_desc = cls._describe_map_value_schema(value)
                if value_desc:
                    existing = result.get("description", "")
                    sep = " " if existing else ""
                    result["description"] = (
                        f"{existing}{sep}"
                        f"This is a JSON object (dict) mapping string keys to values. "
                        f"Each value is an object with: {value_desc}. "
                        f'Example: {{"key1": {value_desc.split(" required")[0]}}}'
                    )
                continue
            result[key] = cls._sanitise_schema_strict(value)
        return result

    @staticmethod
    def _describe_map_value_schema(schema: dict[str, Any]) -> str:
        """Build a short textual description of a JSON Schema for map values."""
        props = schema.get("properties", {})
        if not props:
            return ""
        parts = []
        for name, prop in props.items():
            ptype = prop.get("type", "any")
            parts.append(f"{name} ({ptype})")
        required = schema.get("required", [])
        desc = "{" + ", ".join(parts) + "}"
        if required:
            desc += f" required: [{', '.join(required)}]"
        return desc

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tool schemas to LiteLLM format.

        For models with strict schema validation (e.g. xAI/Grok), applies
        additional sanitisation to remove non-standard JSON Schema keys and
        convert typed-map ``additionalProperties`` into enriched descriptions.
        Models that accept the full schema (Claude, GPT, Gemini) get tools
        unchanged.
        """
        if not self._needs_strict_schema():
            return tools

        sanitised: list[dict[str, Any]] = []
        for tool in tools:
            tool = self._sanitise_schema_strict(tool)
            # Remove top-level 'description' inside parameters (Pydantic artefact)
            if isinstance(tool, dict):
                func = tool.get("function", {})
                params = func.get("parameters")
                if isinstance(params, dict):
                    params.pop("description", None)
            sanitised.append(tool)
        return sanitised

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        retry=retry_if_exception(_should_retry_exception),
        before_sleep=before_sleep_log(get_logger("llm_retry").logger, logging.WARNING),
        reraise=True,
    )
    def generate(
        self,
        system: str | None = None,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        reasoning: str | None = None,
        top_p: float | None = None,
    ) -> GenerationResult:
        """
        Generate completion from LLM

        Args:
            system: System prompt
            messages: Conversation messages
            tools: Available tools (OpenAI function calling format)
            tool_choice: Tool choice strategy ("auto", "none", or specific tool)
            temperature: Sampling temperature (overrides config)
            max_tokens: Max tokens to generate (overrides config)
            seed: Random seed (overrides config)
            reasoning: Reasoning effort ("off", "low", "medium", "high") - overrides config
            top_p: Nucleus sampling parameter (0.0-1.0) - overrides config

        Returns:
            GenerationResult with text, tool_calls, token usage, latency, and reasoning
        """
        messages = messages or []

        if self.provider == "mock":
            return self._mock_generate(messages, tools)

        litellm_messages = self._convert_messages(system, messages)

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": litellm_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
        }

        # Add top_p only if specified
        top_p_value = top_p if top_p is not None else self.config.top_p
        if top_p_value is not None:
            kwargs["top_p"] = top_p_value

        # Add max_tokens only if specified
        max_tokens_value = max_tokens if max_tokens is not None else self.config.max_tokens
        if max_tokens_value is not None:
            kwargs["max_tokens"] = max_tokens_value

        # Add seed if supported and provided
        # Note: Anthropic doesn't support seed parameter as of 2025-10
        # OpenAI and some other providers do support it
        if seed is not None or self.config.seed is not None:
            # Only add seed for providers that support it
            provider_lower = self.config.provider.lower()
            if "anthropic" not in provider_lower and "claude" not in self.model_name.lower():
                kwargs["seed"] = seed if seed is not None else self.config.seed

        # Add tools if provided
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        # Add reasoning effort if enabled.
        # OpenRouter consolidates reasoning settings in extra_body:
        #   {"reasoning": {"effort": "<level>", "enabled": true}}
        # See https://openrouter.ai/docs/reasoning.
        # LiteLLM does not (yet) support ``reasoning_effort`` for the
        # openrouter provider, so we pass it via ``extra_body``.
        # For native providers (openai, anthropic, …) we use LiteLLM's
        # built-in ``reasoning_effort`` keyword.
        reasoning_effort = reasoning if reasoning is not None else self.config.reasoning
        if reasoning_effort and reasoning_effort.lower() not in ("off", ""):
            if self.provider.startswith("openrouter"):
                extra_body = kwargs.get("extra_body", {})
                extra_body["reasoning"] = {
                    "effort": reasoning_effort.lower(),
                    "enabled": True,
                }
                kwargs["extra_body"] = extra_body
            else:
                kwargs["reasoning_effort"] = reasoning_effort.lower()

        if self.provider.startswith("openrouter"):
            extra_headers = dict(self._openrouter_headers)
            existing_extra = kwargs.get("extra_headers")
            if isinstance(existing_extra, dict):
                extra_headers.update(existing_extra)
            kwargs["extra_headers"] = extra_headers
            kwargs.setdefault("custom_llm_provider", self.provider.split("/")[0])

        start_time = time.time()
        # Key rotation loop - retry with next key on exhaustion
        while True:
            try:
                # Handle Nova provider with custom configuration
                if self.provider == "nova":
                    # Nova uses OpenAI-compatible API with custom base URL and auth
                    kwargs["api_base"] = "https://api.nova.amazon.com/v1"
                    kwargs["api_key"] = self._secrets.get_secret("NOVA_API_KEY")
                    if not kwargs["api_key"]:
                        raise RuntimeError(
                            "NOVA_API_KEY environment variable is required for Nova provider"
                        )

                    # Use custom_llm_provider to tell LiteLLM to treat Nova as OpenAI-compatible
                    kwargs["custom_llm_provider"] = "openai"

                    # Remove model prefix if present since Nova expects clean model names
                    if kwargs["model"].startswith("nova/"):
                        kwargs["model"] = kwargs["model"][5:]

                    # Prefix with openai/ for LiteLLM routing
                    if not kwargs["model"].startswith("openai/"):
                        kwargs["model"] = f"openai/{kwargs['model']}"

                # For OpenRouter with nested providers (e.g., openrouter/google/...), use custom_llm_provider
                elif "/" in self.config.provider:
                    # Format like "openrouter/google" - first part is the provider
                    base_provider = self.config.provider.split("/")[0]
                    kwargs["custom_llm_provider"] = base_provider

                response = completion(**kwargs)
                break  # Success - exit the key rotation loop
            except Exception as e:
                error_str = str(e)
                # Check for key exhaustion (403 "Key limit exceeded")
                if (
                    "Key limit exceeded" in error_str
                    or "requires more credits" in error_str
                    or '"code":403' in error_str
                    or '"code":402' in error_str
                ):
                    if self._rotate_key():
                        self.logger.warning(
                            "API key exhausted, rotated to next key",
                            key_index=self._current_key_index,
                            remaining_keys=len(self._api_keys) - self._current_key_index - 1,
                        )
                        # Retry with new key - don't count this as a retry
                        continue
                    else:
                        self.logger.error("All API keys exhausted")
                        raise RuntimeError("All API keys exhausted") from e
                # Not a key exhaustion error - re-raise for tenacity retry handling
                raise RuntimeError(f"LLM API call failed: {e}") from e
        latency = time.time() - start_time

        # Extract response content
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls = []
        reasoning_text = None

        # Extract reasoning/thinking blocks (OpenRouter-compatible)
        # Try reasoning_content first (summary), then thinking_blocks (Anthropic)
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            reasoning_text = message.reasoning_content
        elif hasattr(message, "thinking_blocks") and message.thinking_blocks:
            # Merge Anthropic thinking blocks
            blocks = []
            for block in message.thinking_blocks:
                if isinstance(block, dict) and "thinking" in block:
                    blocks.append(block["thinking"])
            if blocks:
                reasoning_text = "\n\n".join(blocks)

        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                arguments = self._parse_tool_arguments(tc.function.name, tc.function.arguments)

                # Handle Nova/AWS Bedrock wrapping arguments in 'input' key
                # Nova may return {'input': {'actual_arg': 'value'}} instead of {'actual_arg': 'value'}
                if isinstance(arguments, dict) and "input" in arguments and len(arguments) == 1:
                    inner = arguments["input"]
                    if isinstance(inner, dict):
                        arguments = inner

                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                    )
                )

        # Extract token usage
        token_usage = {}
        if hasattr(response, "usage") and response.usage:
            token_usage = {
                "input": response.usage.prompt_tokens or 0,
                "output": response.usage.completion_tokens or 0,
            }

        # Estimate cost (None when pricing is unknown for this model)
        cost_usd: float | None = None
        if token_usage:
            cost_usd = estimate_cost(
                model=self.model_name,
                input_tokens=token_usage["input"],
                output_tokens=token_usage["output"],
            )

        return GenerationResult(
            text=text,
            tool_calls=tool_calls,
            token_usage=token_usage,
            latency_s=latency,
            cost_usd=cost_usd,
            reasoning=reasoning_text,
        )

    def _mock_generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
    ) -> GenerationResult:
        """Deterministic mock responder for offline tests."""

        last_message = messages[-1] if messages else None
        text: str

        # Differentiate agent vs user by model name suffix
        name_hint = (self.config.name or "").lower()
        last_content = (last_message.content if last_message else "") or ""
        lower_content = last_content.lower()

        if "judge" in name_hint or "grading judge" in lower_content:
            text = '{"score": 0.7, "reasons": "Mock judge: baseline structured evaluation."}'
        elif "user" in name_hint:
            # First turn: provide instruction, subsequent turns stop quickly
            if not messages or (
                len(messages) == 1 and last_message and last_message.role == MessageRole.ASSISTANT
            ):
                text = "Hello, I need help completing this benchmark task."
            else:
                text = "Thanks, that answers my question. ###STOP###"
        else:
            # Agent response: acknowledge and mark completion so the runner exits fast
            text = "Acknowledged. Task complete."

        return GenerationResult(
            text=text,
            tool_calls=[],
            token_usage={"input": 0, "output": len(text.split())},
            latency_s=0.0,
            cost_usd=0.0,
        )


class UserSimulator:
    """User simulator for benchmarking"""

    def __init__(
        self,
        mode: str = "scripted",
        llm_config: ModelConfig | None = None,
        persona: str = "cooperative",
        backstory: str | None = None,
        scripted_flow: list[dict[str, str]] | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
    ):
        self.mode = mode
        self.persona = persona
        self.backstory = backstory
        self.scripted_flow = scripted_flow or []
        self.tool_schemas = tool_schemas or []
        self.llm_client = LLMClient(llm_config) if llm_config and mode == "llm" else None

    def reply(self, context: list[Message]) -> GenerationResult:
        """Generate user reply based on context

        Returns:
            GenerationResult with text and optional tool_calls
        """
        if self.mode == "scripted":
            text = self._scripted_reply(context)
            return GenerationResult(text=text, tool_calls=[])
        elif self.mode == "llm":
            return self._llm_reply(context)
        else:
            raise ValueError(f"Unknown user simulator mode: {self.mode}")

    def _scripted_reply(self, context: list[Message]) -> str:
        """Generate scripted reply based on flow rules"""
        if not context:
            return "Hello, I need help with this task."

        # If there are unconditional scripted entries, deliver each once in order
        if self.scripted_flow:
            sent_messages = {
                msg.content.strip()
                for msg in context
                if msg.role == MessageRole.USER and msg.content
            }

            for rule in self.scripted_flow:
                has_condition = any(key.startswith("if_") for key in rule)
                if not has_condition:
                    text = rule.get("user", "").strip()
                    if text and text not in sent_messages:
                        return text

        # Find last assistant message
        last_assistant_msg = None
        for msg in reversed(context):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant_msg = msg.content
                break

        if not last_assistant_msg:
            return "I'm waiting for your response."

        # Check scripted flow for matching rules
        default_response = None
        for rule in self.scripted_flow:
            if (
                "if_assistant_contains" in rule
                and rule["if_assistant_contains"].lower() in last_assistant_msg.lower()
            ):
                return rule["user"]
            # Capture default for later use
            if "default" in rule:
                default_response = rule["default"]

        # Use scripted default if provided
        if default_response:
            return default_response

        # Fallback to generic responses
        if "?" in last_assistant_msg:
            return "Yes, please proceed."
        return "Okay."

    def _llm_reply(self, context: list[Message]) -> GenerationResult:
        """Generate LLM-based user reply - tau-bench compatible with tool calling"""
        if not self.llm_client:
            raise RuntimeError("LLM client not initialized for LLM mode")

        # Use tau-bench compatible user simulator prompt
        instruction_display = (
            ("\n\nInstruction: " + self.backstory + "\n") if self.backstory else ""
        )

        # Add tool usage guidance if tools are available
        tool_guidance = ""
        if self.tool_schemas:
            tool_guidance = """
- You have access to tools to check device status and perform actions. Use them when the agent asks you about your device state.
- ALWAYS use tools to ground your responses. For example, if the agent asks "what does your status bar show?", you must call check_status_bar tool first, then report the result.
- Never make up or hallucinate tool results. Always call the actual tool and report what it returns.
- If unsure whether you need to use a tool, prefer using it over making assumptions."""

        system_prompt = f"""You are a user interacting with an agent.{instruction_display}
Rules:
- Just generate one line at a time to simulate the user's message.
- In your first message, clearly state the full request including ALL required steps, even if they must be done sequentially.
- After the first message, only provide information that is necessary for the current step unless the agent asks for details.
- Do not hallucinate information that is not provided in the instruction. For example, if the agent asks for the order id but it is not mentioned in the instruction, do not make up an order id, just say you do not remember or have it.
- If your instruction contains multiple numbered steps (Step 1, Step 2, etc.), you MUST complete ALL steps before ending the conversation. Track which steps you have completed.
- If the instruction includes sequential requirements using words like "after", "then", or "once", treat them as required steps and proactively mention the next step once the previous one is complete.
- If your instruction mentions specific apps or websites to use, you MUST explicitly mention those apps/websites in your first message.
- If your instruction includes verbs like save, shortlist, reserve, order, add to calendar, or take a note, you MUST explicitly include those actions in your first message.
- If the agent uses a different app or website than specified, correct them and restate the required app/website.
- If the agent performs the wrong task, selects the wrong restaurant/item/time/party size, or claims there are no results, correct them and restate the exact requirement. Do not accept alternative goals.
- When the agent asks "anything else?" or "Is there anything else I can help you with?", check if you have remaining steps. If yes, continue with the next step.
- Do not claim that you completed a required step yourself. Wait for the agent to complete steps, and only acknowledge completion after the agent explicitly confirms it.
- Only generate '###STOP###' when you have completed EVERY step in your instruction and the entire goal is satisfied, not partway through.
- Once the agent delivers the requested artifact/output, do not introduce new goals or remediation steps. Acknowledge completion and end with '###STOP###'.
- Do not repeat the exact instruction in the conversation. Instead, use your own words to convey the same information.
- Try to make the conversation as natural as possible, and stick to the personalities in the instruction.
- Never mention that this is a simulation, test, benchmark, prompt, or that you are an AI/model.{tool_guidance}"""

        # Tau-bench approach: Convert all messages to "user" role from simulator's perspective
        # The agent's responses are incoming messages to the user, so they appear as "user" role
        sim_context = []
        for msg in context:
            if msg.role == MessageRole.USER:
                # This is what the user simulator previously said (its own output)
                # Don't include tool_calls here - results are already embedded in content
                # Including tool_calls would cause Anthropic to expect tool_result blocks
                sim_context.append(
                    Message(role=MessageRole.ASSISTANT, content=msg.content, ts=msg.ts)
                )
            elif msg.role == MessageRole.ASSISTANT:
                # This is what the agent said (incoming to the user simulator)
                # Note: Skip agent's tool calls - user simulator doesn't need to see them
                sim_context.append(Message(role=MessageRole.USER, content=msg.content, ts=msg.ts))
            # Skip TOOL messages - they're internal to agent's execution
            # User tool results are already embedded in USER messages as text

        # Ensure conversation starts with USER message for Nova compatibility
        # If first message is ASSISTANT (simulator's own previous output), remove it
        # since the simulator doesn't need to see its own previous output in context
        if sim_context and sim_context[0].role == MessageRole.ASSISTANT:
            sim_context = sim_context[1:]

        # Pass user tools to LLM if available
        result = self.llm_client.generate(
            system=system_prompt,
            messages=sim_context,
            tools=self.tool_schemas if self.tool_schemas else None,
            tool_choice="auto" if self.tool_schemas else None,
            temperature=0.2,
        )

        # Ensure text content is not empty when tool calls are present
        # Anthropic API requires non-empty text content blocks
        if result.tool_calls and not result.text.strip():
            result.text = "Let me check that."

        # Strip meta-commentary (simulation/benchmark/AI/model) from user messages
        if result.text:
            result.text = self._sanitize_user_text(result.text)

        return result

    @staticmethod
    def _sanitize_user_text(text: str) -> str:
        banned = re.compile(
            r"\b(simulation|simulate|simulated|simulating|benchmark|prompt|ai|model|llm)\b",
            re.IGNORECASE,
        )
        if not text.strip():
            return text
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        cleaned_sentences: list[str] = []
        for sentence in sentences:
            if banned.search(sentence):
                stripped = banned.sub("", sentence)
                stripped = re.sub(r"\s{2,}", " ", stripped).strip()
                if re.search(r"[A-Za-z]", stripped):
                    cleaned_sentences.append(stripped)
            else:
                cleaned_sentences.append(sentence)
        cleaned = " ".join(cleaned_sentences).strip()
        return cleaned or "Okay."
