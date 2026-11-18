"""Real local source search and conditional-answer fallback behavior."""
import pytest
import shutil
import zipfile
import subprocess
from unittest.mock import Mock

from gateway.ivd_source_search import search_sources
from tests.gateway.test_expert_fallback_user_preference import prepare


@pytest.mark.parametrize('group_mode', ['available', 'unavailable', 'error'])
def test_document_worker_timeout_cleans_up_on_each_platform(monkeypatch, tmp_path, group_mode):
    from gateway import ivd_source_search
    proc = Mock(pid=123)
    proc.communicate.side_effect = [subprocess.TimeoutExpired('worker', 0.1), ('', '')]
    monkeypatch.setattr(ivd_source_search.subprocess, 'Popen', lambda *a, **k: proc)
    kill_group = (None if group_mode == 'unavailable' else
                  Mock(side_effect=OSError('unavailable')) if group_mode == 'error' else Mock())
    monkeypatch.setattr(ivd_source_search.os, 'killpg', kill_group, raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        ivd_source_search._run_document_worker([tmp_path], ['NIFTY'], 'NIFTY', 0.1)
    if group_mode == 'available':
        kill_group.assert_called_once()
        proc.kill.assert_not_called()
    else:
        proc.kill.assert_called_once()
    assert proc.communicate.call_count == 2


def test_search_returns_actual_file_lines_and_retains_distinct_versions(tmp_path):
    (tmp_path / 'NIFTY-A1.md').write_text('NIFTY 提取血浆的条件：手工方案。\n', encoding='utf-8')
    (tmp_path / 'NIFTY-A2.md').write_text('NIFTY 提取血浆的条件：自动方案。\n', encoding='utf-8')
    (tmp_path / 'other.md').write_text('WES 建库质控。\n', encoding='utf-8')
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    assert result['status'] == 'complete'
    assert {item['path'] for item in result['matches']} == {
        str(tmp_path / 'NIFTY-A1.md'), str(tmp_path / 'NIFTY-A2.md')}
    assert all(item['line'] == 1 and '血浆' in item['text'] for item in result['matches'])


def test_search_does_not_claim_success_when_root_is_missing(tmp_path):
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path / 'missing'])
    assert result['status'] == 'unavailable'
    assert not result['matches']


def test_search_does_not_claim_complete_when_time_budget_is_exhausted(tmp_path):
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path], timeout=0)
    assert result['status'] != 'complete'
    assert not result['matches']


def test_search_reports_missing_ripgrep_without_stopping_fallback(tmp_path, monkeypatch):
    from gateway import ivd_source_search
    monkeypatch.setattr(ivd_source_search.shutil, 'which', lambda _name: None)
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    assert result['status'] == 'unavailable' and not result['matches']


def test_unexpected_search_io_failure_still_reaches_expert(monkeypatch):
    from gateway import after_sales_guard, ivd_source_search
    monkeypatch.setattr(after_sales_guard, '_validated_conditional_preflight', lambda _turn: '')

    def fail(*_args, **_kwargs):
        raise OSError('synthetic search error')

    monkeypatch.setattr(ivd_source_search, 'search_sources', fail)
    turn = prepare('NIFTY提取需要多少血浆')
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.blocks_answer_generation and not turn.direct_response
    assert 'unavailable' in turn.context


def test_search_does_not_follow_file_symlinks_outside_source_roots(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('NIFTY 提取血浆：不应读取的文件。\n', encoding='utf-8')
    (root / 'link.md').symlink_to(outside)
    result = search_sources('NIFTY提取需要多少血浆', [root])
    assert not result['matches']


def test_search_extracts_word_content_with_honest_location(tmp_path):
    path = tmp_path / 'NIFTY-A1.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>NIFTY 提取血浆采用手工方法。</w:t></w:r></w:p></w:body></w:document>')
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    match = next(item for item in result['matches'] if item['path'] == str(path))
    assert '血浆' in match['text']
    assert match['location_type'] == 'extracted_line'


def test_word_search_still_works_when_ripgrep_is_missing(tmp_path, monkeypatch):
    from gateway import ivd_source_search
    path = tmp_path / 'NIFTY-A1.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>NIFTY 提取血浆采用手工方式。</w:t></w:r></w:p></w:body></w:document>')
    monkeypatch.setattr(ivd_source_search.shutil, 'which', lambda _name: None)
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    assert result['status'] == 'partial'
    assert any(item['path'] == str(path) for item in result['matches'])


def test_word_search_keeps_product_and_answer_from_different_paragraphs(tmp_path):
    path = tmp_path / 'manual.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>NIFTY</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>手工提取需要使用血浆。</w:t></w:r></w:p></w:body></w:document>')
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    assert any(item['path'] == str(path) and '血浆' in item['text'] for item in result['matches'])


def test_search_extracts_visible_excel_sheet(tmp_path):
    source = tmp_path / 'NIFTY-A1.xlsx'
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('xl/workbook.xml',
            f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
            '<sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml',
            f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr">'
            '<is><t>NIFTY 提取血浆采用手工方式。</t></is></c></row></sheetData></worksheet>')
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    match = next(item for item in result['matches'] if item['path'] == str(source))
    assert '血浆' in match['text'] and match['location_type'] == 'extracted_line'


def test_search_ignores_pdf_and_images_even_when_converter_is_available(tmp_path, monkeypatch):
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'rg').symlink_to(shutil.which('rg'))
    marker = tmp_path / 'converter-was-run'
    converter = binary / 'pdftotext'
    converter.write_text(f'#!/bin/sh\nprintf x > "{marker}"\nprintf "NIFTY 提取血浆\\n"\n', encoding='utf-8')
    converter.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary))
    (tmp_path / 'NIFTY-A1.pdf').write_bytes(b'%PDF-1.4 synthetic test')
    (tmp_path / 'NIFTY-A1.png').write_bytes(b'synthetic image')
    result = search_sources('NIFTY提取需要多少血浆', [tmp_path])
    assert result['status'] == 'complete' and not result['matches']
    assert not marker.exists()
    assert all('pdf' not in item and 'image' not in item for item in result['formats'])


@pytest.mark.parametrize('matched', [True, False])
def test_failed_conditional_source_search_then_expert_without_canned_reply(tmp_path, monkeypatch, matched):
    if matched:
        (tmp_path / 'source-A1.md').write_text('NIFTY 提取血浆采用手工方式。\n', encoding='utf-8')
    monkeypatch.setenv('IVD_MATERIAL_LIBRARY_ROOT', str(tmp_path))
    from gateway import after_sales_guard
    monkeypatch.setattr(after_sales_guard, '_validated_conditional_preflight', lambda _turn: '')
    turn = prepare('NIFTY提取需要多少血浆')
    assert turn is not None and not turn.blocks_answer_generation
    assert turn.route_id == 'expert_mode_auto_fallback'
    assert not turn.direct_response and not turn.has_validator
    assert '专家模式' in turn.context
    assert '提取文本的行号' in turn.context
    if matched:
        assert 'source-A1.md' in turn.context and '手工方式' in turn.context
    else:
        assert str(tmp_path / 'source-A1.md') not in turn.context
    from tests.gateway.test_synthetic_adapter_final_exit import _agent
    reply = ('候选资料 source-A1.md 第 1 行提到手工方式。' if matched
             else '可以从提取流程和样本条件两个方面分析。')
    outputs, requests = [reply], []
    agent = _agent(outputs, requests)
    agent._final_response_validator = None
    result = agent.run_conversation('NIFTY提取需要多少血浆',
                                    system_message='SYNTHETIC_TEST_ONLY\n' + turn.context,
                                    task_id=turn.task_contract['task_id'])
    assert result['final_response'] == reply
    assert len(requests) == 1 and not outputs


@pytest.mark.asyncio
async def test_real_gateway_delivers_search_result_after_failed_binding(tmp_path, monkeypatch):
    from gateway import after_sales_guard
    from tests.gateway.test_auto_expert_minimal_release import G
    from tests.gateway.test_ivd_gateway_lifecycle_integration import (
        _install_runtime, _runner, _source, _write_projection,
    )

    (tmp_path / 'source-A1.md').write_text('NIFTY 提取血浆采用手工方式。\n', encoding='utf-8')
    monkeypatch.setenv('IVD_MATERIAL_LIBRARY_ROOT', str(tmp_path))
    monkeypatch.setattr(after_sales_guard, '_validated_conditional_preflight', lambda _turn: '')
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []
            self._session_messages = []
            self.context = kwargs.get('ephemeral_system_prompt') or ''

        def run_conversation(self, message, **kwargs):
            calls.append(self.context)
            answer = '候选资料 source-A1.md 第 1 行提到手工方式。'
            return {'final_response': answer, 'messages': [{'role': 'assistant', 'content': answer}],
                    'api_calls': 1, 'completed': True}

    config = {'after_sales_guard': {**G, 'serving_projection_path': str(_write_projection(tmp_path))}}
    _install_runtime(monkeypatch, config, Agent)
    runner = _runner(config)
    runner._get_proxy_url = lambda: None
    result = await runner._run_agent('NIFTY提取需要多少血浆', '', [], _source(), 'source-search-test')
    assert len(calls) == 1 and 'source-A1.md' in calls[0]
    assert 'source-A1.md' in result['final_response']
    assert result['api_calls'] == 1
