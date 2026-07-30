# Human Outbound Confidentiality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every human-visible Hermes message through one confidentiality policy and prevent internal scaffolding from entering IVD review candidates.

**Architecture:** Add a focused outbound-policy module and preserve existing `gateway.run` helper contracts by delegation. Inject the policy into streaming and replace unsafe direct-send call sites. Harden the separate KB review extractor and local runtime permissions without disabling prompt persistence.

**Tech Stack:** Python 3.11, asyncio, pytest, SQLite, YAML, systemd/WSL.

---

### Task 1: Outbound policy core

**Files:** `gateway/outbound_policy.py`, `gateway/run.py`, `tests/gateway/test_outbound_policy.py`

- [ ] Write failing tests for internal markers, raw provider errors, message kinds, and normal IVD answers.
- [ ] Run the tests and confirm expected failures.
- [ ] Implement the shared policy and compatibility delegation.
- [ ] Re-run focused and existing noise-filter tests.
- [ ] Commit.

### Task 2: Direct and streaming boundaries

**Files:** `gateway/run.py`, `gateway/stream_consumer.py`, gateway boundary tests.

- [ ] Write failing tests for interim, queued, streaming, reset, and compression paths.
- [ ] Confirm bypass failures.
- [ ] Apply the policy before affected sends and edits.
- [ ] Replace technical notices with silent or concise Chinese behavior.
- [ ] Re-run focused tests and commit.

### Task 3: Prompt confidentiality instruction

**Files:** `agent/prompt_builder.py`, `tests/agent/test_prompt_builder.py`

- [ ] Write a failing stable-prompt test.
- [ ] Add one compact confidentiality instruction without duplicating IVD rules.
- [ ] Re-run prompt builder and cache tests.
- [ ] Commit.

### Task 4: Knowledge-review hygiene

**Files:** KnowledgeHub `scripts/hermes_platform_daily_review_core.py` and `scripts/test_qqbot_daily_review.py`

- [ ] Write failing fixtures for compaction, validation, retrieval-control, inactive, and compacted messages.
- [ ] Confirm current incorrect pairing.
- [ ] Filter both sides and select active, non-compacted messages.
- [ ] Run review and historical-gap tests.
- [ ] Commit only these files on current remote main.

### Task 5: Platform defaults and permissions

**Files:** `/home/slim/.hermes/config.yaml` and runtime permission hook if available.

- [ ] Explicitly disable streaming, interim commentary, tool progress, and detailed busy acknowledgments for all three platforms while retaining generic Chinese long-task heartbeat.
- [ ] Restrict state, logs, session indexes, and backups to owner-only access.
- [ ] Verify restart preserves connectivity and permissions.

### Task 6: Verification and deployment

- [ ] Run confidentiality, streaming, validation, retrieval, and review tests.
- [ ] Run Hermes gateway regression and KnowledgeHub golden-answer suites.
- [ ] Run `py_compile` and `git diff --check` in both repositories.
- [ ] Restart the gateway and confirm all three platforms connected.
- [ ] Push both repositories and report commit IDs.
