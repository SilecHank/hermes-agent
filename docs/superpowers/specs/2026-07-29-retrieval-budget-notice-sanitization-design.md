# Retrieval Budget Notice Sanitization Design

## Goal

Keep the IVD per-turn retrieval budget active while preventing its internal
counter and control instructions from appearing in human-facing chat replies.
Answer quality, evidence boundaries, and auditability must not regress.

## Current Behavior

IVD answer turns allow one file search on fast paths and four file searches on
normal paths. When the limit is exceeded, the file search tool returns a
Chinese error containing the used/maximum counter. The model can repeat that
tool text in its final answer, exposing an implementation detail to the user.

## Design

Use two layers of protection:

1. The file search tool returns an internal control message that tells the
   model to stop searching, answer from evidence already collected, and never
   disclose the budget, counter, or control message.
2. The gateway final-response sanitizer removes any leaked retrieval-budget
   control text from human-facing chat platforms. If the response contains no
   useful content after removal, it returns a concise business-facing evidence
   boundary: `现有证据不足，需要进一步检索确认。`

Programmatic and local diagnostic surfaces keep raw text for debugging. The
existing retrieval limits, fast-path routing, source authority, and
`pending_verify` restrictions remain unchanged.

## Data Flow

1. An IVD file search consumes the turn-local counter.
2. A search beyond the limit returns the internal stop-search signal.
3. The model uses already retrieved evidence to finish the answer.
4. Before a chat reply is sent, the gateway strips any leaked internal signal.
5. Logs and tool traces retain the original event for maintenance analysis.

## Failure Handling

- A normal answer that happens to discuss search generally is preserved.
- Only the exact IVD budget-control shape is sanitized.
- If useful answer text surrounds the leaked control text, that answer text is
  retained.
- If only the control text remains, the user receives the evidence-boundary
  sentence instead of silence.

## Tests

- The tool refuses searches beyond the configured limit and emits the internal
  non-disclosure instruction.
- Chat gateways remove the counter and internal control language.
- Chat gateways preserve useful answer text around a leaked control line.
- A control-only response becomes the concise evidence-boundary sentence.
- Local diagnostic output remains unchanged.
- Existing IVD runtime, file search, and gateway sanitizer tests continue to
  pass.

## Non-Goals

- Raising or removing retrieval limits.
- Hiding real provider failures or approval requests.
- Treating insufficient evidence as a confirmed answer.
- Changing SOP coverage or product routing.
