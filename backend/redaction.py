"""Credential-shaped material, removed from one string at a RESPONSE boundary.

Why this exists on the server
-----------------------------

`frontend/lib/sanitize.ts` already runs `redactSecretText` over every durable
string the workspace renders, and that stays. It is not sufficient on its own,
and independent review of CODE-3 said so plainly: by the time it runs, the HTTP
response has already been delivered to the browser. It has sat in the network
panel, in whatever proxies and extensions observe traffic, and in any log that
captured it. "Hidden from the DOM" is not "never sent", and only the server can
make the second statement true.

So this is the server-side counterpart, applied on the way OUT of the API.
The browser one remains as a second, independent defense.

What it is NOT
--------------

It is not a replacement for the repository's existing evidence-fragment
boundary. `safe_fragment_text` / `_FRAGMENT_SECRET_MARKERS`
(`backend/engines/swarm_v2/evidence.py`) guard PERSISTENCE: they REJECT, they
are mirrored by a CHECK constraint in
`supabase/migrations/20260828000200_source_evidence_fragments.sql`, and they
keep credential-shaped text out of durable evidence in the first place. That
marker vocabulary is deliberately left exactly where it is -- it is named in a
migration's comment, and moving it would mean editing `supabase/`.

The two are pinned together instead:
`tests/test_catalog_review_surface.py::test_the_backend_redactor_neutralizes_the_reviewed_marker_vocabulary`
asserts that every marker that vocabulary names is something this redactor also
neutralizes, so a value the durable boundary would refuse can never be the
value a response prints.

Reject or redact?
-----------------

REDACT, deliberately. The persistence boundary rejects because a caller writing
a credential into durable evidence is a bug that should stop. A READ surface is
the opposite case: refusing a whole page because one stored trim happens to
look like a token would be a denial of inspection on the surface an operator
reaches for during an incident. Over-redaction is the intended failure
direction -- a legitimate value that looks like a key is shown as `[REDACTED]`,
which is lossy and safe, rather than printed, which is not.

Mirror discipline
-----------------

The patterns, their ORDER and the labelled-secret rule are a faithful mirror of
`redactSecretText`. Two boundaries running different vocabularies would be
worse than one, because each would be trusted to cover what the other missed.
`test_the_backend_and_frontend_redactors_agree_on_every_sentinel` holds the two
to the same sentinel set, and the sentinels themselves are assembled at runtime
in both places so no key-shaped literal is ever committed.

Pure module: regular expressions and string substitution. No I/O, no clock, no
configuration, no global mutable state.
"""

from __future__ import annotations

import re
from typing import Any

#: What replaces a credential. One token, so a reader can tell redaction from
#: truncation and from a value that was simply absent.
REDACTED = "[REDACTED]"

#: Credential SHAPES, in the order they must run.
#:
#: `Bearer <token>` is collapsed whole BEFORE the bare token shapes, so a
#: redacted token can never leave a stray `Bearer` behind. Same ordering, same
#: reason, as the frontend.
SECRET_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM blocks: the header alone is enough to know what follows. Unterminated
    # blocks are matched to end-of-string rather than left half-printed.
    re.compile(r"-----BEGIN[^-]*-----[\s\S]*?(?:-----END[^-]*-----|$)", re.IGNORECASE),
    # `Bearer <token>` as a whole, before the token shapes below.
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}", re.IGNORECASE),
    # LEGACY Supabase service-role shape, and JWTs generally: two or three
    # base64url segments. This does NOT cover the modern key format below.
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?"),
    # MODERN Supabase server-side secret key. Anchored on `sb_secret_` and NOT
    # on `sb_`: a publishable key (`sb_publishable_...`) is public
    # configuration the browser is MEANT to hold, and redacting it would hide a
    # legitimate value while protecting nothing.
    re.compile(r"\bsb_secret_[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    # Provider API key shape.
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    # AWS access key id, which the evidence marker vocabulary implies through
    # `aws_secret_access_key` and which has an unmistakable shape of its own.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}"),
)

#: LABELLED secrets: the label is kept, only the value is replaced.
#:
#: The label alternation is the same one the frontend uses, widened to cover
#: every marker `_FRAGMENT_SECRET_MARKERS` names in labelled form
#: (`client_secret`, `x-api-key`, `aws_secret_access_key`, `secret_key`,
#: `private_key`, `refresh_token`, `lease_token`, `password`, `authorization`).
#: The lookahead skips a value a shape pattern above already collapsed, so
#: `authorization: Bearer <token>` ends as `authorization: [REDACTED]` rather
#: than re-wrapping the marker.
LABELLED_SECRET = re.compile(
    r"((?:service[_-]?role|api[_-]?key|apikey|x-api-key|authorization|secret"
    r"|secret[_-]?key|client[_-]?secret|aws[_-]?secret[_-]?access[_-]?key"
    r"|password|credential|access[_-]?token|refresh[_-]?token|private[_-]?key"
    r"|lease[_-]?token)[\"']?\s*[:=]\s*[\"']?)"
    r"(?!\[REDACTED\])([^\"'\s,;}\]]+)",
    re.IGNORECASE,
)


def redact_secret_text(value: Any) -> str:
    """Return `value` as text with credential-shaped material replaced.

    Total by construction: a boundary that could raise would turn a hostile
    stored value into a failed response, which is a worse outcome than a
    redacted one. Anything in, a string out.
    """
    text = "" if value is None else str(value)
    for pattern in SECRET_SHAPE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return LABELLED_SECRET.sub(rf"\1{REDACTED}", text)


__all__ = ["LABELLED_SECRET", "REDACTED", "SECRET_SHAPE_PATTERNS", "redact_secret_text"]
