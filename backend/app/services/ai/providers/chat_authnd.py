"""LangChain ChatModel implementation for AuthND (NVIDIA Build browser-backed route).

Allows NovelForge to invoke AuthND models (Kimi, DeepSeek, Nemotron, etc.)
as standard LangChain ChatModel instances with streaming, async support and
structured output (instruction mode + JSON repair + Pydantic validation, since
the NVIDIA browser route exposes no native tool/JSON-schema mode).
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence, Type, Union

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.ai.providers import authnd_auth

try:
    from json_repair import repair_json as _repair_json
except Exception:  # pragma: no cover - optional dependency
    _repair_json = None


def _convert_message_to_dict(message: BaseMessage) -> Dict[str, Any]:
    content = message.content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(str(part["text"]))
        text_content = "\n".join(text_parts)
    else:
        text_content = str(content or "")

    if isinstance(message, HumanMessage):
        return {"role": "user", "content": text_content}
    elif isinstance(message, AIMessage):
        return {"role": "assistant", "content": text_content}
    elif isinstance(message, SystemMessage):
        return {"role": "system", "content": text_content}
    elif isinstance(message, (ToolMessage, FunctionMessage)):
        return {"role": "user", "content": f"[Tool Result]: {text_content}"}
    elif isinstance(message, ChatMessage):
        return {"role": message.role, "content": text_content}
    else:
        return {"role": "user", "content": text_content}


class StructuredOutputError(ValueError):
    """Model output could not be parsed/validated into the requested schema."""


_FENCE_RX = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json_text(text: str) -> str:
    """Pull the most plausible JSON document out of free-form model text."""
    raw = (text or "").strip()
    if not raw:
        return raw
    m = _FENCE_RX.search(raw)
    if m and m.group(1).strip():
        raw = m.group(1).strip()
    # Trim leading prose before the first bracket and trailing prose after the last.
    first_obj, first_arr = raw.find("{"), raw.find("[")
    starts = [i for i in (first_obj, first_arr) if i >= 0]
    if starts:
        start = min(starts)
        end = max(raw.rfind("}"), raw.rfind("]"))
        if end > start:
            raw = raw[start:end + 1]
    return raw


def parse_structured_text(text: str, schema: Type[BaseModel]) -> BaseModel:
    """Parse (with repair) and validate model text against a Pydantic schema."""
    candidate = extract_json_text(text)
    if not candidate:
        raise StructuredOutputError("model returned no JSON")
    data: Any = None
    try:
        data = json.loads(candidate)
    except Exception:
        if _repair_json is None:
            raise StructuredOutputError("model output is not valid JSON and json_repair is unavailable")
        try:
            repaired = _repair_json(candidate, return_objects=True)
        except Exception as exc:
            raise StructuredOutputError(f"JSON repair failed: {exc}")
        data = repaired
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            raise StructuredOutputError("model output is a bare string, not an object")
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise StructuredOutputError(f"model output is {type(data).__name__}, expected an object")
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise StructuredOutputError(f"schema validation failed: {exc.errors()[:5]}")


def structured_instruction(schema: Type[BaseModel]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        "You must answer with ONE JSON object only — no prose, no markdown fences, no comments. "
        "The object must validate against this JSON Schema (respect required fields, enums and types; "
        "use null for unknown optional values):\n" + schema_json
    )


class ChatAuthND(BaseChatModel):
    """AuthND (NVIDIA Build browser-backed route) ChatModel for inference."""

    model_name: str = Field(default=authnd_auth.DEFAULT_MODEL, alias="model")
    temperature: Optional[float] = 0.3
    max_tokens: Optional[int] = 32768
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    timeout: Optional[float] = None
    connect_timeout: Optional[float] = None
    proxy: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    streaming: bool = False
    structured_max_attempts: int = 2

    model_config = ConfigDict(populate_by_name=True)

    @property
    def _llm_type(self) -> str:
        return "authnd"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "proxy": self.proxy,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
        }

    def _convert_messages(self, messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        return [_convert_message_to_dict(m) for m in messages]

    def _request_kwargs(self, **kwargs: Any) -> Dict[str, Any]:
        timeout_int = int(self.timeout) if self.timeout is not None else None
        return dict(
            model=self.model_name,
            temperature=self.temperature if self.temperature is not None else kwargs.get("temperature", 0.3),
            max_tokens=self.max_tokens if self.max_tokens is not None else kwargs.get("max_tokens", 32768),
            top_p=self.top_p if self.top_p is not None else kwargs.get("top_p"),
            frequency_penalty=self.frequency_penalty if self.frequency_penalty is not None else kwargs.get("frequency_penalty"),
            presence_penalty=self.presence_penalty if self.presence_penalty is not None else kwargs.get("presence_penalty"),
            timeout=timeout_int or 180,
            connect_timeout=self.connect_timeout or 60.0,
            proxy=self.proxy or kwargs.get("proxy"),
            reasoning_enabled=self.thinking_enabled if self.thinking_enabled is not None else kwargs.get("reasoning_enabled"),
            reasoning_effort=self.reasoning_effort if self.reasoning_effort is not None else kwargs.get("reasoning_effort"),
        )

    @staticmethod
    def _to_result(result: Dict[str, Any]) -> ChatResult:
        content = result.get("content") or ""
        reasoning = result.get("reasoning_content")
        usage = result.get("usage")
        additional_kwargs: Dict[str, Any] = {}
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        if usage:
            additional_kwargs["usage"] = usage
        usage_metadata = None
        if isinstance(usage, dict):
            usage_metadata = {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        message = AIMessage(content=content, additional_kwargs=additional_kwargs, usage_metadata=usage_metadata)
        return ChatResult(generations=[ChatGeneration(message=message, generation_info={"finish_reason": result.get("finish_reason")})], llm_output={"usage": usage})

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = authnd_auth.send_chat_completion(
            messages=self._convert_messages(messages),
            stream=False,
            control=kwargs.get("control"),
            **self._request_kwargs(**kwargs),
        )
        return self._to_result(result)

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Own the request control so asyncio cancellation (workflow cancel, client
        # disconnect) stops exactly this request's stream/browser helper.
        control = kwargs.pop("control", None) or authnd_auth.RequestControl()
        task = asyncio.get_running_loop().run_in_executor(None, lambda: self._generate(messages, stop, None, control=control, **kwargs))
        try:
            return await task
        except asyncio.CancelledError:
            control.cancel()
            raise

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        dict_messages = self._convert_messages(messages)
        chunk_queue: "queue.Queue[Optional[tuple[str, Optional[str]]]]" = queue.Queue()
        error_holder: list[Exception] = []
        control = kwargs.pop("control", None) or authnd_auth.RequestControl()
        req_kwargs = self._request_kwargs(**kwargs)

        def _on_chunk(text: str, reasoning: Optional[str]) -> None:
            chunk_queue.put((text, reasoning))

        def _worker() -> None:
            try:
                authnd_auth.send_chat_completion(messages=dict_messages, stream=True, chunk_callback=_on_chunk, control=control, **req_kwargs)
            except Exception as exc:
                error_holder.append(exc)
            finally:
                chunk_queue.put(None)

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        try:
            while True:
                item = chunk_queue.get()
                if item is None:
                    break
                text, reasoning = item
                if text or reasoning:
                    additional_kwargs: Dict[str, Any] = {}
                    if reasoning:
                        additional_kwargs["reasoning_content"] = reasoning
                    chunk = ChatGenerationChunk(message=AIMessageChunk(content=text, additional_kwargs=additional_kwargs))
                    if run_manager:
                        run_manager.on_llm_new_token(text, chunk=chunk)
                    yield chunk
        except GeneratorExit:
            control.cancel()
            raise
        if error_holder:
            raise error_holder[0]

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        dict_messages = self._convert_messages(messages)
        loop = asyncio.get_running_loop()
        async_queue: "asyncio.Queue[Optional[tuple[str, Optional[str]]]]" = asyncio.Queue()
        error_holder: list[Exception] = []
        control = kwargs.pop("control", None) or authnd_auth.RequestControl()
        req_kwargs = self._request_kwargs(**kwargs)

        def _on_chunk(text: str, reasoning: Optional[str]) -> None:
            loop.call_soon_threadsafe(async_queue.put_nowait, (text, reasoning))

        def _worker() -> None:
            try:
                authnd_auth.send_chat_completion(messages=dict_messages, stream=True, chunk_callback=_on_chunk, control=control, **req_kwargs)
            except Exception as exc:
                error_holder.append(exc)
            finally:
                loop.call_soon_threadsafe(async_queue.put_nowait, None)

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        try:
            while True:
                item = await async_queue.get()
                if item is None:
                    break
                text, reasoning = item
                if text or reasoning:
                    additional_kwargs: Dict[str, Any] = {}
                    if reasoning:
                        additional_kwargs["reasoning_content"] = reasoning
                    chunk = ChatGenerationChunk(message=AIMessageChunk(content=text, additional_kwargs=additional_kwargs))
                    if run_manager:
                        await run_manager.on_llm_new_token(text, chunk=chunk)
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            control.cancel()
            raise
        if error_holder:
            raise error_holder[0]

    # ------------------------------------------------------------ structured
    def with_structured_output(
        self,
        schema: Union[Dict, Type[BaseModel]],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable:
        """Instruction-mode structured output with JSON repair and Pydantic validation.

        The browser route has no native JSON-schema mode, so the schema is
        injected as a system instruction, the reply is parsed (repairing minor
        JSON damage) and validated. A validation failure is retried once with
        the validation errors fed back to the model; the final failure raises
        ``StructuredOutputError`` so callers never persist unvalidated data.
        """
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise NotImplementedError("ChatAuthND structured output requires a Pydantic model class")
        model_cls: Type[BaseModel] = schema
        instruction = structured_instruction(model_cls)
        attempts = max(1, int(self.structured_max_attempts))

        def _prepare(messages: Any) -> List[BaseMessage]:
            if isinstance(messages, str):
                msgs: List[BaseMessage] = [HumanMessage(content=messages)]
            elif hasattr(messages, "to_messages"):
                msgs = list(messages.to_messages())
            else:
                msgs = list(messages)
            if msgs and isinstance(msgs[0], SystemMessage):
                msgs[0] = SystemMessage(content=f"{msgs[0].content}\n\n{instruction}")
            else:
                msgs.insert(0, SystemMessage(content=instruction))
            return msgs

        def _finish(raw: AIMessage, parsed: Optional[BaseModel], error: Optional[Exception]):
            if include_raw:
                return {"raw": raw, "parsed": parsed, "parsing_error": error}
            if error is not None:
                raise error
            return parsed

        def _invoke(messages: Any, config: Any = None) -> Any:
            msgs = _prepare(messages)
            last_error: Optional[Exception] = None
            raw = AIMessage(content="")
            for attempt in range(attempts):
                raw = self.invoke(msgs, config=config)
                try:
                    return _finish(raw, parse_structured_text(str(raw.content), model_cls), None)
                except StructuredOutputError as exc:
                    last_error = exc
                    msgs = msgs + [raw, HumanMessage(content=f"Your previous answer was rejected: {exc}. Reply again with ONE corrected JSON object only.")]
            return _finish(raw, None, last_error)

        async def _ainvoke(messages: Any, config: Any = None) -> Any:
            msgs = _prepare(messages)
            last_error: Optional[Exception] = None
            raw = AIMessage(content="")
            for attempt in range(attempts):
                raw = await self.ainvoke(msgs, config=config)
                try:
                    return _finish(raw, parse_structured_text(str(raw.content), model_cls), None)
                except StructuredOutputError as exc:
                    last_error = exc
                    msgs = msgs + [raw, HumanMessage(content=f"Your previous answer was rejected: {exc}. Reply again with ONE corrected JSON object only.")]
            return _finish(raw, None, last_error)

        return RunnableLambda(_invoke, afunc=_ainvoke)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable:
        raise NotImplementedError("AuthND browser route does not support native tool calling; use the ReAct text agent mode.")
