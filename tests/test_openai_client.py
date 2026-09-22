"""OpenAI Chat Completions 协议客户端: 消息/工具转换、chunk→wire 流式组装、
finish_reason 映射、SDK 异常→重试错误、工厂选择。

合成 chunk 用 SimpleNamespace 构造（实现侧全部 getattr 访问, 与真实
openai SDK 对象鸭子兼容）, 不打网络。
"""
try:
    import httpx
except ImportError:   # openai>=3 改用 httpx2 分支包
    import httpx2 as httpx
import openai
import pytest
from types import SimpleNamespace as NS

import api_client
from api_client import (
    OpenAIApiClient,
    ClaudeApiClient,
    make_api_client,
    normalize_protocol,
    WireTextDelta,
    WireThinkingDelta,
    WireToolStart,
    WireToolEnd,
    WireUsage,
    WireStop,
)
from models import (Message, TextContentBlock, ToolContentBlock,
                    ToolResultContentBlock, ImageContentBlock)
import retry as retry_module
from retry import (ApiError as RetryApiError,
                   AuthError as RetryAuthError,
                   ConnectionError as RetryConnectionError,
                   HttpApiError as RetryHttpApiError)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------
def _client(**kw) -> OpenAIApiClient:
    kw.setdefault("emit_output", False)
    return OpenAIApiClient(api_key="k", model="test-model", **kw)


def _chunk(delta=None, finish=None, usage=None):
    choice = NS(delta=delta, finish_reason=finish) if (delta or finish) else None
    return NS(choices=[choice] if choice else [], usage=usage)


def _tool_delta(index, id=None, name=None, args=None):
    fn = NS(name=name, arguments=args) if (name is not None or args is not None) else None
    return NS(index=index, id=id, function=fn)


# ---------------------------------------------------------------------------
# 消息转换
# ---------------------------------------------------------------------------
def test_convert_plain_text_and_system_separate():
    msgs = [Message.user_text("你好")]
    out = api_client._convert_message_openai(msgs)
    assert out == [{"role": "user", "content": "你好"}]


def test_convert_assistant_tool_use_keeps_raw_json():
    m = Message(role="assistant", content=[
        TextContentBlock(text="前置说明"),
        ToolContentBlock(id="tu-1", name="echo", input='{"n": 1.0}'),
    ])
    out = api_client._convert_message_openai([m])
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "前置说明"
    assert out[0]["tool_calls"] == [{
        "id": "tu-1", "type": "function",
        "function": {"name": "echo", "arguments": '{"n": 1.0}'},   # 原样, 不经 dict 往返
    }]


def test_convert_tool_result_maps_to_tool_role():
    tr = Message.tool_result("tu-1", "echo", "结果文本", False)
    out = api_client._convert_message_openai([tr])
    assert out == [{"role": "tool", "tool_call_id": "tu-1", "content": "结果文本"}]


def test_convert_user_image_and_attachment():
    m = Message.user_text("看图")
    m.content.append(ImageContentBlock(source={
        "type": "base64", "media_type": "image/png", "data": "QUJD"}))
    out = api_client._convert_message_openai([m])
    assert out[0]["role"] == "user"
    assert out[0]["content"] == [
        {"type": "text", "text": "看图"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]


def test_convert_drops_empty_assistant():
    m = Message(role="assistant", content=[])
    assert api_client._convert_message_openai([m]) == []


# ---------------------------------------------------------------------------
# 工具声明与 finish_reason
# ---------------------------------------------------------------------------
def test_openai_tools_renames_schema():
    spec = {"name": "echo", "description": "回声",
            "input_schema": {"type": "object", "properties": {}}}
    out = api_client._openai_tools([spec])
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "echo"
    assert out[0]["function"]["parameters"] is spec["input_schema"]   # 同一对象, 未深拷贝
    assert "input_schema" not in out[0]["function"]


@pytest.mark.parametrize("reason,expected", [
    ("tool_calls", "tool_use"),
    ("function_call", "tool_use"),
    ("length", "max_tokens"),
    ("stop", "end_turn"),
    ("content_filter", "end_turn"),
    ("weird_new_reason", "end_turn"),   # 未知值保守按正常结束
    (None, None),
])
def test_finish_reason_mapping(reason, expected):
    assert api_client._openai_finish_reason_to_stop(reason) == expected


# ---------------------------------------------------------------------------
# 流式组装
# ---------------------------------------------------------------------------
def test_stream_text_and_wire_sequence():
    c = _client()
    chunks = [
        _chunk(delta=NS(content="你")),
        _chunk(delta=NS(content="好")),
        _chunk(finish="stop"),
        _chunk(usage=NS(prompt_tokens=10, completion_tokens=5,
                        prompt_tokens_details=None)),
    ]
    wire = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(c.raw_client.chat.completions, "create",
                   lambda **kw: iter(chunks), raising=True)
    try:
        events = c.stream(system_prompt=["sys"], messages=[Message.user_text("hi")],
                          on_event=wire.append)
    finally:
        monkey.undo()

    texts = [e.text for e in events if hasattr(e, "text")]
    assert "".join(texts) == "你好"
    stop = events[-1]
    assert stop.stop_reason == "end_turn"
    assert stop.usage.input_tokens == 10 and stop.usage.output_tokens == 5
    # wire 顺序: 文本delta×2 → Usage → Stop（每次调用恰好一个 Stop）
    assert [type(e) for e in wire] == [WireTextDelta, WireTextDelta, WireUsage, WireStop]


def test_stream_tool_call_reassembly_across_fragments():
    """工具调用跨片: 首片只有 id, 次片补名字, 后续片拼参数——
    id+name 齐了才广播 WireToolStart; finish_reason=tool_calls 收束。"""
    c = _client()
    chunks = [
        _chunk(delta=NS(tool_calls=[_tool_delta(0, id="call-1")])),
        _chunk(delta=NS(tool_calls=[_tool_delta(0, name="echo")])),
        _chunk(delta=NS(tool_calls=[_tool_delta(0, args='{"n":')])),
        _chunk(delta=NS(tool_calls=[_tool_delta(0, args='1}')])),
        _chunk(finish="tool_calls"),
    ]
    wire = []
    pytest.MonkeyPatch().setattr(c.raw_client.chat.completions, "create",
                                 lambda **kw: iter(chunks))
    events = c.stream(system_prompt=[], messages=[Message.user_text("hi")],
                      on_event=wire.append)
    tool_events = [e for e in events if hasattr(e, "id") and hasattr(e, "input")]
    assert len(tool_events) == 1
    assert tool_events[0].id == "call-1"
    assert tool_events[0].name == "echo"
    assert tool_events[0].input == '{"n":1}'
    # wire: ToolStart 恰好一次（名字补齐那片之后）, ToolEnd 带完整参数
    starts = [e for e in wire if isinstance(e, WireToolStart)]
    ends = [e for e in wire if isinstance(e, WireToolEnd)]
    assert len(starts) == 1 and starts[0].name == "echo"
    assert ends[0].input_json == '{"n":1}'
    assert events[-1].stop_reason == "tool_use"


def test_stream_reasoning_content_drives_thinking_only():
    """思考增量只进 wire 与指示器, 不进返回事件（不得污染会话历史）。"""
    c = _client()
    chunks = [
        _chunk(delta=NS(reasoning_content="想一下")),
        _chunk(delta=NS(content="答案")),
        _chunk(finish="stop"),
    ]
    wire = []
    pytest.MonkeyPatch().setattr(c.raw_client.chat.completions, "create",
                                 lambda **kw: iter(chunks))
    events = c.stream(system_prompt=[], messages=[Message.user_text("hi")],
                      on_event=wire.append)
    assert [e.text for e in events if hasattr(e, "text")] == ["答案"]   # 思考未入
    assert any(isinstance(e, WireThinkingDelta) and e.text == "想一下" for e in wire)


def test_stream_kwargs_system_and_tools():
    """请求组装: system 首插、工具按 include_tools 决定、usage 统计开启。"""
    c = _client(tools=[{"name": "echo", "description": "",
                        "input_schema": {"type": "object", "properties": {}}}])
    captured = {}
    def fake_create(**kw):
        captured.update(kw)
        return iter([_chunk(finish="stop")])
    pytest.MonkeyPatch().setattr(c.raw_client.chat.completions, "create", fake_create)
    c.stream(system_prompt=["s1", "s2"], messages=[Message.user_text("hi")])
    assert captured["model"] == "test-model"
    assert captured["messages"][0] == {"role": "system", "content": "s1\n\ns2"}
    assert captured["messages"][1]["role"] == "user"
    assert captured["stream_options"] == {"include_usage": True}
    assert captured["tools"][0]["function"]["name"] == "echo"

    captured.clear()
    c.stream(system_prompt=[], messages=[Message.user_text("hi")], include_tools=False)
    assert "tools" not in captured


# ---------------------------------------------------------------------------
# 错误映射与重试
# ---------------------------------------------------------------------------
def _rate_limit_error():
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    return openai.RateLimitError("429 too many", response=httpx.Response(429, request=req),
                                 body=None)


def test_error_mapping_to_retry_types():
    m = OpenAIApiClient._map_to_retry_error
    req = httpx.Request("POST", "https://x")
    rl = m(_rate_limit_error())
    assert isinstance(rl, RetryHttpApiError) and rl.status_code == 429
    assert isinstance(m(openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=req), body=None)), RetryAuthError)
    assert isinstance(m(openai.APIConnectionError(request=req)), RetryConnectionError)
    assert isinstance(m(openai.APIStatusError(
        "boom", response=httpx.Response(500, request=req), body=None)), RetryHttpApiError)
    assert m(ValueError("其它")) is None


def test_stream_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr(retry_module.time, "sleep", lambda s: None)
    c = _client()
    calls = {"n": 0}
    def flaky_create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error()
        return iter([_chunk(delta=NS(content="ok")), _chunk(finish="stop")])
    monkeypatch.setattr(c.raw_client.chat.completions, "create", flaky_create)
    retried = []
    c.on_retry = lambda attempt, mx, delay, err: retried.append(attempt)
    events = c.stream(system_prompt=[], messages=[Message.user_text("hi")])
    assert "".join(e.text for e in events if hasattr(e, "text")) == "ok"
    assert retried == [1]


def test_stream_auth_error_not_retried(monkeypatch):
    c = _client()
    req = httpx.Request("POST", "https://x")
    def bad_create(**kw):
        raise openai.AuthenticationError("bad key",
                                         response=httpx.Response(401, request=req),
                                         body=None)
    monkeypatch.setattr(c.raw_client.chat.completions, "create", bad_create)
    with pytest.raises(api_client.RetryAuthError):
        c.stream(system_prompt=[], messages=[Message.user_text("hi")])


# ---------------------------------------------------------------------------
# 非流式与工厂
# ---------------------------------------------------------------------------
def test_generate_text_non_stream():
    c = _client()
    captured = {}
    def fake_create(**kw):
        captured.update(kw)
        return NS(choices=[NS(message=NS(content="  标题 内容  "))])
    pytest.MonkeyPatch().setattr(c.raw_client.chat.completions, "create", fake_create)
    out = c.generate_text(system=[], user="起个名")
    assert out == "  标题 内容  "
    assert captured["messages"] == [{"role": "user", "content": "起个名"}]
    assert "stream" not in captured   # 非流式调用


def test_normalize_protocol():
    assert normalize_protocol(None) == "anthropic"
    assert normalize_protocol("") == "anthropic"
    assert normalize_protocol(" OpenAI ") == "openai"
    with pytest.raises(ValueError):
        normalize_protocol("gemini")


def test_make_api_client_factory():
    a = make_api_client("anthropic", api_key="k", model="m", emit_output=False)
    b = make_api_client("openai", api_key="k", model="m", emit_output=False)
    c = make_api_client(None, api_key="k", model="m", emit_output=False)   # 缺省
    assert isinstance(a, ClaudeApiClient) and a.protocol == "anthropic"
    assert isinstance(b, OpenAIApiClient) and b.protocol == "openai"
    assert isinstance(c, ClaudeApiClient)
    with pytest.raises(ValueError):
        make_api_client("unknown-proto", api_key="k", model="m")
