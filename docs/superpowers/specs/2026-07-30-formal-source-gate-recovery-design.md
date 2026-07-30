# Formal Source Gate Recovery Design

## Goal

Prevent internal formal-source instructions from reaching after-sales users
while preserving fail-closed validation for SOP parameters and product
isolation.

## Confirmed Failure

The question `CNV上纳磁提取有48通量的脚本吗` was classified as an SOP
parameter question because it contained `提取`. That created a direct route
with zero file-search allowance. Hermes repeatedly attempted blocked searches,
read several unrelated files, and then failed the exact routed-source check.
The single validation retry did not tell the model to call `read_file`, so the
internal fallback was sent to the user.

## Design

1. File or script discovery questions are not parameter questions. They miss
   the parameter fast path and use the existing one-search `index_fallback`
   profile. True questions about numeric SOP parameters remain on the direct,
   fail-closed route.
2. A `formal_source_not_read` retry explicitly instructs the model to call
   `read_file` on one of the exact routed formal paths before answering. The
   source allowlist remains unchanged; arbitrary files do not satisfy the gate.
3. If the automatic retry still cannot verify a source, the deterministic
   fallback explains that the parameter cannot currently be verified and asks
   only for useful business context such as product version or SOP identifier.
   It never exposes an instruction to operate Hermes internals.

## Safety Invariants

- No unverified number can pass the parameter gate.
- Only exact routed formal sources satisfy direct-route validation.
- `pending_verify`, extracted, archived, and candidate material remain
  ineligible as formal evidence.
- Product scope is never inferred or filled.
- No extra model call is added; the existing bounded validation retry is reused.

## Tests

- The reproduced CNV script-existence question does not enter the parameter
  fast path and retains one index-fallback search.
- A true CNV extraction parameter question still enters the direct source
  route and remains fail-closed.
- `formal_source_not_read` produces a retry instruction containing the concrete
  `read_file` action.
- A second failure returns a user-facing evidence boundary without the internal
  instruction `请先读取`.
- Existing fast-response, retrieval-budget, and after-sales guard tests remain
  green.
