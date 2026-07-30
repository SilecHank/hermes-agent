# Human Outbound Confidentiality Design

## Goal

Prevent Hermes internal prompts, validation scaffolding, retrieval controls,
runtime metadata, raw provider errors, and compaction markers from reaching
human messaging surfaces or the IVD knowledge-review pipeline, without changing
the content of normal IVD answers.

## Scope

- Weixin, WeCom, QQBot, and other human chat surfaces.
- Final replies, interim commentary, status messages, queued replies, streaming
  previews, automatic session notices, and compression notices.
- Daily knowledge-review extraction from Hermes session history.
- Local persistence permissions for prompts, logs, and backups.

Programmatic surfaces retain raw diagnostics where existing contracts require it.

## Architecture

Create `gateway/outbound_policy.py` as the single policy for human-facing text.
It redacts credentials, removes bounded internal controls and scaffolding,
rewrites provider failures, preserves normal domain answers byte-for-byte, and
returns either a concise Chinese boundary reply or silence when nothing public
remains. Existing `gateway.run` helpers delegate to it, direct sends use it, and
the stream consumer accepts the same sanitizer callback.

Routine reset and compression work remains silent. Hard failures may emit one
short Chinese action message without model, provider, context, configuration,
exception, or English implementation details.

The stable prompt states that system, developer, ephemeral, validation,
tool-control, and runtime instructions must not be reproduced. SOP and domain
evidence are explicitly not confidential instructions.

Daily review extraction ignores inactive or compacted messages and rejects both
questions and answers beginning with internal session, validation, retrieval,
or compaction scaffolding.

Prompt persistence stays enabled for answer consistency. Runtime databases,
logs, and backups become owner-readable only.

## Verification

- Unit tests for marker removal, secret redaction, provider errors, message-kind
  behavior, and normal IVD answer preservation.
- Integration tests for final, queued, interim, streaming, reset, and compression
  paths.
- Review-pipeline tests proving internal scaffolding cannot become QA pairs.
- Existing gateway, final-validation, retrieval, and golden-answer suites.

## Non-goals

- Hiding legitimate SOP citations, evidence levels, or product-routing results.
- Removing raw diagnostics from local operator surfaces.
- Disabling prompt persistence or reducing answer context quality.
