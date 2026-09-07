"""R4: the durable support a stored verdict rests on.

Before R4 a verdict was three strings -- `{claim_id, verdict, reason}`.  The
grounded verifier already REQUIRED a `verified` answer to cite durable
fragment hashes of the claim's own source (B5), but it then dropped them at
the parse boundary, so nothing durable recorded WHICH evidence had settled the
claim, under WHICH rules, or by WHICH mechanism.  A stored `verified` was
therefore unauditable: it could not be re-checked, and a later release could
not tell a grounded decision from an ungrounded one.

This module defines the missing record and nothing else:

*   `SupportLink` -- one exact durable evidence row: its id when the resolver
    could supply one, its content hash always, and the locator it was read
    from.  Bounded identifiers only.  No fragment text, no prompt, no provider
    payload, no explanation and no chain of thought can be represented here --
    there is simply no field for them.
*   `VerificationMode` -- HOW the verdict was reached: by the deterministic
    structured comparison, by the grounded model verifier, or by a local
    deterministic rule that needed no evidence comparison at all.
*   `VERIFIER_CONTRACT_VERSION` -- the bounded identifier of the verification
    contract as a whole, persisted next to every verdict this release decides.

Pure module: no database access, no provider call, no tool execution, no
global mutable state.  Validation is the point of it -- a support link that
does not belong to the claim's own source, run, task and source version fails
closed here, before it can be stored or believed.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping, Sequence

from .comparison import STRUCTURED_COMPARISON_VERSION, UNIT_RULE_VERSION
from .contracts import SupportLink
from .evidence_bounds import MAX_VERIFIER_CONTRACT_VERSION_CHARS
from .fragments import MAX_FRAGMENTS_PER_SOURCE

# The bounded identifier of the whole R4 verification contract: the structured
# comparison rules, the unit allowlist and the support-link requirement,
# named together.  It is persisted with every verdict this release decides, so
# a durable verdict always states the contract that produced it and a verdict
# from an earlier release can never be presented as an R4-grounded one.
VERIFIER_CONTRACT_VERSION = f"r4.verifier.1+{STRUCTURED_COMPARISON_VERSION}+{UNIT_RULE_VERSION}"
if len(VERIFIER_CONTRACT_VERSION) > MAX_VERIFIER_CONTRACT_VERSION_CHARS:
    raise ValueError("the verifier contract version must fit the durable bound")

#: HOW a verdict was reached.  Closed and server-owned: a model never names
#: its own mode, and an unknown mode is a contract failure rather than a value
#: to be interpreted charitably.
VERIFICATION_MODES = ("deterministic_local", "deterministic_structured", "grounded_model")

#: The modes whose `verified` answers MUST cite durable evidence.  A locally
#: settled verdict (an unsupported claim, an unresolved conflict, a source
#: with no captured text) never says `verified`, so it never cites evidence.
EVIDENCE_BEARING_MODES = frozenset({"deterministic_structured", "grounded_model"})

MAX_SUPPORT_LINKS_PER_VERDICT = MAX_FRAGMENTS_PER_SOURCE

SUPPORT_REASONS = frozenset({
    "SUPPORT_LINK_INVALID",
    "SUPPORT_LINK_FOREIGN_SOURCE",
    "SUPPORT_LINK_UNKNOWN_EVIDENCE",
    "SUPPORT_LINK_MISSING",
    "SUPPORT_LINK_VERSION_MISMATCH",
})


class SupportContractError(ValueError):
    """A support-link failure carrying ONLY a static, code-owned reason.

    The rejected link, the stored hash and the resolver diagnostic never
    travel with the classification, so the safe representation is fit for
    durable state, a run event and telemetry alike.
    """

    MESSAGES = {
        "SUPPORT_LINK_INVALID": "a verdict support link is malformed",
        "SUPPORT_LINK_FOREIGN_SOURCE": "a verdict support link belongs to another source",
        "SUPPORT_LINK_UNKNOWN_EVIDENCE": "a verdict support link cites evidence that was not supplied",
        "SUPPORT_LINK_MISSING": "an accepted verdict cites no durable evidence",
        "SUPPORT_LINK_VERSION_MISMATCH": "a verdict support link belongs to another source version",
    }

    def __init__(self, reason_code: str):
        if reason_code not in SUPPORT_REASONS:
            raise ValueError("support reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


def _link(source: Any, fragment: Any) -> SupportLink:
    """One validated link to ONE durable fragment of the resolved source."""
    try:
        return SupportLink(source_id=source.source_id, content_hash=fragment.content_hash,
                           fragment_id=getattr(fragment, "fragment_id", None),
                           locator=fragment.locator)
    except SupportContractError:
        raise
    except Exception:
        # `from None`: a pydantic message quotes the rejected material.
        raise SupportContractError("SUPPORT_LINK_INVALID") from None


def links_for_hashes(source: Any, hashes: Iterable[str]) -> tuple[SupportLink, ...]:
    """Build the support links for hashes cited against ONE resolved source.

    Every hash must be one the source actually supplied; a hash from another
    source in the same batch, an unknown hash and an invented hash all fail
    closed here rather than being stored as provenance nobody can replay.
    """
    by_hash: dict[str, list[Any]] = {}
    for fragment in source.fragments:
        by_hash.setdefault(fragment.content_hash, []).append(fragment)
    links: list[SupportLink] = []
    for content_hash in sorted(set(hashes)):
        fragments = by_hash.get(content_hash)
        if not fragments:
            raise SupportContractError("SUPPORT_LINK_UNKNOWN_EVIDENCE")
        for fragment in sorted(fragments, key=lambda item: item.fragment_index):
            links.append(_link(source, fragment))
    return tuple(sorted(links, key=lambda item: item.identity))


def links_for_locator(source: Any, locator: str | None) -> tuple[SupportLink, ...]:
    """The support links of the fragments recorded at ONE exact locator.

    This is how a deterministic structured decision names its evidence: the
    fact it compared against was read at a locator, and R3 guarantees that
    locator is also one of the source's own focused fragment locators, so the
    fragments there are exactly the durable rows behind the decision.
    """
    if not locator:
        raise SupportContractError("SUPPORT_LINK_MISSING")
    fragments = [item for item in source.fragments if item.locator == locator]
    if not fragments:
        raise SupportContractError("SUPPORT_LINK_UNKNOWN_EVIDENCE")
    return tuple(sorted((_link(source, item) for item in fragments),
                        key=lambda item: item.identity))


def validate_support(links: Sequence[SupportLink], *, source: Any,
                     claim_source_version: str | None = None) -> tuple[SupportLink, ...]:
    """Re-check stored or incoming links against the claim's OWN source context.

    Applied at every boundary a support link crosses, so a forged link, a link
    copied from another source and a link to evidence that was never supplied
    are refused wherever they appear -- including on the way back out of a
    checkpoint.  The claim's recorded source version must still be the
    version the durable source reports, so evidence read at one version can
    never be presented as support for a claim read at another.
    """
    if len(links) > MAX_SUPPORT_LINKS_PER_VERDICT:
        raise SupportContractError("SUPPORT_LINK_INVALID")
    if claim_source_version is not None and claim_source_version != source.source_version:
        raise SupportContractError("SUPPORT_LINK_VERSION_MISMATCH")
    supplied = {(item.content_hash, item.locator) for item in source.fragments}
    hashes = {item.content_hash for item in source.fragments}
    for link in links:
        if not isinstance(link, SupportLink):
            raise SupportContractError("SUPPORT_LINK_INVALID")
        if link.source_id != source.source_id:
            raise SupportContractError("SUPPORT_LINK_FOREIGN_SOURCE")
        if link.locator is not None:
            if (link.content_hash, link.locator) not in supplied:
                raise SupportContractError("SUPPORT_LINK_UNKNOWN_EVIDENCE")
        elif link.content_hash not in hashes:
            raise SupportContractError("SUPPORT_LINK_UNKNOWN_EVIDENCE")
    identities = [link.identity for link in links]
    if len(set(identities)) != len(identities):
        raise SupportContractError("SUPPORT_LINK_INVALID")
    return tuple(sorted(links, key=lambda item: item.identity))


def parse_support(raw: Any) -> tuple[SupportLink, ...]:
    """Rebuild support links from durable state, or fail closed.

    A checkpoint is durable data, not a trusted object graph: every stored
    link is revalidated through the same contract that created it.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, (list, tuple)):
        raise SupportContractError("SUPPORT_LINK_INVALID")
    links: list[SupportLink] = []
    for item in raw:
        if isinstance(item, SupportLink):
            links.append(item)
            continue
        if not isinstance(item, Mapping):
            raise SupportContractError("SUPPORT_LINK_INVALID")
        try:
            links.append(SupportLink(**{str(key): value for key, value in item.items()}))
        except SupportContractError:
            raise
        except Exception:
            # `from None`: a pydantic message quotes the rejected link.
            raise SupportContractError("SUPPORT_LINK_INVALID") from None
    if len(links) > MAX_SUPPORT_LINKS_PER_VERDICT:
        raise SupportContractError("SUPPORT_LINK_INVALID")
    return tuple(sorted(links, key=lambda item: item.identity))


VerificationMode = Literal["deterministic_local", "deterministic_structured", "grounded_model"]


__all__ = ["EVIDENCE_BEARING_MODES", "MAX_SUPPORT_LINKS_PER_VERDICT",
           "MAX_VERIFIER_CONTRACT_VERSION_CHARS", "SUPPORT_REASONS", "VERIFICATION_MODES",
           "VERIFIER_CONTRACT_VERSION",
           "SupportContractError", "SupportLink", "VerificationMode", "links_for_hashes",
           "links_for_locator", "parse_support", "validate_support"]
