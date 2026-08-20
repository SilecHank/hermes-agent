# IVD Hybrid Expert Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the IVD expert-answer behavior while retaining deterministic structured-card answers for exact, uniquely verified scalar facts.

**Architecture:** The gateway will use a hybrid boundary. A deterministic package probe may serve only an exact scalar fact when product scope, variant, field, source revision, and answer shape are all unambiguous. All mechanism, troubleshooting, report interpretation, mixed-intent, weak-hit, ambiguous, and non-scalar questions will enter the existing expert guard, which combines unified quick cards, formal sources, validation, and model reasoning. The serving build will compile the legacy quick-answer cards into the package and reject a package that has no compiled fast-card manifest.

**Tech Stack:** Python 3.11, pytest, immutable IVD serving package, YAML runtime configuration, SQLite/JSON facts indexes.

---

### Task 1: Lock the hybrid routing contract with failing tests

**Files:**
- Create: `tests/gateway/test_ivd_hybrid_routing.py`
- Modify: `tests/gateway/test_ivd_exclusive_runtime.py`

- [ ] **Step 1: Write failing tests**

Add tests asserting that `hybrid` is accepted, mechanism questions do not use an exclusive scalar answer, ambiguous product questions use expert clarification, and a uniquely matched scalar question may use the package probe.

- [ ] **Step 2: Run the focused tests and verify they fail for the missing hybrid behavior**

Run: `python -m pytest tests/gateway/test_ivd_hybrid_routing.py -q`

Expected: failure because `hybrid` is not an accepted engine mode and the gateway has no hybrid route.

- [ ] **Step 3: Commit the red tests**

Run: `git add tests/gateway/test_ivd_hybrid_routing.py tests/gateway/test_ivd_exclusive_runtime.py && git commit -m "test: define hybrid expert routing contract"`

### Task 2: Implement the hybrid boundary and conservative scalar gate

**Files:**
- Create: `gateway/ivd_hybrid_router.py`
- Modify: `gateway/ivd_runtime.py:ivd_engine_mode`
- Modify: `gateway/run.py:_prepare_gateway_ivd_boundary`
- Test: `tests/gateway/test_ivd_hybrid_routing.py`

- [ ] **Step 1: Implement a pure route decision helper**

The helper must classify mechanism, troubleshooting, report interpretation, mixed-intent, clarification, and non-scalar results as `expert`; only a resolved, non-ambiguous, `scalar_lookup`/`direct_fact` result with exactly one structured source is eligible for `package_scalar`.

- [ ] **Step 2: Connect `engine_mode: hybrid`**

Run the package probe only as a bounded classifier/evidence lookup. On any exception, ambiguity, missing source, multiple claims, or non-scalar shape, call `prepare_after_sales_turn` and preserve the legacy expert context and validators.

- [ ] **Step 3: Run focused tests and the existing exclusive runtime tests**

Run: `python -m pytest tests/gateway/test_ivd_hybrid_routing.py tests/gateway/test_ivd_exclusive_runtime.py -q`

Expected: all focused tests pass and package mode remains exclusive.

### Task 3: Compile legacy quick cards into the serving package

**Files:**
- Modify: `scripts/build_ivd_serving_package.py`
- Modify: `knowledge-base/ivd-serving-package-allowlist.json`
- Modify: `tests/test_ivd_serving_package_build.py`
- Create: serving-package `fast_cards_manifest.json` through the existing build output

- [ ] **Step 1: Add a failing build assertion**

The build test must require a fast-card manifest containing the source revision, route count, and only formal/non-candidate card inputs. It must fail when `_fast-after-sales-answers.md` is absent from the package.

- [ ] **Step 2: Implement compilation**

Copy the approved legacy quick-card source into a package-local compiled manifest. Keep it as routing/context material only; formal SOP facts and source validation remain authoritative. Reject candidate/pending_verify/_extracted inputs.

- [ ] **Step 3: Run package build tests**

Run: `python -m pytest tests/test_ivd_serving_package_build.py -q`

Expected: all package build tests pass, including the fast-card manifest check.

### Task 4: Add expert fallback and answer-shape regression coverage

**Files:**
- Modify: `tests/gateway/test_ivd_gateway_lifecycle_integration.py`
- Modify: `tests/gateway/test_ivd_run_contract.py`
- Modify: `tests/test_hermes_real_task_regression.py` if the fixture exists

- [ ] **Step 1: Add regression cases**

Cover: “为什么磁珠结冰不能用”, “同批多个产品异常怎么排查”, “携带者筛查 DNA 起始投入量是多少”, and “无创提取需要多少血浆”. The first two must retain expert context; the latter two may produce one canonical scalar response only when the formal source is uniquely resolved.

- [ ] **Step 2: Run tests before implementation changes where applicable**

Run the new focused tests and record the expected red failures for missing fallback or over-expansion.

- [ ] **Step 3: Implement only the minimum response-shape changes**

Do not add new prompt text. Enforce the answer contract at the route/validator layer: scalar questions return one value and unit; expert questions retain the existing D0-D6 reasoning and boundary controls.

- [ ] **Step 4: Run focused regression tests**

Run: `python -m pytest tests/gateway/test_ivd_gateway_lifecycle_integration.py tests/gateway/test_ivd_run_contract.py tests/test_hermes_real_task_regression.py -q`

### Task 5: Build, gate, and publish without changing the production owner

**Files:**
- Modify only generated release artifacts and deployment metadata as produced by the existing release pipeline.

- [ ] **Step 1: Run structural, experience, answer-quality, index-freshness, and agent tests**

All must pass; no pending_verify or `_extracted` source may enter the formal package.

- [ ] **Step 2: Build a new immutable release and verify package/manifest digests**

The release must include the fast-card manifest and hybrid route code.

- [ ] **Step 3: Deploy to WSL primary only**

Keep `wsl-primary` active; do not start Mac gateway or alter active-host ownership.

- [ ] **Step 4: Run live boundary and three-platform health checks**

Verify one scalar parameter case, one mechanism case, one troubleshooting case, one mixed-product case, and Weixin/WeCom/QQBot health.

- [ ] **Step 5: Commit and push each repository after its stage passes**

Push agent and KnowledgeHub changes separately, preserving unrelated user changes.
