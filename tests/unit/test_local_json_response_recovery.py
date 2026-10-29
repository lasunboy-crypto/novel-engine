import json

import pytest
import structlog

from src.config import settings
from src.core.agents._retry_feedback import SchemaRetryExhausted, run_with_schema_retry
from src.core.llm.local_json_contract import LocalJsonResponseViolation
from tests.unit.test_experimental_pipeline_contract import Decision, make_router, prepared


@pytest.mark.parametrize('bad', ['', 'not json', '{"actor_id":"actor-2","steps":["a"]}', '{"actor_id":"actor-1","steps":[],"extra":1}'])
def test_invalid_raw_response_reaches_existing_feedback_and_is_archived(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(settings, 'data_dir', tmp_path / 'workspaces')
    router, _, clients = make_router(tmp_path)
    target = router.route('outline_builder')
    kwargs = prepared(target)
    calls = []
    def llm_call(messages):
        calls.append(messages[-1].content)
        clients[0].content = bad if len(calls) == 1 else '{"actor_id":"actor-1","steps":["fixed"]}'
        return target.client.complete(messages, **kwargs)
    with structlog.contextvars.bound_contextvars(workspace_id='local-response-test'):
        result, attempts, _ = run_with_schema_retry(agent_name='outline_builder', stage='volume_plans',
            system_text='Return JSON', initial_user_text='Make a decision', output_model=Decision,
            llm_call=llm_call, max_attempts=2)
    assert attempts == 2 and result.actor_id == 'actor-1'
    assert calls[0] != calls[1]
    archives = list((settings.data_dir/'local-response-test/archives/responses').glob('*/*.json'))
    assert len(archives) == 2
    assert bad in [json.loads(p.read_text())['content'] for p in archives]


def test_exhaustion_preserves_last_raw_and_wire_contract_cannot_be_bypassed(tmp_path):
    router, _, clients = make_router(tmp_path)
    target = router.route('outline_builder')
    kwargs = prepared(target)
    clients[0].content = '{"actor_id":"actor-2","steps":["a"]}'
    with pytest.raises(SchemaRetryExhausted) as error:
        run_with_schema_retry(agent_name='outline_builder', stage='volume_plans',
            system_text='Return JSON', initial_user_text='Make a decision', output_model=Decision,
            llm_call=lambda messages: target.client.complete(messages, **kwargs), max_attempts=2)
    assert error.value.attempts == 2
    assert error.value.last_response_content == clients[0].content
    assert len(clients[0].calls) == 2
    with pytest.raises(LocalJsonResponseViolation):
        target.client.complete([], **kwargs)


def test_model_local_reference_errors_use_opt_in_feedback_without_mutating_input():
    from src.core.agents.creative_delta import CreativeDeltaAssemblyError
    from src.core.llm.base import LLMResponse, TokenUsage
    import copy

    calls = []
    inputs = []
    raw = {'actor_id': 'actor-1', 'steps': ['original']}
    def llm_call(messages):
        calls.append(messages[-1].content)
        return LLMResponse(content=json.dumps(raw), model_used='deepseek-v4-flash',
                           usage=TokenUsage(input_tokens=1, output_tokens=1))
    def assemble(data):
        inputs.append(copy.deepcopy(data))
        if len(inputs) == 1:
            raise CreativeDeltaAssemblyError('ambiguous_local_reference', 'moment[6]', 'finale already occupies this beat')
        return data
    result, attempts, _ = run_with_schema_retry(
        agent_name='outline_builder', stage='volume_plans', system_text='JSON', initial_user_text='design',
        output_model=Decision, llm_call=llm_call, normalize_data=assemble,
        normalization_retryable=lambda exc: isinstance(exc, CreativeDeltaAssemblyError), max_attempts=2)
    assert attempts == 2 and result.steps == ['original']
    assert inputs == [raw, raw]
    assert 'finale already occupies this beat' in calls[1]
    with pytest.raises(CreativeDeltaAssemblyError):
        run_with_schema_retry(agent_name='outline_builder', stage='volume_plans', system_text='JSON',
            initial_user_text='design', output_model=Decision, llm_call=llm_call,
            normalize_data=lambda data: (_ for _ in ()).throw(CreativeDeltaAssemblyError('authority_drift', 'source', 'changed')),
            max_attempts=2)


def test_prompt_json_fence_is_unwrapped_but_raw_archive_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path / 'workspaces')
    router, _, clients = make_router(tmp_path, structured_mode='prompt_json_local_schema')
    target = router.route('chapter_card_generator')
    kwargs = prepared(target)
    raw = '```json\n{"actor_id":"actor-1","steps":["action"]}\n```'
    clients[0].content = raw
    with structlog.contextvars.bound_contextvars(workspace_id='fenced-json-test'):
        response = target.client.complete([], **kwargs)
    assert json.loads(response.content)['steps'] == ['action']
    archives = list((settings.data_dir/'fenced-json-test/archives/responses').glob('*/*.json'))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())['content'] == raw
    assert clients[0].content == raw
    assert len(clients[0].calls) == 1


@pytest.mark.parametrize('raw', [
    'explanation\n```json\n{"actor_id":"actor-1","steps":["a"]}\n```',
    '```json\n{"actor_id":"actor-2","steps":["a"]}\n```',
    '```json\n{"actor_id":"actor-1","steps":[],"extra":1}\n```',
    '```json\n{"actor_id":"actor-1","steps":["a"]}\n```\n{}',
])
def test_prompt_json_wrapper_never_relaxes_schema_or_extracts_partial_objects(tmp_path, raw):
    router, _, clients = make_router(tmp_path, structured_mode='prompt_json_local_schema')
    target = router.route('chapter_card_generator')
    clients[0].content = raw
    with pytest.raises(LocalJsonResponseViolation):
        target.client.complete([], **prepared(target))
    assert len(clients[0].calls) == 1
