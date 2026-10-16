"""Provider adapters against mocked HTTP: request shape, response parsing, error mapping."""

from __future__ import annotations

import json

import anthropic
import httpx
import httpx2
import pytest

from sdlc.llm.anthropic_provider import AnthropicProvider
from sdlc.llm.base import FatalLLMError, RefusalError, ToolResult, ToolSpec, TransientLLMError
from sdlc.llm.openai_compat import OpenAICompatProvider

TOOLS = [
    ToolSpec(
        "read_file",
        "read",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
]


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message(stop_reason: str, blocks: list[dict]) -> bytes:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 4000,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
    ]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            events += [
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": b["text"]},
                },
            ]
        else:
            events += [
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}},
                },
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])},
                },
            ]
        events.append({"type": "content_block_stop", "index": i})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 200},
        },
        {"type": "message_stop"},
    ]
    return _sse(events)


def _provider(handler) -> AnthropicProvider:
    p = AnthropicProvider(api_key="test-key")
    p.client = anthropic.Anthropic(
        api_key="test-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    return p


def test_anthropic_request_shape_and_tool_parsing():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_message(
                "tool_use",
                [
                    {"type": "text", "text": "Reading the entrypoint."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "app/main.py"},
                    },
                ],
            ),
        )

    p = _provider(handler)
    messages = p.initial_messages("do the task")
    resp = p.complete(
        system="SYSTEM",
        messages=messages,
        tools=TOOLS,
        model="claude-opus-5-5",
        max_tokens=32000,
        effort="high",
    )
    body = seen["body"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # frozen prefix cached
    assert body["cache_control"] == {"type": "ephemeral"}  # rolling history cached
    assert body["stream"] is True and body["tools"][0]["name"] == "read_file"
    assert resp.tool_calls[0].name == "read_file" and resp.tool_calls[0].input == {"path": "app/main.py"}
    assert resp.text == "Reading the entrypoint." and resp.stop_reason == "tool_use"
    assert resp.usage.cache_read_tokens == 4000
    # 1000*$4 + 200*$20 + 4000*$0.20 per MTok
    assert resp.cost_usd == pytest.approx((1000 * 4 + 200 * 20 + 4000 * 0.2) / 1e6)

    p.append_exchange(messages, resp, [ToolResult("toolu_1", "print('hi')")])
    assert messages[-1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "print('hi')",
        "is_error": False,
    }


def test_anthropic_error_mapping():
    def status(code):
        return lambda request: httpx2.Response(
            code, json={"type": "error", "error": {"type": "x", "message": f"status {code}"}}
        )

    with pytest.raises(FatalLLMError):
        _provider(status(401)).complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=TOOLS,
            model="claude-opus-5-5",
            max_tokens=100,
            effort="low",
        )
    for code in (429, 500, 529):
        with pytest.raises(TransientLLMError):
            _provider(status(code)).complete(
                system="s",
                messages=[{"role": "user", "content": "x"}],
                tools=TOOLS,
                model="claude-opus-5-5",
                max_tokens=100,
                effort="low",
            )


def test_anthropic_midstream_disconnect_is_transient():
    def handler(request):
        raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body")

    with pytest.raises(TransientLLMError):
        _provider(handler).complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=TOOLS,
            model="claude-opus-5-5",
            max_tokens=100,
            effort="low",
        )


def test_anthropic_refusal_raises():
    p = _provider(
        lambda request: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_message("refusal", [])
        )
    )
    with pytest.raises(RefusalError):
        p.complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=TOOLS,
            model="claude-opus-5-5",
            max_tokens=100,
            effort="low",
        )


def test_openai_compatible_adapter(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002 - mirrors httpx
        assert url.endswith("/chat/completions") and json["tools"][0]["function"]["name"] == "read_file"
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    p = OpenAICompatProvider(api_key="k")
    resp = p.complete(
        system="s",
        messages=p.initial_messages("go"),
        tools=TOOLS,
        model="gpt-x",
        max_tokens=10,
        effort="high",
    )
    assert resp.tool_calls[0].input == {"path": "a.py"} and resp.stop_reason == "tool_use"
