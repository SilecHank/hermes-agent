# Purpose-Driven Retrieval State Machine Design

## Goal

Replace the normal fixed four-search allowance with a deterministic retrieval
state machine. Routine IVD answers should use direct source reads or one batched
index fallback, while evidence-heavy and genuinely complex questions receive a
small number of targeted additional stages. Accuracy and source authority must
not regress.

## Why A State Machine

The knowledge base is static and structured. Repeated searches usually indicate
missing routing, unbatched aliases, or an unclear retrieval purpose rather than
a need to scan the same corpus again. A count-only budget cannot distinguish a
useful evidence supplement from repeated low-value searches.

## Inputs

The policy is resolved without another model call from existing deterministic
signals:

- `AfterSalesTurn.fast_path`
- `AfterSalesTurn.route_id`
- `AfterSalesTurn.source_paths`
- `AfterSalesTurn.requires_source_validation`
- user-message intent markers for mechanism, literature, version conflict,
  comparison, and multi-product diagnosis
- the path, target, pattern, and result of each file search

## Profiles

| Profile | Normal search stages | Search allowance |
| --- | --- | ---: |
| `direct` | Read routed files directly | 0 |
| `index_fallback` | One batched index/content lookup | 1 |
| `evidence_supplement` | Index fallback, then targeted evidence lookup | 2 |
| `complex_diagnosis` | Product lookup, conflict branch, evidence supplement | 3 |

An independent hard ceiling of four searches remains as a defensive invariant,
but no normal profile plans to consume it.

## Profile Resolution

1. Use `direct` when a verified fast route supplies one or more formal source
   paths. The model reads those paths; file search is not needed.
2. Use `evidence_supplement` for mechanism, principle, guideline, literature,
   source-version, or explicit evidence questions.
3. Use `complex_diagnosis` only for explicit cross-product, multi-abnormality,
   or conflicting-version diagnosis.
4. Use `index_fallback` for all remaining unmatched IVD questions.
5. A route that requires formal-source validation but has no valid source path
   cannot use `direct`; it falls back to `index_fallback` and remains fail-closed.

## Runtime Transitions

1. Start the turn in the profile's first allowed stage.
2. Reject an exact duplicate search signature (`target`, normalized `path`,
   normalized `pattern`) without consuming another useful stage.
3. Record whether a completed search produced new formal source paths.
4. A normal index fallback that finds formal sources transitions to `stop`;
   subsequent work should use direct file reads.
5. Evidence and complex profiles may enter only their declared next stage.
6. Two consecutive searches with no new formal source paths transition to
   `stop`, even when a numeric allowance remains.
7. Searches in `_extracted`, candidate matrices, archives, deprecated content,
   or `pending_verify` layers never count as evidence gain and never unlock an
   additional stage.

## Query Batching

The turn context instructs the model to combine known aliases into one regular
expression when an index fallback is necessary. A second search that only
changes a synonym is treated as duplicate intent unless the first search had no
results and the profile explicitly permits another stage.

## Search And Read Boundary

- `search` locates a source and is controlled by this state machine.
- `read_file` verifies an already located source and does not consume a search
  stage.
- Direct reads remain constrained by the existing source validation and numeric
  claim gates.

## User Experience

The state machine is silent. Internal profile, stage, counter, duplicate, and
stop reasons remain in logs and telemetry. If evidence remains insufficient,
the final answer states the evidence boundary without exposing retrieval
implementation details.

## Telemetry

Each IVD turn records:

- resolved retrieval profile
- stages entered
- search signatures
- result count and novel formal source count
- stop reason (`direct`, `formal_source_found`, `duplicate`, `no_gain`, or
  `hard_limit`)

Telemetry is diagnostic only and cannot write or promote formal knowledge.

## Safety Invariants

- Source authority and product isolation remain unchanged.
- `pending_verify` and case candidates never become formal evidence.
- Missing product lines are not inferred or filled.
- The state machine adds no model or API call.
- A policy-resolution failure falls back to the existing four-search hard cap
  and existing fail-closed answer validation.
- Maintenance-mode searches remain outside the answer-plane state machine.

## Tests

- Direct routed questions reject file search while allowing direct reads.
- Unmatched ordinary questions allow one search and block the second.
- Mechanism/evidence questions allow at most two declared stages.
- Complex mixed questions allow at most three declared stages.
- Duplicate signatures are rejected.
- Formal result gain stops ordinary fallback searching.
- Two no-gain searches stop evidence/complex profiles early.
- Candidate and extracted results do not unlock another stage.
- The four-search hard ceiling cannot be exceeded.
- Existing answer validation, product isolation, maintenance mode, and chat
  sanitization tests continue to pass.

## Non-Goals

- Adding vector retrieval or a new index engine.
- Increasing the normal search allowance above three.
- Automatically converting search misses into fast-path rules.
- Changing SOP coverage.
