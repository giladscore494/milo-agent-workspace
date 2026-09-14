"""R5: the read-only saved-document proof tool for the official Toyota page.

The Web source family of the R5 proof is ONE saved page: the official Toyota
Israel model page for the RAV4 Plug-in, captured as a public unauthenticated
HTTPS GET. There is no browser here, no live fetch, no URL parameter and no
HTML parser dependency -- only a committed file and a checksum gate.

What is committed is the page's deterministic VISIBLE-TEXT PROJECTION, not the
raw HTML, and the manifest records it as a `deterministic_projection` rather
than as a response. Two reasons, in this order. The projection is the evidence
surface: every document-span locator points into it, and the markup around it
supports no claim. And the raw page carries the SITE's own client-side tokens
-- Mapbox publishable keys and an analytics key that every visitor to the
public site receives -- which GitHub push protection classifies as secrets and
refuses; bypassing that to commit a third party's token is not a call a proof
gets to make, and redacting bytes would break the digest chain anyway.

The raw response is therefore not committed, exactly as the 7.3 MB upstream
Yeda catalog is not. Its SHA-256 is recorded as `upstream_sha256` and remains
the SOURCE VERSION, so the evidence is still pinned to the whole captured body
rather than to the excerpt that was kept.

What this source may and may not establish
------------------------------------------

The page is an ARCHIVED-MODEL page, not a specification sheet. It is read for
exactly two kinds of statement, both of which its visible text actually makes:

*   who the model is -- Toyota, RAV4, Plug-in/PHEV; and
*   that Toyota Israel has ENDED marketing it.

It is never read for a technical value. No displacement, power, battery,
consumption or price is extracted, inferred from the model name, or inferred
from the URL or the file name, because the page states none of them and an
official page saying what a car IS does not thereby say what it MEASURES. The
structured technical source of this proof is the government registry, and the
authority table keeps it that way: this source's type is deliberately one
`conflict_policy.SOURCE_TYPE_AUTHORITY` does not name, so it is authoritative
for nothing and can never close a conflict about a specification.

Why the evidence is a projection, not the raw HTML
--------------------------------------------------

A page's scripts, styles, templates and inline SVG are not things the page
says. Reading a fact out of a `<script>` would let whatever a bundler happened
to inline become evidence, so those elements are removed WITH their contents
before any text is considered, and the remaining markup is reduced to the
visible text the page renders. That projection is deterministic and versioned
(`WEB_TEXT_PROJECTION_VERSION`): the same response bytes always produce the
same text and therefore the same character offsets, so a reviewer holding the
capture archive re-derives exactly the committed file and can then read the
same span.

`visible_text_projection` is idempotent, which is what makes the committed file
demonstrably a projection OUTPUT rather than an edited copy of the page:
projecting it again is a no-op, and both the importer and a test check that.

Selection inside the projection is by EXACT expected phrase from a closed,
server-owned table, and a phrase must occur exactly once. A phrase that is
absent, or that occurs more than once, fails the call closed: an archived-model
page that no longer carries its archived-status sentence must stop the proof,
not quietly produce a shorter answer or pick the first of several matches.

The whole projection is deliberately NOT returned across the tool boundary. A
tool result is bounded by `MAX_TOOL_OUTPUT_JSON_BYTES`, and ten thousand
characters of Hebrew exceed it once JSON-escaped -- a real production bound
that a proof does not get to widen. So the span is proven HERE, where the
document actually is: this tool asserts that the committed projection really
does read exactly the expected phrase at the offsets it reports, and returns
only those offsets, that text, and the SHA-256 of the whole projection.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any, Mapping

from backend.engines.swarm_v2.evidence_bounds import MAX_DOCUMENT_OFFSET
from backend.engines.swarm_v2.fragments import MAX_FRAGMENT_CHARS
from backend.tools.contracts import ToolContext, ToolError, ToolMode, ToolOperation

from .manifest import ProofManifestError, load_text_fixture

#: The version of the projection below. It is recorded in the manifest and
#: travels in the tool result, because the offsets of every document-span
#: locator are only meaningful relative to an exact projection rule. Changing
#: the rule is changing where the evidence points, so it changes this string.
WEB_TEXT_PROJECTION_VERSION = "r5.visible_text.1"

#: Elements removed WITH their contents before any text is read. These carry
#: code, styling and inert templates -- never something the page states -- so
#: they must not be able to contribute a single character of evidence.
NON_VISIBLE_ELEMENTS: tuple[str, ...] = ("script", "style", "template", "noscript",
                                         "svg", "iframe", "head")

#: The hard ceiling on a committed page. The evidence contract cannot express a
#: document offset beyond this, so a projection that exceeded it could produce
#: locators no durable row could hold.
MAX_PROJECTION_CHARS = MAX_DOCUMENT_OFFSET

_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]*>")
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")


def _collapse(text: str) -> str:
    """Collapse horizontal whitespace and drop lines that hold no text."""
    lines = (_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _strip_markup(fragment: str) -> str:
    """Remove markup, then unescape -- in that order, never the reverse.

    Unescaping first would let an escaped `&lt;script&gt;` in ordinary page
    text become a real element for the tag stripper to act on, which is how a
    text-extraction routine ends up honouring markup an author wrote as data.
    """
    return _collapse(html.unescape(_TAG.sub("\n", fragment)))


def visible_text_projection(document: str) -> str:
    """The deterministic visible text of one saved page.

    `WEB_TEXT_PROJECTION_VERSION` names this exact procedure:

    1.  take the document TITLE -- the one piece of `<head>` a browser renders,
        and where this page states its full "Toyota RAV4 PLUGIN (PHEV)"
        designation -- as the first line;
    2.  remove every non-visible element together with its contents;
    3.  remove HTML comments;
    4.  replace every remaining tag with a line break, so text from two
        elements can never be concatenated into a sentence neither states;
    5.  unescape entities;
    6.  collapse horizontal whitespace and drop empty lines.

    Pure function of the input string: no I/O, no state, no model.
    """
    if not isinstance(document, str):
        raise ToolError("R5_WEB_DOCUMENT_INVALID", "the saved document is not text")
    title_match = _TITLE.search(document)
    title = _strip_markup(title_match.group(1)) if title_match else ""
    body = document
    for element in NON_VISIBLE_ELEMENTS:
        body = re.sub(rf"<{element}\b[^>]*>.*?</{element}\s*>", "\n", body,
                      flags=re.DOTALL | re.IGNORECASE)
    body = _strip_markup(_COMMENT.sub("\n", body))
    return f"{title}\n{body}" if title else body


@dataclass(frozen=True)
class DocumentStatement:
    """One statement a saved document is READ FOR, fixed in advance.

    `phrase` is the exact visible text the page must carry; `field_key` and
    `value` are what that phrase means, decided by a reviewer and written here
    rather than derived at run time. Nothing about a statement is a function of
    the document: the document can only satisfy a statement or fail to.
    """

    key: str
    field_key: str
    phrase: str
    value: str
    note: str


#: What the committed Toyota page is read for, and the entire list of it.
#:
#: `manufacturer_model_designation` is the page's own title line, which names
#: the make, the model and the plug-in/PHEV propulsion in one span. The value
#: is the page's text verbatim, not a rewriting of it.
#:
#: `marketing_status` is the one interpretive step in this module, and it is a
#: closed one: the exact sentence "שיווק הדגם ראב4 פלאג-אין הסתיים." -- "marketing
#: of the RAV4 Plug-in model has ended." -- maps to the single value `ended`.
#: The sentence is matched exactly; no other wording produces this fact, and
#: its absence fails the call rather than defaulting to "still marketed".
TOYOTA_RAV4_PHEV_STATEMENTS: tuple[DocumentStatement, ...] = (
    DocumentStatement(
        key="model_designation", field_key="manufacturer_model_designation",
        phrase="טויוטה ראב4 פלאג אין - Toyota RAV4 PLUGIN (PHEV)",
        value="טויוטה ראב4 פלאג אין - Toyota RAV4 PLUGIN (PHEV)",
        note="the page's own document title: make, model and plug-in/PHEV propulsion"),
    DocumentStatement(
        key="archived_model_heading", field_key="archived_model_heading",
        phrase="ראב4 פלאג-אין - RAV4 Plug-in",
        value="ראב4 פלאג-אין - RAV4 Plug-in",
        note="the archived model's own heading on the page body"),
    DocumentStatement(
        key="marketing_ended", field_key="marketing_status",
        phrase="שיווק הדגם ראב4 פלאג-אין הסתיים.", value="ended",
        note="'marketing of the RAV4 Plug-in model has ended' -- the archived-status "
             "sentence, matched exactly and mapped to one closed value"),
)

#: The closed document table. A `document_id` not named here has no fixture,
#: no statements and no canonical URL, so it cannot be read at all.
WEB_DOCUMENTS: Mapping[str, tuple[str, tuple[DocumentStatement, ...]]] = {
    "toyota_il_rav4_phev": ("web_toyota_rav4_phev", TOYOTA_RAV4_PHEV_STATEMENTS),
}

#: The vehicle the committed document is about, as the SOURCE names it. Toyota
#: Israel's own commercial name for this model is "RAV4 Plug-in", which is not
#: the bare "RAV4" the catalog and the registry use. Recording Toyota's own
#: name -- rather than normalizing it to match the other two sources -- is the
#: conservative choice: it keeps this page's statement about a differently
#: named commercial model from silently merging into their entity.
TOYOTA_DOCUMENT_VEHICLE: Mapping[str, str] = {
    "make": "Toyota", "commercial_model": "RAV4 Plug-in", "market": "IL",
}

_DOCUMENT_REQUEST = {
    "type": "object",
    "properties": {
        # The Registry's schema subset has no `enum`, and production's schema
        # validator is not widened for a proof. The closed authority is
        # `WEB_DOCUMENTS` itself: an identity it does not name has no fixture
        # and is refused at execution with `R5_WEB_DOCUMENT_UNKNOWN`, which is
        # the right layering anyway -- the schema states a shape, the
        # server-owned table states what exists.
        "document_id": {"type": "string"},
        # Asserted by the caller and CHECKED, never used to select anything:
        # the plan states which vehicle it believes the document is about, and
        # a document that does not say so fails the call closed.
        "make": {"type": "string"},
        "commercial_model": {"type": "string"},
        "market": {"type": "string"},
    },
    "required": ["commercial_model", "document_id", "make", "market"],
    "additionalProperties": False,
}

_DOCUMENT_RESULT = {
    "type": "object",
    "properties": {
        "source": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "canonical_url": {"type": "string"},
                "retrieved_at_utc": {"type": "string"},
                "content_sha256": {"type": "string"},
                "response_byte_count": {"type": "integer"},
                "text_projection_version": {"type": "string"},
                "projection_char_count": {"type": "integer"},
                # The digest of the WHOLE visible-text projection. It is what
                # makes an offset into that projection checkable by someone who
                # only has the committed bytes: re-derive, compare, then read.
                "projection_sha256": {"type": "string"},
            },
            "required": ["canonical_url", "content_sha256", "document_id",
                         "projection_char_count", "projection_sha256",
                         "response_byte_count", "retrieved_at_utc",
                         "text_projection_version"],
            "additionalProperties": False,
        },
        "model": {
            "type": "object",
            "properties": {"make": {"type": "string"},
                           "commercial_model": {"type": "string"},
                           "market": {"type": "string"}},
            "required": ["commercial_model", "make", "market"],
            "additionalProperties": False,
        },
        "statements": {
            "type": "array",
            "maxItems": len(TOYOTA_RAV4_PHEV_STATEMENTS),
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "field_key": {"type": "string"},
                    # `text` is what the projection says at the span below,
                    # read back out of it after the search; `value` is what
                    # that statement MEANS under the closed table above. For
                    # the two identity statements they are the same string,
                    # and for the archived-status sentence they are not.
                    "text": {"type": "string"},
                    "value": {"type": "string"},
                    "char_start": {"type": "integer"},
                    "char_end": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["char_end", "char_start", "field_key", "key", "note",
                             "text", "value"],
                "additionalProperties": False,
            },
        },
        # Stated so the proof's product output can say what this source does
        # NOT establish, instead of leaving a reader to assume it might.
        "states_no_technical_specification": {"type": "boolean"},
    },
    "required": ["model", "source", "statements", "states_no_technical_specification"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ToyotaArchivedModelDocumentTool:
    """One saved official page, exposed as ONE bounded read-only lookup.

    `read_archived_model_document` answers with the page's visible-text
    projection and the exact span of every statement the closed table expects,
    or it fails. There is no search operation, no selector expression and no
    way to ask for a span the table does not name, so the page cannot be mined
    for whatever a caller would like it to have said.
    """

    name: str = "toyota.archived_model_document"
    description: str = ("Saved official Toyota Israel archived-model page; identity and "
                        "archived status only, never a technical specification")
    required_scope: str = "web:saved_document_read"
    mode: ToolMode = ToolMode.READ
    operations = {
        "read_archived_model_document": ToolOperation(
            "read_archived_model_document",
            "Return the visible-text projection of one saved official page and the exact "
            "span of every archived-identity statement it must carry, or fail closed.",
            _DOCUMENT_REQUEST, _DOCUMENT_RESULT),
    }

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        document_id = str(payload["document_id"])
        selected = WEB_DOCUMENTS.get(document_id)
        if selected is None:
            raise ToolError("R5_WEB_DOCUMENT_UNKNOWN",
                            "no saved document is committed under this identity",
                            tool=self.name)
        source_key, statements = selected
        try:
            entry, text = load_text_fixture(source_key)
        except ProofManifestError as failure:
            raise ToolError(failure.reason_code, failure.safe_message, tool=self.name) from None
        if str(entry.get("document_id")) != document_id or \
                str(entry.get("text_projection_version")) != WEB_TEXT_PROJECTION_VERSION:
            # The manifest and this module disagree about what is committed, or
            # about how it is projected. Either way the offsets below would not
            # mean what the manifest says they mean.
            raise ToolError("R5_WEB_DOCUMENT_UNKNOWN",
                            "the committed document does not match its manifest identity",
                            tool=self.name)
        self._check_vehicle(payload)
        if not text or len(text) > MAX_PROJECTION_CHARS:
            raise ToolError("R5_WEB_DOCUMENT_INVALID",
                            "the saved document has no bounded visible text", tool=self.name)
        # The committed file must still BE a projection under the version the
        # manifest names. Idempotency is the checkable property of one: if
        # projecting it again changes anything, it is not a projection output,
        # and the offsets below would not be the offsets a reviewer re-derives.
        if visible_text_projection(text) != text:
            raise ToolError("R5_WEB_DOCUMENT_INVALID",
                            "the committed document is not a stable visible-text projection",
                            tool=self.name)
        return {
            "source": {
                "document_id": document_id,
                "canonical_url": str(entry["canonical_url"]),
                "retrieved_at_utc": str(entry["retrieved_at_utc"]),
                "content_sha256": str(entry["upstream_sha256"]),
                "response_byte_count": int(entry["response_byte_count"]),
                "text_projection_version": WEB_TEXT_PROJECTION_VERSION,
                "projection_char_count": len(text),
                "projection_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            "model": dict(TOYOTA_DOCUMENT_VEHICLE),
            "statements": [self._locate(text, statement) for statement in statements],
            # A statement of scope, not a discovery: this module extracts no
            # technical value from any document, by construction.
            "states_no_technical_specification": True,
        }

    def _check_vehicle(self, payload: Mapping[str, Any]) -> None:
        """The asserted vehicle must be the one this document is about."""
        for field, expected in TOYOTA_DOCUMENT_VEHICLE.items():
            if str(payload[field]).strip() != expected:
                raise ToolError("R5_WEB_DOCUMENT_IDENTITY_MISMATCH",
                                "the saved document does not describe this vehicle",
                                tool=self.name)

    def _locate(self, text: str, statement: DocumentStatement) -> dict[str, Any]:
        """The EXACT span of one expected phrase, or fail closed.

        Exactly one occurrence is required. Zero means the page no longer makes
        the statement the proof rests on -- the archived-status sentence being
        the one that matters most -- and more than one means the locator would
        be ambiguous. Neither is something to work around.
        """
        occurrences = [match.start() for match in
                       re.finditer(re.escape(statement.phrase), text)]
        if len(occurrences) != 1:
            raise ToolError(
                "R5_WEB_STATEMENT_ABSENT" if not occurrences else "R5_WEB_STATEMENT_AMBIGUOUS",
                "the saved document does not state this exactly once", tool=self.name)
        start, end = occurrences[0], occurrences[0] + len(statement.phrase)
        located = text[start:end]
        # The span is PROVEN here, against the real projection, because the
        # projection itself cannot cross the tool-result bound. Reading the
        # offsets back out rather than trusting the search is what makes the
        # returned text and the returned span impossible to disagree.
        if located != statement.phrase or end > MAX_DOCUMENT_OFFSET or \
                len(located) > MAX_FRAGMENT_CHARS:
            raise ToolError("R5_WEB_STATEMENT_ABSENT",
                            "the saved document does not state this exactly once",
                            tool=self.name)
        return {"key": statement.key, "field_key": statement.field_key,
                "text": located, "value": statement.value, "char_start": start,
                "char_end": end, "note": statement.note}


__all__ = ["MAX_PROJECTION_CHARS", "NON_VISIBLE_ELEMENTS", "TOYOTA_DOCUMENT_VEHICLE",
           "TOYOTA_RAV4_PHEV_STATEMENTS", "WEB_DOCUMENTS", "WEB_TEXT_PROJECTION_VERSION",
           "DocumentStatement", "ToyotaArchivedModelDocumentTool", "visible_text_projection"]
