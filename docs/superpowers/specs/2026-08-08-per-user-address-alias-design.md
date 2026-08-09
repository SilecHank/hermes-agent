# Per-user Address Alias Design

**Date:** 2026-08-08
**Status:** approved
**Scope:** Hermes messaging gateway identity context

## Problem

The preferred address for the primary operator was stored only in the global
`USER.md` profile. Profile compaction preserved the name `斯霖` but dropped the
preferred address `老板`. Shared QQ group sessions also make a global address
unsafe because another member could inherit it from group context.

## Goals

- Restore `老板` for the verified primary operator identities and `我是海`
  for the separately verified QQ group member.
- Resolve the address from the current inbound sender, not conversation history.
- Keep group conversation context shared while keeping identity and address
  isolated per member.
- Preserve aliases across session rollover, profile compaction, deployment, and
  portable-state transfer.
- Leave unknown members unnamed instead of guessing.

## Design

Add a top-level `identity_aliases` configuration keyed by platform and exact
platform user ID. Each record can contain a factual `display_name` and a
`preferred_address`.

At the start of an inbound turn, the gateway resolves the current sender against
this registry. A match contributes a turn-local identity note to the model
context. It does not change the session key, authorization identity, chat ID, or
another member's context. An unmatched or malformed record contributes nothing.

The initial runtime configuration binds QQ member
`A071C72D625932F8520407E574D6AE17` to `display_name: 斯霖` and
`preferred_address: 老板`, and QQ member
`9383D64A9ABFABBEDFFD45BB1BB95FA8` to `display_name: 我是海` and
`preferred_address: 我是海`. The verified Weixin home identity remains bound to
the primary operator. WeCom remains unchanged until an owner identity is
explicitly confirmed because it has no configured home user.

When the registry is enabled but the current sender has no exact match, the
gateway adds a turn-local no-alias boundary. This explicitly prevents the model
from copying a name or form of address out of global `USER.md`, memory, or an
earlier participant's group message.

`USER.md` may continue to describe the operator, but it is no longer authoritative
for per-platform addressing.

## Failure Handling

- Invalid configuration fails closed to no alias.
- An unknown platform or user ID receives no inferred name or address.
- Alias resolution never changes access control or session routing.
- Internal identity instructions remain prompt-only and are not emitted to users.

## Tests

- Exact platform and user ID receives the configured address.
- A separately configured member in the same group receives only their address.
- An unknown member in the same group receives no inferred address.
- The same user ID on another platform does not cross-match.
- Malformed configuration is ignored safely.
- Shared group session keys remain unchanged.

## Success Criteria

- The primary operator is addressed as `老板` on verified QQ and Weixin accounts.
- Other QQ group members are never addressed as `老板` unless separately mapped.
- Session reset and profile compaction do not remove the mapping.
