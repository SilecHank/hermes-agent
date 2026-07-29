# Purpose-Driven Retrieval State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the normal fixed four-search allowance with deterministic direct, fallback, evidence, and complex retrieval profiles while retaining a four-search defensive ceiling.

**Architecture:** Resolve a profile from existing route and user-intent signals without another model call. Store turn-local signatures, stages, evidence gain, and stop reasons in `gateway.ivd_runtime`; pass search inputs and results through `tools.file_operations`; inject a private stage instruction and log a compact snapshot from the gateway.

**Tech Stack:** Python 3.11, dataclasses, contextvars, regular expressions, Hermes parallel test runner, systemd user service.

---

### Task 1: Resolve deterministic retrieval profiles

**Files:**
- Modify: `gateway/ivd_runtime.py`
- Modify: `tests/gateway/test_ivd_runtime.py`

- [x] **Step 1: Write failing profile-resolution tests**

Add tests using `types.SimpleNamespace` for these cases:

```python
def test_verified_routed_sources_use_direct_profile():
    turn = SimpleNamespace(fast_path=True, source_paths=("/kb/formal.md",), route_id="sop_parameter_short_answer")
    assert resolve_ivd_retrieval_policy("建库投入量是多少", turn).profile == "direct"


def test_cross_product_conflict_overrides_direct_route():
    turn = SimpleNamespace(fast_path=True, source_paths=("/kb/formal.md",), route_id="sop_parameter_short_answer")
    assert resolve_ivd_retrieval_policy("NIFTY 和 CNV 同批异常且版本冲突", turn).profile == "complex_diagnosis"


def test_literature_intent_uses_evidence_profile_without_direct_source():
    assert resolve_ivd_retrieval_policy("这个机制有什么文献依据", None).profile == "evidence_supplement"


def test_unmatched_question_uses_single_index_fallback():
    assert resolve_ivd_retrieval_policy("这个问题怎么处理", None).profile == "index_fallback"
```

- [x] **Step 2: Run the runtime test and confirm failure**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py -q
```

Expected: imports or assertions fail because the resolver and policy type do not exist.

- [x] **Step 3: Implement policy types and resolver**

Add an immutable `IVDRetrievalPolicy` with `profile`, `stages`, `max_searches`, and `hard_limit=4`. Define the four profile constants and resolve them with precedence `complex -> eligible direct -> evidence -> fallback`. Use only deterministic message regexes and attributes already present on `AfterSalesTurn`.

- [x] **Step 4: Add and test the private context renderer**

Add `build_ivd_retrieval_context(policy)` that names the allowed stages, requires batched aliases, distinguishes `search` from `read_file`, and forbids disclosure of internal policy text. Test that direct context says to read routed sources and fallback context says one batched lookup.

- [x] **Step 5: Run the runtime test and confirm it passes**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py -q
```

Expected: every test in the file passes.

### Task 2: Implement turn-local stage and evidence-gain state

**Files:**
- Modify: `gateway/ivd_runtime.py`
- Modify: `tests/gateway/test_ivd_runtime.py`

- [x] **Step 1: Write failing state-machine tests**

Cover direct profile blocking its first search, fallback allowing one, evidence allowing two, complex allowing three, exact duplicate signature rejection, ordinary formal-source gain recording, two consecutive no-gain results stopping early, non-formal paths not counting as gain, maintenance bypass, context reset, and the hard ceiling of four.

- [x] **Step 2: Run the runtime test and confirm the new cases fail**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py -q
```

Expected: failures show missing signature, result-feedback, snapshot, and stop-reason behavior.

- [x] **Step 3: Extend the runtime state**

Implement:

```python
consume_ivd_search(*, pattern="", path=".", target="content")
record_ivd_search_result(*, pattern, path, target, result_paths)
get_ivd_retrieval_snapshot()
```

Normalize signatures with collapsed whitespace, case-folded patterns, normalized expanded paths, and target. Track entered stage names, unique formal paths, no-gain streak, and stop reason. Treat `_extracted`, `_wechat-mirror`, evaluation output, matrices, archive, deprecated, superseded, and candidate paths as non-formal. Preserve the existing three-value return tuple for compatibility.

- [x] **Step 4: Run the runtime test and confirm it passes**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py -q
```

Expected: every runtime test passes.

### Task 3: Feed real search signatures and results into the state machine

**Files:**
- Modify: `tools/file_operations.py`
- Modify: `tests/tools/test_file_operations.py`

- [x] **Step 1: Write failing file-operation integration tests**

Add tests proving that a direct profile blocks the first real file search, a fallback profile permits one real search and blocks the second, and a repeated real search is rejected as a duplicate while the first result paths are recorded.

- [x] **Step 2: Run the file-operation test and confirm failure**

```bash
./scripts/run_tests.sh tests/tools/test_file_operations.py -q
```

Expected: failures show that `search()` does not pass inputs or results to the runtime state.

- [x] **Step 3: Wire search request and result feedback**

Pass `pattern`, expanded `path`, and `target` to `consume_ivd_search`. Execute either file or content search into a local `SearchResult`, collect `result.files` and `match.path`, call `record_ivd_search_result`, and then return the result. Record an empty result for a missing path. Keep maintenance behavior and broad IVD exclusions unchanged.

- [x] **Step 4: Include internal stop reason without exposing it**

Keep the existing `IVD_INTERNAL_RETRIEVAL_BUDGET_EXHAUSTED` signal and add the runtime stop reason inside the bracket for diagnostics. The chat sanitizer already removes the complete bracketed shape.

- [x] **Step 5: Run file-operation and chat sanitizer tests**

```bash
./scripts/run_tests.sh tests/tools/test_file_operations.py tests/gateway/test_telegram_noise_filter.py -q
```

Expected: all tests in both files pass.

### Task 4: Wire policy, private instructions, and telemetry into the gateway

**Files:**
- Modify: `gateway/run.py`
- Modify: `gateway/after_sales_telemetry.py`
- Modify: `tests/gateway/test_ivd_runtime.py`
- Modify: `tests/gateway/test_after_sales_telemetry.py`

- [x] **Step 1: Add a failing telemetry snapshot test**

Assert that `get_ivd_retrieval_snapshot()` returns a serializable dictionary containing profile, entered stages, searches, formal-source count, no-gain streak, and stop reason, and returns an inactive snapshot outside an IVD answer turn.

- [x] **Step 2: Run the runtime test and confirm failure**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py -q
```

Expected: the snapshot shape is incomplete or unavailable.

- [x] **Step 3: Replace fixed gateway allocation**

When the after-sales guard is enabled for the current platform, resolve the policy from `message` and `_after_sales_turn`, append `build_ivd_retrieval_context(policy)` to `combined_ephemeral`, and call `begin_ivd_answer_turn(policy=policy, mode="answer")`. Remove the fixed `1 if fast_path else 4` allocation.

- [x] **Step 4: Log the snapshot before resetting the contextvar**

In the existing `finally` block, read the snapshot and emit one structured info log containing profile, stages, searches, formal-source count, and stop reason. Then call `end_ivd_answer_turn` as before. Logging failure must not affect the answer.

- [x] **Step 5: Run gateway-adjacent tests**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py tests/test_after_sales_guard.py tests/gateway/test_telegram_noise_filter.py -q
```

Expected: all selected tests pass.

### Task 5: Verify, commit, push, and deploy

**Files:**
- Modify: `gateway/ivd_runtime.py`
- Modify: `gateway/run.py`
- Modify: `tools/file_operations.py`
- Modify: `tests/gateway/test_ivd_runtime.py`
- Modify: `tests/tools/test_file_operations.py`

- [x] **Step 1: Run the full adjacent regression set**

```bash
./scripts/run_tests.sh tests/gateway/test_ivd_runtime.py tests/tools/test_file_operations.py tests/gateway/test_telegram_noise_filter.py tests/test_after_sales_guard.py -q
```

Expected: all selected tests pass with zero failures.

- [x] **Step 2: Run static checks**

```bash
python3 -m py_compile gateway/ivd_runtime.py gateway/run.py tools/file_operations.py
git diff --check
```

Expected: both commands exit successfully without diagnostics.

- [x] **Step 3: Commit and push**

```bash
git add gateway/ivd_runtime.py gateway/run.py gateway/after_sales_telemetry.py tools/file_operations.py tests/gateway/test_ivd_runtime.py tests/gateway/test_after_sales_telemetry.py tests/tools/test_file_operations.py docs/superpowers/specs/2026-07-29-purpose-driven-retrieval-state-machine-design.md
git commit -m "Add purpose-driven IVD retrieval states"
git push hermes-bot ivd-after-sales-guard-20260725
```

Expected: the implementation commit is available on the Hermes maintenance branch.

- [x] **Step 4: Restart and verify the live gateway**

```bash
/home/slim/.hermes/hermes-agent/venv/bin/hermes gateway restart
/home/slim/.hermes/hermes-agent/venv/bin/hermes gateway status
```

Expected: the gateway is active with a stable new PID, zero restart loop, and systemd linger enabled.
