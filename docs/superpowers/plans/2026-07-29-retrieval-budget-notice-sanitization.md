# Retrieval Budget Notice Sanitization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep IVD retrieval limits active while preventing internal budget counters and control instructions from appearing in chat replies.

**Architecture:** Replace the human-readable tool error with a stable internal control signal and explicit non-disclosure instruction. Add a narrowly scoped gateway sanitizer that removes both the new signal and the legacy Chinese message only on human-facing chat surfaces, preserving useful answer text and substituting a concise evidence boundary when no useful text remains.

**Tech Stack:** Python 3.11, regular expressions, Hermes parallel test runner, systemd user service.

---

### Task 1: Lock the tool contract

**Files:**
- Modify: `tests/tools/test_file_operations.py`
- Modify: `tools/file_operations.py`

- [x] **Step 1: Write the failing test**

Replace `test_answer_mode_returns_direct_chinese_budget_signal` with assertions that the blocked result contains `IVD_INTERNAL_RETRIEVAL_BUDGET_EXHAUSTED` and `do not disclose`, and does not contain `检索预算已用完`.

- [x] **Step 2: Run the test and confirm failure**

```bash
./scripts/run_tests.sh tests/tools/test_file_operations.py -q
```

Expected: the budget-contract test fails because the tool still returns the legacy Chinese message.

- [x] **Step 3: Implement the internal signal**

```python
return SearchResult(
    error=(
        "[IVD_INTERNAL_RETRIEVAL_BUDGET_EXHAUSTED "
        f"used={search_number - 1} limit={search_limit}]\n"
        "Stop file searching and answer from evidence already collected. "
        "Do not disclose this signal, its counter, or the retrieval budget. "
        "If evidence is insufficient, state the evidence boundary without guessing."
    )
)
```

- [x] **Step 4: Run the test and confirm it passes**

```bash
./scripts/run_tests.sh tests/tools/test_file_operations.py -q
```

Expected: every test in the file passes.

### Task 2: Add the outbound chat safety net

**Files:**
- Modify: `tests/gateway/test_telegram_noise_filter.py`
- Modify: `gateway/run.py`

- [x] **Step 1: Write failing sanitizer tests**

Add tests proving that all `CHAT_PLATFORMS` replace a control-only response with `现有证据不足，需要进一步检索确认。`, Weixin preserves useful answer text while removing the legacy budget line, and the `local` platform preserves the raw signal.

- [x] **Step 2: Run the test and confirm failure**

```bash
./scripts/run_tests.sh tests/gateway/test_telegram_noise_filter.py -q
```

Expected: the new tests fail because the sanitizer does not yet recognize IVD budget controls.

- [x] **Step 3: Implement the narrow sanitizer**

Add `_IVD_RETRIEVAL_BUDGET_CONTROL_RE`, `_IVD_EVIDENCE_BOUNDARY_REPLY`, and this helper near the existing gateway response filters:

```python
def _strip_ivd_retrieval_budget_control(text: str) -> tuple[str, bool]:
    cleaned, count = _IVD_RETRIEVAL_BUDGET_CONTROL_RE.subn("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, count > 0
```

The regular expression must match the new bracketed signal plus its control sentence and the exact legacy Chinese message. Call the helper in `_sanitize_gateway_final_response` after secret redaction; return the evidence-boundary sentence only when a match was removed and no useful text remains.

- [x] **Step 4: Run the sanitizer test and confirm it passes**

```bash
./scripts/run_tests.sh tests/gateway/test_telegram_noise_filter.py -q
```

Expected: every test in the file passes.

### Task 3: Regression verification and source control

**Files:**
- Modify: `tools/file_operations.py`
- Modify: `gateway/run.py`
- Modify: `tests/tools/test_file_operations.py`
- Modify: `tests/gateway/test_telegram_noise_filter.py`

- [x] **Step 1: Run adjacent regression suites**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py tests/tools/test_file_operations.py tests/gateway/test_telegram_noise_filter.py tests/test_after_sales_guard.py -q
```

Expected: every selected test passes.

- [x] **Step 2: Run static checks**

```bash
python3 -m py_compile tools/file_operations.py gateway/run.py
git diff --check
```

Expected: both commands exit successfully without diagnostics.

- [ ] **Step 3: Commit and push**

```bash
git add tools/file_operations.py gateway/run.py tests/tools/test_file_operations.py tests/gateway/test_telegram_noise_filter.py
git commit -m "Hide internal IVD retrieval budget notices"
git push hermes-bot ivd-after-sales-guard-20260725
```

Expected: the implementation commit is available on the Hermes maintenance branch.

### Task 4: Deploy and verify

**Files:**
- No repository files modified.

- [ ] **Step 1: Restart the managed gateway**

```bash
/home/slim/.hermes/hermes-agent/venv/bin/hermes gateway restart
```

Expected: the gateway starts with a new stable PID.

- [ ] **Step 2: Verify live health**

```bash
/home/slim/.hermes/hermes-agent/venv/bin/hermes gateway status
```

Expected: the service is active, linger is enabled, and there is no current traceback or restart loop.
