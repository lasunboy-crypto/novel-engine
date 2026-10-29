"""``_retry_feedback`` 通用模块单测(v1.0.x wave 5 session 2 引入)。

覆盖:
1. 反馈消息生成器(json decode / validation error / post-validate)中文模板
2. ``run_with_schema_retry`` 三类失败重试路径(invalid_json / schema / post)
3. 首次成功不重试 + 多次失败终态抛 ``SchemaRetryExhausted``
4. 反馈累加(retry user_text 包含上次错误反馈)
5. ``post_validate`` 接入
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field, ValidationError

from src.core.agents._retry_feedback import (
    SchemaRetryFailure,
    SchemaRetryFailureFingerprint,
    SchemaRetryExhausted,
    format_json_decode_feedback,
    format_post_validate_feedback,
    format_validation_error_feedback,
    run_with_schema_retry,
)
from src.core.llm.base import LLMResponse, Message, TokenUsage


# ---------------------------------------------------------------------------
# 测试模型
# ---------------------------------------------------------------------------


class _Sample(BaseModel):
    name: str = Field(..., min_length=1)
    count: int = Field(..., ge=1, le=10)


# ---------------------------------------------------------------------------
# 反馈消息生成器
# ---------------------------------------------------------------------------


def test_format_json_decode_feedback_含中文与定位信息() -> None:
    try:
        json.loads("{ bad ")
    except json.JSONDecodeError as exc:
        msg = format_json_decode_feedback(exc)
        assert "JSON 解析失败" in msg
        assert "line" in msg.lower() or "1" in msg  # 位置信息
        assert "markdown" in msg.lower()
        assert "不要任何前后缀" in msg


def test_format_validation_error_feedback_含字段定位与中文翻译() -> None:
    try:
        _Sample.model_validate({"name": "", "count": 0})
    except ValidationError as exc:
        msg = format_validation_error_feedback(exc)
        assert "schema 校验失败" in msg
        assert "`name`" in msg or "`count`" in msg
        # count=0 触发 ge=1 → greater_than_equal 中文翻译
        assert "数值小于允许最小值" in msg or "字符串太短" in msg


def test_format_post_validate_feedback_含业务规则错误() -> None:
    exc = ValueError("volumes 长度 5 必须 == total_volumes 3")
    msg = format_post_validate_feedback(exc)
    assert "业务规则不满足" in msg
    assert "volumes 长度 5" in msg


def test_format_validation_error_翻译未知type保留原msg() -> None:
    """validation error 含未知 type 时不丢信息。"""

    # 构造一个非常用错误类型
    class _StrictModel(BaseModel):
        items: list[int] = Field(..., min_length=2)

    try:
        _StrictModel.model_validate({"items": [1]})  # too_short
    except ValidationError as exc:
        msg = format_validation_error_feedback(exc)
        assert "items" in msg
        # too_short 在翻译表里 → 含"数组元素过少"
        assert "数组元素过少" in msg or "items" in msg


# ---------------------------------------------------------------------------
# run_with_schema_retry 核心驱动
# ---------------------------------------------------------------------------


def _make_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="qwen-novel-test",
        usage=TokenUsage(input_tokens=10, output_tokens=20),
    )


def test_same_failure_fingerprint_stops_before_third_purchase() -> None:
    calls = 0

    def llm_call(_messages: list[Message]) -> LLMResponse:
        nonlocal calls
        calls += 1
        return _make_response('{"name": "same", "count": 1}')

    def reject(_result: _Sample) -> None:
        raise ValueError("constraint=rule-a evidence=quote-a field=field-a source=plot")

    def fingerprint(failure: SchemaRetryFailure) -> SchemaRetryFailureFingerprint:
        assert failure.reason == "post_validate_failed"
        return SchemaRetryFailureFingerprint(
            value="rule-a|quote-a|field-a|plot"
        )

    with pytest.raises(SchemaRetryExhausted) as exc_info:
        run_with_schema_retry(
            agent_name="test_agent",
            stage="no_progress",
            system_text="sys",
            initial_user_text="user",
            output_model=_Sample,
            llm_call=llm_call,
            post_validate=reject,
            failure_fingerprint=fingerprint,
            max_attempts=3,
        )

    assert calls == 2
    assert exc_info.value.attempts == 2
    assert exc_info.value.stop_reason == "no_progress"
    assert exc_info.value.failure_fingerprint == "rule-a|quote-a|field-a|plot"


def test_different_failure_field_or_source_can_continue_to_success() -> None:
    responses = [
        '{"name": "field-a", "count": 1}',
        '{"name": "field-b", "count": 1}',
        '{"name": "fixed", "count": 1}',
    ]
    calls = 0

    def llm_call(_messages: list[Message]) -> LLMResponse:
        nonlocal calls
        calls += 1
        return _make_response(responses.pop(0))

    def reject_until_fixed(result: _Sample) -> None:
        if result.name != "fixed":
            raise ValueError(result.name)

    def fingerprint(failure: SchemaRetryFailure) -> SchemaRetryFailureFingerprint:
        return SchemaRetryFailureFingerprint(value=str(failure.error))

    result, attempts, _model = run_with_schema_retry(
        agent_name="test_agent",
        stage="different_failure",
        system_text="sys",
        initial_user_text="user",
        output_model=_Sample,
        llm_call=llm_call,
        post_validate=reject_until_fixed,
        failure_fingerprint=fingerprint,
        max_attempts=3,
    )

    assert result.name == "fixed"
    assert attempts == 3
    assert calls == 3


def test_caller_fail_closed_fingerprint_stops_after_current_response() -> None:
    calls = 0

    def llm_call(_messages: list[Message]) -> LLMResponse:
        nonlocal calls
        calls += 1
        return _make_response('{"name": "ambiguous", "count": 1}')

    def reject(_result: _Sample) -> None:
        raise ValueError("evidence has no candidate field")

    def fingerprint(_failure: SchemaRetryFailure) -> SchemaRetryFailureFingerprint:
        return SchemaRetryFailureFingerprint(
            value="ambiguous-evidence",
            retryable=False,
        )

    with pytest.raises(SchemaRetryExhausted) as exc_info:
        run_with_schema_retry(
            agent_name="test_agent",
            stage="caller_fail_closed",
            system_text="sys",
            initial_user_text="user",
            output_model=_Sample,
            llm_call=llm_call,
            post_validate=reject,
            failure_fingerprint=fingerprint,
            max_attempts=3,
        )

    assert calls == 1
    assert exc_info.value.stop_reason == "caller_fail_closed"


def test_首次成功不重试() -> None:
    """LLM 第一次就返合规 JSON,只调一次,attempts=1。"""
    call_count = 0

    def llm_call(messages: list[Message]) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return _make_response('{"name": "alpha", "count": 5}')

    result, attempts, model_used = run_with_schema_retry(
        agent_name="test_agent",
        stage="happy",
        system_text="sys",
        initial_user_text="user",
        output_model=_Sample,
        llm_call=llm_call,
        max_attempts=3,
    )

    assert result.name == "alpha"
    assert result.count == 5
    assert attempts == 1
    assert call_count == 1
    assert model_used == "qwen-novel-test"


def test_invalid_json_首轮失败_次轮成功() -> None:
    """首次返非法 JSON → 反馈追加 → 第二次成功;attempts=2。"""
    responses = ["not json {", '{"name": "alpha", "count": 3}']
    user_texts_seen: list[str] = []

    def llm_call(messages: list[Message]) -> LLMResponse:
        user_msg = next(m for m in messages if m.role == "user")
        user_texts_seen.append(user_msg.content)
        return _make_response(responses.pop(0))

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="json_recovery",
        system_text="sys",
        initial_user_text="原始任务",
        output_model=_Sample,
        llm_call=llm_call,
        max_attempts=3,
    )

    assert result.count == 3
    assert attempts == 2
    # 第二次 user_text 应含原任务 + JSON 解析失败反馈
    assert user_texts_seen[0] == "原始任务"
    assert "原始任务" in user_texts_seen[1]
    assert "JSON 解析失败" in user_texts_seen[1]


def test_schema_失败_反馈含字段定位_次轮成功() -> None:
    """首次 JSON 解析通过但 schema 不符 → 反馈含字段名 → 二次成功。"""
    responses = [
        '{"name": "", "count": 5}',  # name min_length=1 fail
        '{"name": "fixed", "count": 5}',
    ]
    user_texts_seen: list[str] = []

    def llm_call(messages: list[Message]) -> LLMResponse:
        user_msg = next(m for m in messages if m.role == "user")
        user_texts_seen.append(user_msg.content)
        return _make_response(responses.pop(0))

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="schema_recovery",
        system_text="sys",
        initial_user_text="原始",
        output_model=_Sample,
        llm_call=llm_call,
        max_attempts=3,
    )

    assert result.name == "fixed"
    assert attempts == 2
    # 第二次 user_text 应含 schema 失败反馈 + 字段名 name
    assert "schema 校验失败" in user_texts_seen[1]
    assert "name" in user_texts_seen[1]


def test_normalize_data_在schema前机械修复且不增加请求() -> None:
    """调用方限定的结构补救命中后仍走 schema，且不消耗第二次 LLM。"""
    call_count = 0

    def llm_call(messages: list[Message]) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return _make_response('{"name": "alpha", "misplaced": ["count", 5]}')

    def normalize(data: object) -> object:
        assert isinstance(data, dict)
        if data.get("misplaced") == ["count", 5]:
            return {"name": data["name"], "count": 5}
        return data

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="deterministic_normalizer",
        system_text="sys",
        initial_user_text="原始",
        output_model=_Sample,
        llm_call=llm_call,
        normalize_data=normalize,
        max_attempts=3,
    )

    assert result == _Sample(name="alpha", count=5)
    assert attempts == 1
    assert call_count == 1


def test_post_validate_失败_反馈含业务规则_次轮成功() -> None:
    """schema 通过但 post_validate 业务规则失败 → 反馈追加 → 二次成功。"""
    responses = [
        '{"name": "x", "count": 5}',  # post_validate 拒(count != 7)
        '{"name": "x", "count": 7}',
    ]
    user_texts_seen: list[str] = []

    def llm_call(messages: list[Message]) -> LLMResponse:
        user_msg = next(m for m in messages if m.role == "user")
        user_texts_seen.append(user_msg.content)
        return _make_response(responses.pop(0))

    def must_be_seven(result: _Sample) -> None:
        if result.count != 7:
            raise ValueError(f"count must be 7, got {result.count}")

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="post_recovery",
        system_text="sys",
        initial_user_text="原始",
        output_model=_Sample,
        llm_call=llm_call,
        post_validate=must_be_seven,
        max_attempts=3,
    )

    assert result.count == 7
    assert attempts == 2
    assert "业务规则不满足" in user_texts_seen[1]
    assert "count must be 7" in user_texts_seen[1]


def test_全部失败抛SchemaRetryExhausted() -> None:
    """3 次都返非法 JSON → 抛 SchemaRetryExhausted 含证据。"""

    def llm_call(messages: list[Message]) -> LLMResponse:
        return _make_response("garbage {")

    with pytest.raises(SchemaRetryExhausted) as exc_info:
        run_with_schema_retry(
            agent_name="test_agent",
            stage="dead",
            system_text="sys",
            initial_user_text="user",
            output_model=_Sample,
            llm_call=llm_call,
            max_attempts=3,
        )

    err = exc_info.value
    assert err.stage == "dead"
    assert err.attempts == 3
    assert err.last_model_used == "qwen-novel-test"
    assert err.last_response_content == "garbage {"
    assert isinstance(err.root_exc, json.JSONDecodeError)


def test_max_attempts_无效抛ValueError() -> None:
    def llm_call(messages: list[Message]) -> LLMResponse:
        return _make_response("{}")

    with pytest.raises(ValueError, match="max_attempts"):
        run_with_schema_retry(
            agent_name="test_agent",
            stage="x",
            system_text="sys",
            initial_user_text="user",
            output_model=_Sample,
            llm_call=llm_call,
            max_attempts=0,
        )


def test_反馈累加_多次失败时上一次反馈不丢失() -> None:
    """retry 多次时,user_text 持续累加反馈(不替换),让 LLM 看到全部历史错误。"""
    responses = [
        "not json {",
        '{"name": "", "count": 5}',  # schema fail
        '{"name": "ok", "count": 5}',
    ]
    user_texts_seen: list[str] = []

    def llm_call(messages: list[Message]) -> LLMResponse:
        user_msg = next(m for m in messages if m.role == "user")
        user_texts_seen.append(user_msg.content)
        return _make_response(responses.pop(0))

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="累加",
        system_text="sys",
        initial_user_text="原任务",
        output_model=_Sample,
        llm_call=llm_call,
        max_attempts=3,
    )

    assert attempts == 3
    assert result.name == "ok"
    # 第三次 user_text 应同时含两次错误反馈
    third = user_texts_seen[2]
    assert "原任务" in third
    assert "JSON 解析失败" in third
    assert "schema 校验失败" in third


def test_strip_format_noise_注入正确剥代码块() -> None:
    """LLM 输出含 ```json ... ``` 包裹时,strip_format_noise 应正确剥离。"""

    def llm_call(messages: list[Message]) -> LLMResponse:
        return _make_response('```json\n{"name": "boxed", "count": 5}\n```')

    def strip(text: str) -> str:
        # 简化版剥代码块
        if text.startswith("```"):
            lines = text.split("\n")
            return "\n".join(lines[1:-1])
        return text

    result, attempts, _ = run_with_schema_retry(
        agent_name="test_agent",
        stage="boxed",
        system_text="sys",
        initial_user_text="u",
        output_model=_Sample,
        llm_call=llm_call,
        strip_format_noise=strip,
        max_attempts=3,
    )
    assert result.name == "boxed"
    assert attempts == 1


def test_max_attempts_1_一次失败立即抛() -> None:
    """max_attempts=1 时不重试,首次失败直接抛 SchemaRetryExhausted。"""
    call_count = 0

    def llm_call(messages: list[Message]) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return _make_response("not json")

    with pytest.raises(SchemaRetryExhausted) as exc_info:
        run_with_schema_retry(
            agent_name="test_agent",
            stage="one_shot",
            system_text="sys",
            initial_user_text="u",
            output_model=_Sample,
            llm_call=llm_call,
            max_attempts=1,
        )
    assert call_count == 1
    assert exc_info.value.attempts == 1


@pytest.mark.parametrize("failure_kind", ["invalid_json", "schema", "normalization", "business"])
def test_retry_source_preserves_wire_candidate_and_original_context(failure_kind):
    from src.core.agents.structured_handoff import build_packet, render_packet, prepare_handoff_messages

    initial = render_packet(build_packet(stage="characters", data={"world": {"rule": "原世界"}},
                                         original_task="原任务"), "json")
    first = "{ invalid" if failure_kind == "invalid_json" else json.dumps({"name": "许禾", "count": 0 if failure_kind == "schema" else 1}, ensure_ascii=False)
    contents = [first, '{"name":"许禾","count":2}']
    calls = []

    def call(messages):
        actual = prepare_handoff_messages(messages, agent="outline_builder", mode="json")
        calls.append(actual)
        return _make_response(contents.pop(0))

    def normalize(data):
        if failure_kind == "normalization" and data["count"] == 1:
            data["program_only"] = "程序装配内容不能冒充模型输出"
            raise ValueError("局部引用错误")
        return data

    def validate(result):
        if failure_kind == "business" and result.count == 1:
            raise ValueError("同次旧事时间错误")

    result, attempts, _ = run_with_schema_retry(agent_name="test", stage="characters", system_text="sys",
        initial_user_text=initial, output_model=_Sample, llm_call=call, normalize_data=normalize,
        normalization_retryable=lambda _: True, post_validate=validate, preserve_candidate_on_retry=True)
    assert attempts == 2 and result.name == "许禾" and result.count == 2
    assert len(calls[0]) == 2 and len(calls[1]) == 4
    assert calls[1][2].role == "assistant" and calls[1][2].content == first
    packet = json.loads(calls[1][1].content)
    assert packet["data"] == {"world": {"rule": "原世界"}}
    assert len(packet["retry_feedback"]) == 1
    assert "未通过" in json.loads(calls[1][3].content)["feedback"]
    assert "program_only" not in calls[1][2].content


def test_source_retry_replaces_previous_candidate_instead_of_stacking_versions():
    contents = ['{"name":"第一版","count":1}', '{"name":"第二版","count":2}', '{"name":"通过版","count":3}']
    calls = []

    def call(messages):
        calls.append(messages)
        return _make_response(contents.pop(0))

    def validate(result):
        if result.count < 3:
            raise ValueError(f"错误{result.count}")

    result, attempts, _ = run_with_schema_retry(agent_name="test", stage="characters", system_text="sys",
        initial_user_text="原任务", output_model=_Sample, llm_call=call, post_validate=validate,
        preserve_candidate_on_retry=True)
    assert result.count == attempts == len(calls) == 3
    assert len(calls[2]) == 4
    assert calls[2][2].content == '{"name":"第二版","count":2}'
    assert "第一版" not in "".join(m.content for m in calls[2])
    assert "错误1" in calls[2][1].content and "错误2" in calls[2][1].content


def test_default_retry_still_uses_legacy_two_message_protocol():
    responses = ['{"name":"旧稿","count":0}', '{"name":"旧稿","count":1}']
    calls = []
    def call(messages):
        calls.append(messages)
        return _make_response(responses.pop(0))
    run_with_schema_retry(agent_name="test", stage="legacy", system_text="sys", initial_user_text="user",
                          output_model=_Sample, llm_call=call)
    assert [len(messages) for messages in calls] == [2, 2]
