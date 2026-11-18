"""Bounded, local candidate-source search for expert fallback."""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

_STOP = {'需要', '多少', '什么', '怎么', '如何', '是否', '可以', '哪些', '这个',
         '那个', '请问', '一下', '为什么', '应该', '的是', '问题', '结果', '方法'}
_MAX_OUTPUT = 256_000
_MAX_MATCHES = 12
_FORMATS = ['md', 'txt', 'csv', 'tsv', 'docx', 'xlsx']


def _document_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract bounded Office text in a killable subprocess."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.read_extract import ExtractionError, extract_document_text

    terms = payload['terms']
    product = payload['product']
    deadline = time.monotonic() + payload['timeout']
    matches: list[dict[str, Any]] = []
    partial = False
    visited = 0

    def walk_error(_error: OSError) -> None:
        nonlocal partial
        partial = True

    for raw_root in payload['roots']:
        for directory, dirs, files in os.walk(raw_root, followlinks=False, onerror=walk_error):
            dirs[:] = [name for name in dirs if name not in {'.git', '.venv', 'node_modules'}]
            for name in files:
                if time.monotonic() >= deadline or visited >= 1000:
                    return {'status': 'partial', 'matches': matches}
                path = Path(directory) / name
                suffix = path.suffix.lower()
                if suffix not in {'.docx', '.xlsx'} or path.is_symlink():
                    continue
                visited += 1
                # Office extraction omits some parts, such as headers or hidden sheets.
                partial = True
                try:
                    if path.stat().st_size > 20_000_000:
                        partial = True
                        continue
                    with zipfile.ZipFile(path) as archive:
                        if sum(item.file_size for item in archive.infolist()) > 10_000_000:
                            partial = True
                            continue
                    text = extract_document_text(str(path))
                except (OSError, ValueError, zipfile.BadZipFile, ExtractionError, subprocess.TimeoutExpired):
                    partial = True
                    continue
                combined = (str(path) + ' ' + text).upper()
                hits = {term for term in terms if term.upper() in combined}
                if len(hits) < 2 or (product and product not in combined):
                    continue
                scored = []
                for number, line in enumerate(text.splitlines(), 1):
                    count = sum(term.upper() in line.upper() for term in terms)
                    if count:
                        scored.append((count, number, line))
                scored.sort(key=lambda row: (-row[0], row[1]))
                for _, number, line in scored[:2]:
                    matches.append({'path': str(path), 'line': number, 'text': line[:500],
                                    'location_type': 'extracted_line'})
                if len(matches) >= _MAX_MATCHES:
                    return {'status': 'partial', 'matches': matches}
    return {'status': 'partial' if partial else 'complete', 'matches': matches}


def _terms(question: str) -> tuple[list[str], str]:
    text = question[:300]
    words = re.findall(r'[A-Za-z][A-Za-z0-9-]{1,30}', text)
    product = words[0].upper() if words else ''
    terms = list(dict.fromkeys(word.upper() for word in words))
    for chunk in re.findall(r'[\u4e00-\u9fff]+', text):
        for index in range(len(chunk) - 1):
            item = chunk[index:index + 2]
            if item not in _STOP and item not in terms:
                terms.append(item)
    return terms[:24], product


def _run_document_worker(roots: list[Path], terms: list[str], product: str, timeout: float) -> dict[str, Any]:
    payload = {'roots': [str(root) for root in roots], 'terms': terms,
               'product': product, 'timeout': max(0.1, timeout - 0.05)}
    proc = subprocess.Popen(
        [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--document-worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(json.dumps(payload), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group = getattr(os, 'killpg', None)
        kill_signal = getattr(signal, 'SIGKILL', None)
        try:
            if not callable(kill_group) or kill_signal is None:
                raise OSError('process groups unavailable')
            kill_group(proc.pid, kill_signal)
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError('document search failed')
    result = json.loads(stdout)
    if not isinstance(result, dict):
        raise ValueError('invalid document search result')
    return result


def search_sources(question: str, roots: list[Path], *, timeout: float = 5.0) -> dict[str, Any]:
    """Search readable text files and return literal candidate lines, not authority.

    A timeout, missing root, output limit, or unavailable ripgrep is reported;
    a partial search must never be described as covering the whole library.
    """
    executable = shutil.which('rg')
    terms, product = _terms(question)
    if not terms or os.name == 'nt':
        return {'status': 'unavailable', 'matches': [], 'roots_searched': 0,
                'formats': _FORMATS}
    if not executable:
        available = []
        for candidate in roots:
            try:
                path = Path(candidate).expanduser().resolve(strict=True)
                if path.is_dir() and path not in available:
                    available.append(path)
            except OSError:
                continue
        matches = []
        if available and timeout > 0:
            try:
                matches = _run_document_worker(available, terms, product, timeout).get('matches', [])
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
        return {'status': 'partial' if matches else 'unavailable', 'matches': matches[:_MAX_MATCHES],
                'roots_searched': len(available) if matches else 0, 'formats': _FORMATS}
    deadline = time.monotonic() + max(0.0, timeout)
    collected: dict[str, list[dict[str, Any]]] = {}
    priorities: dict[str, int] = {}
    searched = 0
    incomplete = False
    seen: set[Path] = set()
    searchable_roots: list[Path] = []
    for candidate in roots:
        try:
            root = Path(candidate).expanduser().resolve(strict=True)
            if not root.is_dir():
                incomplete = True
                continue
        except OSError:
            incomplete = True
            continue
        if root in seen:
            continue
        seen.add(root)
        searchable_roots.append(root)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            incomplete = True
            break
        argv = [executable, '--json', '--hidden', '--no-ignore', '--max-count', '3', '--max-columns', '500',
                '--max-filesize', '2M', '--glob', '*.md', '--glob', '*.txt',
                '--glob', '*.csv', '--glob', '*.tsv', '--glob', '!**/.git/**',
                '--glob', '!**/node_modules/**', '--glob', '!**/.venv/**', '-i', '-F']
        for term in terms:
            argv.extend(('-e', term))
        argv.extend(('--', str(root)))
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            incomplete = True
            continue
        searched += 1
        output = bytearray()
        try:
            assert proc.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        incomplete = True
                        break
                    events = selector.select(remaining)
                    if not events:
                        incomplete = True
                        break
                    data = os.read(proc.stdout.fileno(), 8192)
                    if not data:
                        break
                    output.extend(data)
                    if len(output) > _MAX_OUTPUT:
                        incomplete = True
                        break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        if proc.returncode not in (0, 1) and not incomplete:
            incomplete = True
        for line in bytes(output[:_MAX_OUTPUT]).splitlines():
            try:
                event = json.loads(line)
                if event.get('type') != 'match':
                    continue
                data = event['data']
                path = data['path']['text']
                text = data['lines']['text'].strip()[:500]
                number = int(data['line_number'])
                if text:
                    collected.setdefault(path, []).append({'path': path, 'line': number, 'text': text,
                                                          'location_type': 'file_line'})
                    if len(collected[path]) >= 3:
                        incomplete = True
                    priorities.setdefault(path, searched)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                incomplete = True
        if time.monotonic() >= deadline:
            incomplete = True
            break
    remaining = deadline - time.monotonic()
    if searchable_roots and remaining > 0:
        try:
            extracted = _run_document_worker(searchable_roots, terms, product, remaining)
            incomplete = incomplete or extracted.get('status') != 'complete'
            for item in extracted.get('matches', []):
                path = item['path']
                collected.setdefault(path, []).append(item)
                priorities.setdefault(path, next(
                    (index + 1 for index, root in enumerate(searchable_roots)
                     if Path(path).is_relative_to(root)), len(searchable_roots)))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired):
            incomplete = True
    elif searchable_roots:
        incomplete = True
    ranked = []
    for path, rows in collected.items():
        combined = (path + ' ' + ' '.join(row['text'] for row in rows)).upper()
        hits = {term for term in terms if term.upper() in combined}
        if len(hits) < 2 or (product and product not in combined):
            continue
        ranked.append((priorities[path], len(hits), path, rows))
    ranked.sort(key=lambda item: (item[0], -item[1], item[2]))
    candidates = [row for _, _, _, rows in ranked for row in rows[:2]]
    matches = candidates[:_MAX_MATCHES]
    return {'status': 'partial' if incomplete and searched else 'complete' if searched else 'unavailable',
            'matches': matches, 'matches_truncated': len(candidates) > _MAX_MATCHES,
            'roots_searched': searched, 'formats': _FORMATS}


if __name__ == '__main__' and sys.argv[1:] == ['--document-worker']:
    print(json.dumps(_document_worker(json.load(sys.stdin)), ensure_ascii=False))
