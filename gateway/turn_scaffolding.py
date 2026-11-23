"""Strip gateway-injected scaffolding so only what the user wrote remains.

Every inbound turn passes through the gateway's preparation pipeline, which
prefixes a trusted sender-identity note and may append media placeholders,
auto-vision descriptions and runtime notices.  None of that is user text.  The
IVD routing boundary needs to know whether a turn carries a question at all,
and retrieval needs the user's actual words: the shortlist ranks English
identifiers four times higher than Chinese phrases, so a note such as
``[Gateway verified sender identity: display name="…"]`` floods the query with
tokens like "Gateway", "verified" and "identity" and pulls back unrelated
documents.

Keep this module dependency-free so both the gateway turn pipeline and the
retrieval adapters can import it.
"""
import re

IDENTITY_NOTE_RE = re.compile(
    r"\[Gateway verified sender identity:[^\]\r\n]*\]"
)
MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\[User sent (?:an image|audio|a video|a file):[^\]\r\n]*\]$",
    re.MULTILINE,
)
# Auto vision enrichment rewrites an image turn into one of these blocks before
# routing.  None of them is text the user wrote: the first carries the model's
# own description, the other two are failure notices.  Left in place they make a
# screenshot look like a customer question, which is answered with a product
# clarification for an image the governed path cannot route anyway.
VISION_DESCRIPTION_RE = re.compile(
    r"\[The user sent an image~ Here's what I can see:.*?\]",
    re.DOTALL,
)
VISION_FAILURE_RE = re.compile(
    r"\[The user sent an image but [^\]]*\]",
    re.DOTALL,
)
VISION_HINT_RE = re.compile(
    r"\[If you need a closer look, use vision_analyze with image_url:[^\]]*\]"
)
# Shared multi-user sessions prefix every turn with "[<sender>] ".  On WeCom the
# sender has no display name, so that token is the raw user id
# ("[woMMGPDwAAkaqmcOIDLm5UcB4bAjPC9g] ...").  Left in, a media-only or empty
# turn still looks like user text: it enters the governed answer path with no
# product scope and comes back as a product clarification, and real questions
# carry the id into the question itself.  Only ids are matched -- a bracketed
# product or short label the user typed does not look like one.
SENDER_PREFIX_RE = re.compile(
    r"^\s*\[(?=[^\]\r\n]*\d)[A-Za-z0-9_@.:\-]{12,64}\]\s*"
)
# Background jobs and gateway bookkeeping post their own turns into the chat:
# ``[IMPORTANT: Background process proc_… completed normally (exit code 0).
# Command: … Output: …]``, ``[IMPORTANT: MCP servers have been reloaded …]``,
# ``[System note: …]``.  None of it is user-authored, but these turns arrive in
# the user role carrying the session's topic -- so a regression run that
# finishes right after a real answer gets routed as a business question,
# preflight blocks it for missing evidence, and the customer sees a
# clarification sentence appended to an answer that was already complete
# (2026-11-01).  The completion notice spans several lines and embeds brackets,
# so the whole-turn form is matched to the end of the turn, not to a ``]``.
BACKGROUND_NOTICE_RE = re.compile(
    r"\A\s*\[IMPORTANT: Background process .*\]\s*\Z",
    re.DOTALL,
)
BACKGROUND_NOTICE_TAIL_RE = re.compile(
    r"\[IMPORTANT: Background process .*\]\s*\Z",
    re.DOTALL,
)
RUNTIME_NOTICE_RE = re.compile(
    r"\[(?:IMPORTANT|System note): [^\]\r\n]*\]"
)


def strip_turn_scaffolding(text: str) -> str:
    """Remove gateway-injected notes so only user-authored text remains."""
    text = IDENTITY_NOTE_RE.sub(" ", str(text or ""))
    text = SENDER_PREFIX_RE.sub(" ", text)
    text = MEDIA_PLACEHOLDER_RE.sub(" ", text)
    text = VISION_DESCRIPTION_RE.sub(" ", text)
    text = VISION_FAILURE_RE.sub(" ", text)
    text = VISION_HINT_RE.sub(" ", text)
    text = BACKGROUND_NOTICE_RE.sub(" ", text)
    text = BACKGROUND_NOTICE_TAIL_RE.sub(" ", text)
    text = RUNTIME_NOTICE_RE.sub(" ", text)
    return text.strip()


def user_authored_text(message: str, business_message: str | None = None) -> str:
    """Return only the text the user actually wrote for this turn.

    A media-only turn (a screenshot with no caption) reaches the IVD boundary
    with no user body at all: the platform event carries no text, and inbound
    preparation glues the sender-identity note -- plus, for queued media, a
    ``[User sent an image: …]`` placeholder -- onto it.  Treating that
    scaffolding as a question routes the turn as a customer case with no
    product scope, which is answered with a product clarification for a
    screenshot the model never got to look at.  Callers use this helper to see
    that such a turn has no user text, so it bypasses the IVD engine and the
    agent answers the attachment itself.
    """
    if business_message is not None:
        return strip_turn_scaffolding(str(business_message))
    return strip_turn_scaffolding(str(message or ""))
