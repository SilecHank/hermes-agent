# Formal Source Gate Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep strict SOP parameter validation while preventing script/file discovery questions and internal source-read instructions from reaching the user incorrectly.

**Architecture:** The KnowledgeHub classifier owns question intent and excludes file-discovery language from the parameter fast path. Hermes keeps exact routed-source validation, but its existing bounded retry explicitly performs the missing read and its terminal fallback is written for an after-sales user.

**Tech Stack:** Python 3, `unittest`, `pytest`, Hermes gateway validation, deterministic KnowledgeHub fast-response routing.

---

### Task 1: Separate File Discovery From Parameter Routing

**Files:**
- Modify: `/home/slim/IVD-KnowledgeHub/scripts/test_hermes_fast_response_pipeline.py`
- Modify: `/home/slim/IVD-KnowledgeHub/scripts/hermes_fast_response_pipeline.py`

- [x] **Step 1: Write the failing routing tests**

Add one test asserting that `CNV上纳磁提取有48通量的脚本吗` does not return
`sop_parameter_short_answer`, and one control test asserting that
`CNV提取后DNA浓度标准是多少` still does.

- [x] **Step 2: Run the focused test and verify RED**

Run:
`python -m unittest scripts.test_hermes_fast_response_pipeline.HermesFastResponsePipelineTest.test_file_discovery_question_does_not_use_parameter_fast_path -v`

Expected: FAIL because the reproduced question currently returns the parameter
answer shape.

- [x] **Step 3: Implement the minimal classifier exclusion**

Add a focused file-discovery expression covering explicit existence/location
questions for scripts, files, SOPs, procedures, and templates. Make
`_is_parameter_question()` return false when that expression matches. Do not
alter parameter terms, product matching, source lists, or numeric validation.

- [x] **Step 4: Run focused and full fast-response tests**

Run:
`python -m unittest scripts.test_hermes_fast_response_pipeline -v`

Expected: all tests pass, including the true-parameter control.

- [x] **Step 5: Commit only the routing files**

Run:
`git add scripts/hermes_fast_response_pipeline.py scripts/test_hermes_fast_response_pipeline.py && git commit -m "Route file discovery outside parameter fast path"`

### Task 2: Make Formal-Source Retry Self-Recovering

**Files:**
- Modify: `/home/slim/.hermes/hermes-agent/tests/agent/test_final_response_validation.py`
- Modify: `/home/slim/.hermes/hermes-agent/tests/test_after_sales_guard.py`
- Modify: `/home/slim/.hermes/hermes-agent/agent/final_response_validation.py`
- Modify: `/home/slim/.hermes/hermes-agent/gateway/after_sales_guard.py`

- [x] **Step 1: Write failing retry and fallback tests**

Add a final-response validation test requiring the first
`formal_source_not_read` rejection to mention `read_file` and routed formal
paths. Extend the guard test to require the second-failure fallback to omit
`请先读取` and explain that the parameter cannot be verified from the formal
source.

- [x] **Step 2: Run the focused tests and verify RED**

Run:
`scripts/run_tests.sh tests/agent/test_final_response_validation.py tests/test_after_sales_guard.py`

Expected: the new assertions fail against the generic repair instruction and
internal fallback wording.

- [x] **Step 3: Implement the minimal recovery behavior**

Map `formal_source_not_read` to an explicit instruction to call `read_file` on
one exact routed formal source before drafting the answer. Replace only that
guard fallback with a user-facing evidence boundary asking for product version
or SOP identifier when automatic verification remains impossible.

- [x] **Step 4: Run the focused Hermes tests**

Run:
`scripts/run_tests.sh tests/agent/test_final_response_validation.py tests/test_after_sales_guard.py tests/gateway/test_ivd_runtime.py`

Expected: all selected test files pass with no flaky retry report.

- [x] **Step 5: Commit the recovery behavior**

Run:
`git add agent/final_response_validation.py gateway/after_sales_guard.py tests/agent/test_final_response_validation.py tests/test_after_sales_guard.py && git commit -m "Recover formal source validation internally"`

### Task 3: Cross-Repository Verification And Deployment

**Files:**
- Verify only: both repositories above

- [x] **Step 1: Run KnowledgeHub governance tests**

Run:
`python -m unittest scripts.test_hermes_fast_response_pipeline scripts.test_hermes_runtime_integration scripts.test_hermes_structural_guardrails -v`

Expected: all tests pass.

- [x] **Step 2: Run Hermes regression tests**

Run:
`scripts/run_tests.sh tests/agent/test_final_response_validation.py tests/test_after_sales_guard.py tests/gateway/test_ivd_runtime.py`

Expected: all files pass and report no flaky retry.

- [x] **Step 3: Run static checks**

Run `python -m py_compile` on the four changed Python modules and `git diff --check`
in both repositories.

Expected: exit code zero.

- [x] **Step 4: Push both commits and restart the live gateway**

Push the KnowledgeHub `main` commit without including pre-existing unrelated
working-tree changes. Push the Hermes feature-branch commit, restart
`hermes-gateway.service`, and verify it is active with all configured platforms
connected.

- [x] **Step 5: Reproduce the original route resolution**

Evaluate the KnowledgeHub plan for `CNV上纳磁提取有48通量的脚本吗` and verify
it is a fast-path miss eligible for one index fallback. Evaluate
`CNV提取后DNA浓度标准是多少` and verify it remains a direct parameter route.
