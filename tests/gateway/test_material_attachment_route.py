"""Exact document requests may produce an attachment directive, never a guess."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from gateway.after_sales_guard import _exact_material_attachment
from gateway.platforms.base import BasePlatformAdapter


def _source(root: Path):
    directory = root / 'CNV'
    directory.mkdir()
    file = directory / 'SOP-JL-185 B3 文库构建.pdf'
    file.write_bytes(b'fixture')
    return {'status': 'source_candidates_found', 'identity': {'sop_id': 'SOP-JL-185', 'revision': 'B3', 'file_format': 'pdf'},
            'candidates': [{'file_name': file.name, 'relative_path': 'CNV/' + file.name,
                            'resolved_path': str(file.resolve()), 'authority': 'locator_only'}]}


def test_exact_single_document_yields_parseable_attachment(tmp_path):
    source = _source(tmp_path)
    result = _exact_material_attachment(source, root=tmp_path)
    media, text = BasePlatformAdapter.extract_media(result)
    assert media == [(source['candidates'][0]['resolved_path'], False)]
    assert '已发送' not in text


def test_ambiguous_missing_or_wrong_identity_never_yields_attachment(tmp_path):
    source = _source(tmp_path)
    assert not _exact_material_attachment({**source, 'candidates': source['candidates'] * 2}, root=tmp_path)
    assert not _exact_material_attachment({**source, 'identity': {**source['identity'], 'revision': ''}}, root=tmp_path)
    assert not _exact_material_attachment({**source, 'identity': {**source['identity'], 'revision': 'B1'}}, root=tmp_path)
    assert not _exact_material_attachment({**source, 'identity': {**source['identity'], 'file_format': ''}}, root=tmp_path)


def test_path_escape_and_mismatched_path_are_rejected(tmp_path):
    source = _source(tmp_path)
    item = source['candidates'][0]
    assert not _exact_material_attachment({**source, 'candidates': [{**item, 'relative_path': '../escape.pdf'}]}, root=tmp_path)
    assert not _exact_material_attachment({**source, 'candidates': [{**item, 'resolved_path': '/outside/SOP-JL-185 B3.pdf'}]}, root=tmp_path)


def test_renderer_prepares_attachment_only_for_explicit_send(tmp_path, monkeypatch):
    import types
    import gateway.after_sales_guard as guard_module
    source = _source(tmp_path)
    plan = {'fast_path': {'hit': False, 'disposition': 'miss'},
            'task_contract': {'intent': 'sop_lookup'}, 'source_location': source,
            'runtime_preflight': {'eligible': False},
            'preflight_gate': {'decision': 'allow', 'pipeline_action': 'continue_final_answer'},
            'expert_route_contract': {'status': 'blocked', 'formal_answer_allowed': False,
                                      'bounded_expert_answer_allowed': False}}
    locator = types.FunctionType((lambda: None).__code__, {'_default_material_root': lambda: tmp_path})
    module = types.SimpleNamespace(build_fast_response_plan=lambda message, question_type: plan,
                                   locate_formal_sources=locator)
    module_path = tmp_path / 'scripts' / 'fast.py'
    module_path.parent.mkdir()
    module_path.write_text('# synthetic planner')
    monkeypatch.setattr(guard_module, '_load_module', lambda *args: module)
    monkeypatch.setattr(guard_module, '_load_presentation_policy', lambda *args, **kwargs: (None, {}))
    result = guard_module._render_fast_response_context({'fast_response_module': str(module_path)},
                message='请发送 SOP-JL-185 B3 PDF', match=None)
    assert 'MEDIA:' in result['direct_response']
    turn = guard_module.AfterSalesTurn(context='', facts={}, validator=None, allowed_numeric_claims=(),
        direct_response=result['direct_response'], preflight_issues=result['preflight_issues'])
    final = guard_module.build_preflight_block_result(turn, '请发送 SOP-JL-185 B3 PDF')['final_response']
    assert BasePlatformAdapter.extract_media(final)[0] == [(source['candidates'][0]['resolved_path'], False)]
    other = guard_module._render_fast_response_context({'fast_response_module': str(module_path)},
                message='SOP-JL-185 B3 的条件是什么', match=None)
    assert 'MEDIA:' not in other['direct_response']
    explanation = guard_module._render_fast_response_context({'fast_response_module': str(module_path)},
                message='给我解释 SOP-JL-185 B3 PDF 的原理', match=None)
    assert 'MEDIA:' not in explanation['direct_response']


@pytest.mark.asyncio
async def test_prepared_directive_reaches_mock_document_adapter(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    source = _source(tmp_path)
    response = _exact_material_attachment(source, root=tmp_path)
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    adapter = SimpleNamespace(name='qq-test', extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images, send_document=AsyncMock(),
        send_voice=AsyncMock(), send_video=AsyncMock(), send_multiple_images=AsyncMock())
    event = SimpleNamespace(source=SimpleNamespace(chat_id='test-group', platform=Platform.QQBOT))
    runner = SimpleNamespace(_thread_metadata_for_source=lambda *args: {},
                             _reply_anchor_for_event=lambda *args: None)
    await GatewayRunner._deliver_media_from_response(runner, response, event, adapter)
    adapter.send_document.assert_awaited_once()
    assert adapter.send_document.await_args.kwargs['file_path'] == source['candidates'][0]['resolved_path']


@pytest.mark.asyncio
async def test_failed_document_upload_is_reported_without_exposing_error(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    source = _source(tmp_path)
    response = _exact_material_attachment(source, root=tmp_path)
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    adapter = SimpleNamespace(name='qq-test', extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send_document=AsyncMock(return_value=SendResult(success=False, error='secret diagnostic')),
        send=AsyncMock(return_value=SendResult(success=True)),
        send_voice=AsyncMock(), send_video=AsyncMock(), send_multiple_images=AsyncMock())
    event = SimpleNamespace(source=SimpleNamespace(chat_id='test-group', platform=Platform.QQBOT))
    runner = SimpleNamespace(_thread_metadata_for_source=lambda *args: {},
                             _reply_anchor_for_event=lambda *args: None)
    await GatewayRunner._deliver_media_from_response(runner, response, event, adapter)
    adapter.send.assert_awaited_once()
    text = adapter.send.await_args.kwargs['content']
    assert '发送失败' in text
    assert 'secret diagnostic' not in text
