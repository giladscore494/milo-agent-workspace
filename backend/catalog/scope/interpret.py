"""Deterministic reading of a typed mapping instruction, in English or Hebrew.

What this is
------------

The chat half of the ONE work-scope contract. A person types

    "Map Toyota and Lexus, starting with 2018+, up to 800 variants."
    "Continue with Japanese manufacturers we have not mapped yet."
    "Do Toyota first, then Mazda. Stop after 500 vehicles."

or the same in Hebrew, and this module reduces the text to the same four
fields a Mapping Plan edit sends -- units, model years, candidate limit, batch
size -- which `contract.scope_from_fields` then validates exactly as it
validates a click. There is no second scope: an instruction is only another way
of stating the fields.

What it is NOT
--------------

Not a model. No provider is called, nothing is sampled, and the same text over
the same current plan and the same coverage always produces the same fields --
`tests/test_work_scope.py` pins that with golden cases. A model could read more
phrasings; it could also produce a structure nobody can predict, and a
model-generated structure is not execution authority. The reader is small and
closed on purpose, and it says what it did not understand rather than guessing.

The reading, in the order it binds
----------------------------------

1.  The text is normalized (NFKD without combining marks, NFKC, casefold;
    every geresh, apostrophe and dash spelling folded onto one) and split into
    numbers, Latin words, Hebrew words and separators. Nothing else survives.
2.  Fixed phrase rules consume, in this order: year RANGES ("2018-2024",
    "between 2018 and 2024", "בין 2018 ל-2024"); BATCH sizes ("batches of 10",
    "באצוות של 10"); candidate LIMITS stated with a limit word or an item noun
    ("stop after 500", "up to 800 variants", "עד 800 דגמים"); single year
    BOUNDS ("2018+", "from 2018", "until 2024", "החל מ-2018"); a bare
    "up to N" that is not a plausible year, as a limit; "all years"; and the
    "not mapped yet" qualifier.
3.  Manufacturer names and origin groups ("Japanese manufacturers") are
    matched against the directory's aliases, longest phrase first. A Hebrew
    word may carry up to two attached prefix letters (ו, ה, ב, ל, מ, ש, כ),
    tried only when the whole word is not itself an alias.
4.  Verbs give each mention a role: SET (the default -- "map", "continue with",
    "only"), ADD ("add", "also", "גם"), REMOVE ("without", "except", "בלי"),
    and FIRST ("first", "קודם"), which marks the nearest mention in its clause.
    Clauses end at punctuation and at "then" / "אחר כך".
5.  Whatever is left that is not a known filler word is reported back as
    UNRECOGNIZED, verbatim and bounded, so a misspelt marque is visible rather
    than silently dropped. An instruction in which NOTHING was recognized is
    refused outright.

How an instruction changes a plan
---------------------------------

A field the instruction states replaces that field; a field it does not state
keeps the current plan's value. A single year bound replaces BOTH bounds
("2018+" means from 2018, with no upper bound). For the manufacturer list:
SET mentions replace the list, in the order they were named; ADD mentions are
appended; REMOVE mentions are dropped; FIRST mentions move to the front in the
order they were named. An instruction whose only mentions are FIRST mentions
("Mazda first") reorders the current plan rather than replacing it. The "not
mapped yet" qualifier drops every SET/ADD manufacturer the canonical catalog is
KNOWN to hold, keeps one it is known not to hold, and keeps -- with a note --
one whose coverage cannot be stated because its register spelling is not
verified. A marque is never skipped because of what is not known about it.

Pure module: string handling only. No I/O, no clock, no environment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import contract as wsc
from . import directory as mdir

#: The longest instruction this reader accepts. A mapping instruction is a
#: sentence or two; anything longer is not one, and the bound keeps the stored
#: instruction history bounded too.
MAX_INSTRUCTION_CHARS = 500

#: How many unrecognized words a note reports, and how long each may be.
MAX_REPORTED_TERMS = 5
MAX_TERM_CHARS = 30

#: A year inside this window reads as a model year where a bare number could
#: also be a limit ("up to 2020"). Outside it the number is a limit. Only the
#: DISAMBIGUATION uses this window; the contract's own year bounds still apply.
PLAUSIBLE_YEAR_MIN = 1980
PLAUSIBLE_YEAR_MAX = 2040

#: The closed vocabulary of notes an interpretation may return.
NOTE_CODES = (
    "WORK_SCOPE_NOTE_DEFAULT_LIMIT",     # a new plan stated no limit
    "WORK_SCOPE_NOTE_UNRECOGNIZED",      # words the reader could not place
    "WORK_SCOPE_NOTE_ALREADY_MAPPED",    # dropped by "not mapped yet"
    "WORK_SCOPE_NOTE_COVERAGE_UNKNOWN",  # kept, but coverage cannot be stated
    "WORK_SCOPE_NOTE_NOT_IN_PLAN",       # asked to remove what the plan lacks
    "WORK_SCOPE_NOTE_NO_CHANGE",         # understood, and the plan is identical
)

INSTRUCTION_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_INSTRUCTION_INVALID":
        f"an instruction is printable text of 1 to {MAX_INSTRUCTION_CHARS} characters",
    "WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD":
        "nothing in the instruction names a manufacturer, a year, a limit or a batch size",
}


class InstructionError(ValueError):
    """A refusal carrying ONLY a static, code-owned message."""

    def __init__(self, code: str):
        if code not in INSTRUCTION_REASONS:
            raise ValueError("instruction refusal must come from the static allowlist")
        self.code = code
        self.safe_message = INSTRUCTION_REASONS[code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class Note:
    """One thing a person should know about how their words were read."""

    code: str
    terms: tuple[str, ...] = ()
    units: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.code not in NOTE_CODES:
            raise ValueError("work scope note must come from the closed vocabulary")

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"code": self.code}
        if self.terms:
            record["terms"] = list(self.terms)
        if self.units:
            record["units"] = list(self.units)
        return record


# ---------------------------------------------------------------------------
# 1. Normalization and tokens.
# ---------------------------------------------------------------------------

_FOLD = str.maketrans({
    "\u05f3": "'", "\u2019": "'", "\u2018": "'", "`": "'", "\u00b4": "'",
    "\u05f4": '"', "\u201c": '"', "\u201d": '"',
    "\u05be": "-", "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2212": "-",
})
_LETTER = r"a-z\u05d0-\u05ea"
#: "mercedes-benz" and "מרצדס-בנץ" are two words; "2018-2024" keeps its dash.
_WORD_HYPHEN = re.compile(rf"(?<=[{_LETTER}])-(?=[{_LETTER}])")
#: "ב.מ.וו" and "b.m.w" are one word, not three clauses.
_ABBREVIATION_DOT = re.compile(rf"(?<=[{_LETTER}])\.(?=[{_LETTER}])")
#: "1,000" is one thousand, not a clause break.
_THOUSANDS = re.compile(r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))")
_TOKEN = re.compile(
    r"(?P<num>[0-9]{1,9})"
    r"|(?P<lat>[a-z]+(?:'[a-z]+)*)"
    r"|(?P<heb>[\u05d0-\u05ea]+(?:['\"][\u05d0-\u05ea]+)*'?)"
    r"|(?P<dash>-)"
    r"|(?P<plus>\+)"
    r"|(?P<sep>[,.;:!?\n])")

HEBREW_PREFIXES = frozenset("והבלמשכ")


@dataclass
class _Token:
    kind: str
    text: str
    used: bool = False

    @property
    def number(self) -> int | None:
        return int(self.text) if self.kind == "num" else None


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    folded = unicodedata.normalize("NFKC", stripped).casefold().translate(_FOLD)
    folded = _THOUSANDS.sub("", folded)
    folded = _ABBREVIATION_DOT.sub("", folded)
    return _WORD_HYPHEN.sub(" ", folded)


def _tokens(text: str) -> list[_Token]:
    return [_Token(match.lastgroup or "", match.group()) for match in _TOKEN.finditer(normalize(text))]


def phrase(text: str) -> tuple[str, ...]:
    """An alias or keyword, tokenized exactly as an instruction is."""
    return tuple(token.text for token in _tokens(text) if token.kind in ("lat", "heb", "num"))


def _hebrew_forms(word: str) -> tuple[str, ...]:
    """The word, then the word without one and two attached prefix letters."""
    forms = [word]
    if word and word[0] in HEBREW_PREFIXES and len(word) > 2:
        forms.append(word[1:])
        if word[1] in HEBREW_PREFIXES and len(word) > 3:
            forms.append(word[2:])
    return tuple(forms)


# ---------------------------------------------------------------------------
# 2. The closed vocabularies.
# ---------------------------------------------------------------------------

def _words(*items: str) -> frozenset[str]:
    return frozenset(items)


ITEM_NOUNS = _words(
    "variant", "variants", "vehicle", "vehicles", "car", "cars", "model", "models",
    "candidate", "candidates", "item", "items", "row", "rows", "entry", "entries",
    "דגם", "דגמים", "רכב", "רכבים", "גרסה", "גרסאות", "מכונית", "מכוניות", "פריטים",
    "וריאנטים", "כלי")
GROUP_NOUNS = _words(
    "manufacturer", "manufacturers", "brand", "brands", "maker", "makers", "make", "makes",
    "carmakers", "automakers", "marques", "marque", "companies",
    "יצרן", "יצרנים", "יצרני", "יצרניות", "מותג", "מותגים", "חברות")
ORIGIN_WORDS: Mapping[str, str] = {
    **{word: "japan" for word in ("japanese", "japan", "יפני", "יפניים", "יפנים", "יפניות",
                                   "יפנית", "יפן")},
    **{word: "south_korea" for word in ("korean", "korea", "קוריאני", "קוריאניים",
                                         "קוריאנים", "קוריאניות", "קוריאה")},
    **{word: "germany" for word in ("german", "germany", "גרמני", "גרמניים", "גרמנים",
                                     "גרמניות", "גרמניה")},
    **{word: "france" for word in ("french", "france", "צרפתי", "צרפתיים", "צרפתים",
                                    "צרפתיות", "צרפת")},
    **{word: "italy" for word in ("italian", "italy", "איטלקי", "איטלקיים", "איטלקים",
                                   "איטליה")},
    **{word: "spain" for word in ("spanish", "spain", "ספרדי", "ספרדיים", "ספרד")},
    **{word: "czechia" for word in ("czech", "czechia", "צ'כי", "צ'כיים", "צ'כיה")},
    **{word: "romania" for word in ("romanian", "romania", "רומני", "רומניים", "רומניה")},
    **{word: "sweden" for word in ("swedish", "sweden", "שוודי", "שוודיים", "שבדי",
                                    "שוודיה")},
    **{word: "uk" for word in ("british", "uk", "english", "בריטי", "בריטיים", "אנגלי",
                                "אנגליים", "בריטניה")},
    **{word: "usa" for word in ("american", "usa", "אמריקאי", "אמריקאיים", "אמריקני",
                                 "אמריקניים", "אמריקאים", "ארה\"ב")},
    **{word: "china" for word in ("chinese", "china", "סיני", "סיניים", "סינים", "סין")},
}
ALL_WORDS = _words("all", "every", "כל")

SET_VERBS = _words("map", "do", "only", "just", "continue", "focus", "cover", "target",
                   "מפה", "תמפה", "למפות", "נמפה", "המשך", "תמשיך", "להמשיך", "נמשיך", "רק",
                   "תעשה", "עשה", "בצע")
ADD_VERBS = _words("add", "also", "include", "including", "plus", "additionally",
                   "הוסף", "תוסיף", "להוסיף", "נוסיף", "גם", "בנוסף", "כולל")
REMOVE_VERBS = _words("remove", "drop", "without", "except", "exclude", "excluding", "skip",
                      "minus", "delete", "not", "הסר", "תסיר", "להסיר", "בלי", "ללא", "חוץ",
                      "מלבד", "הורד", "תוריד", "להוריד", "למעט", "לא")
#: A negated SET verb is a removal, and the verb it negates is part of it:
#: "do not map Lexus" must not read "map" as a fresh SET for Lexus.
NEGATED_VERB_PHRASES: tuple[tuple[str, ...], ...] = (
    ("do", "not", "map"), ("do", "not", "include"), ("don't", "map"), ("dont", "map"),
    ("don't", "include"), ("dont", "include"), ("never", "map"), ("אל", "תמפה"),
    ("לא", "למפות"), ("אל", "תכלול"), ("לא", "לכלול"))
FIRST_VERBS = _words("first", "firstly", "prioritize", "prioritise", "start", "starting",
                     "begin", "beginning", "קודם", "ראשון", "ראשונה", "בהתחלה", "תחילה",
                     "תתחיל", "התחל", "להתחיל")
BOUNDARY_WORDS = _words("then", "next", "afterwards", "afterward", "ואז")
BOUNDARY_PHRASES = (("after", "that"), ("אחר", "כך"), ("אחרי", "זה"), ("לאחר", "מכן"))

#: Words that carry no instruction by themselves. Consumed silently, so a
#: natural sentence does not come back full of "unrecognized" filler -- and
#: only AFTER every rule above has had its chance at them.
FILLER_WORDS = _words(
    "a", "an", "the", "and", "or", "with", "of", "for", "to", "please", "pls", "we", "i",
    "you", "me", "us", "our", "let", "let's", "lets", "want", "would", "like", "could",
    "can", "should", "need", "it", "its", "them", "those", "these", "that", "which", "who",
    "is", "are", "be", "been", "have", "has", "had", "yet", "already", "still", "now",
    "too", "as", "well", "in", "on", "at", "by", "from", "into", "all", "any", "each",
    "every", "more", "some", "other", "others", "remaining", "rest", "year", "years",
    "catalog", "catalogue", "mapping", "mapped", "ok", "okay", "thanks", "thank", "go",
    "ahead", "their", "there", "here", "so", "but", "if", "same", "one", "ones", "scope",
    "plan", "batch", "batches", "per", "up", "stop", "after", "until", "between",
    "את", "של", "עם", "אל", "על", "אנא", "בבקשה", "אני", "אנחנו", "רוצה", "רוצים", "צריך",
    "זה", "זאת", "אלה", "אלו", "כל", "עכשיו", "כבר", "עוד", "עדיין", "שנים", "שנה", "שנת",
    "קטלוג", "מיפוי", "לי", "לנו", "אותם", "אותן", "שלנו", "הבאים", "הבאות", "נוספים",
    "אחרים", "שאר", "יתר", "תודה", "אוקיי", "בסדר", "אחר", "כך", "אחרי", "לאחר", "מכן",
    "עד", "בין", "טרם", "אצווה", "אצוות", "מנות", *ITEM_NOUNS, *GROUP_NOUNS)

#: "not mapped yet", as the fixed phrases a person writes it in.
UNMAPPED_PHRASES: tuple[tuple[str, ...], ...] = tuple(sorted({
    ("not", "mapped"), ("not", "yet", "mapped"), ("not", "been", "mapped"),
    ("not", "yet", "been", "mapped"), ("haven't", "mapped"), ("havent", "mapped"),
    ("haven't", "yet", "mapped"), ("have", "not", "mapped"), ("have", "not", "yet", "mapped"),
    ("have", "not", "been", "mapped"), ("hasn't", "been", "mapped"), ("has", "not", "been",
                                                                       "mapped"),
    ("unmapped",), ("not", "in", "the", "catalog"), ("not", "in", "the", "catalogue"),
    ("שלא", "מיפינו"), ("שעוד", "לא", "מיפינו"), ("שטרם", "מיפינו"), ("עוד", "לא", "מיפינו"),
    ("עדיין", "לא", "מיפינו"), ("שעדיין", "לא", "מיפינו"), ("שלא", "מופו"),
    ("שעוד", "לא", "מופו"), ("שטרם", "מופו"), ("שעדיין", "לא", "מופו"), ("לא", "ממופים"),
    ("שאינם", "ממופים"), ("שלא", "ממופים"),
}, key=lambda words: (-len(words), words)))
UNMAPPED_TRAILERS = _words("yet", "עדיין", "עוד")


def _alias_index() -> dict[tuple[str, ...], str]:
    index: dict[tuple[str, ...], str] = {}
    for entry in mdir.DIRECTORY:
        for alias in (entry.key.replace("_", " "), *entry.aliases):
            words = phrase(alias)
            if words and index.setdefault(words, entry.key) != entry.key:
                raise RuntimeError("two directory entries share one alias")
    return index


#: Alias phrase -> directory key. Built from the directory at import, and
#: refused at import if two entries would share a spelling.
ALIASES: Mapping[tuple[str, ...], str] = _alias_index()
MAX_ALIAS_WORDS = max(len(words) for words in ALIASES)


# ---------------------------------------------------------------------------
# 3. The reading.
# ---------------------------------------------------------------------------

@dataclass
class _Mention:
    keys: tuple[str, ...]
    position: int
    group: bool
    mode: str = "set"
    first: bool = False


@dataclass
class Reading:
    """What one instruction states, before it is applied to a plan."""

    mentions: list[_Mention] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    years_stated: bool = False
    max_items: int | None = None
    batch_size: int | None = None
    unmapped_only: bool = False
    unrecognized: tuple[str, ...] = ()

    @property
    def recognized(self) -> bool:
        return bool(self.mentions or self.years_stated or self.max_items is not None
                    or self.batch_size is not None)

    @property
    def needs_coverage(self) -> bool:
        """Only the "not mapped yet" qualifier makes a reading depend on what
        the catalog holds, so only then is coverage read at all."""
        return self.unmapped_only and any(m.mode in ("set", "add") for m in self.mentions)


def _is_year(value: int | None) -> bool:
    return value is not None and wsc.MIN_MODEL_YEAR <= value <= wsc.MAX_MODEL_YEAR


def _plausible_year(value: int | None) -> bool:
    return value is not None and PLAUSIBLE_YEAR_MIN <= value <= PLAUSIBLE_YEAR_MAX


class _Scanner:
    """Fixed phrase rules over the token list. Every rule consumes what it reads."""

    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.reading = Reading()

    # -- token helpers -----------------------------------------------------------
    def free(self, index: int) -> _Token | None:
        if 0 <= index < len(self.tokens) and not self.tokens[index].used:
            return self.tokens[index]
        return None

    def word(self, index: int, words: Iterable[str]) -> bool:
        token = self.free(index)
        return token is not None and token.kind in ("lat", "heb") and token.text in words

    def num(self, index: int) -> int | None:
        token = self.free(index)
        return token.number if token is not None else None

    def kind(self, index: int, kind: str) -> bool:
        token = self.free(index)
        return token is not None and token.kind == kind

    def consume(self, start: int, end: int) -> None:
        for token in self.tokens[start:end]:
            token.used = True

    def optional(self, index: int, words: Iterable[str]) -> int:
        return index + 1 if self.word(index, words) else index

    def optional_dash(self, index: int) -> int:
        return index + 1 if self.kind(index, "dash") else index

    # -- 2a. year ranges -------------------------------------------------------------
    def year_ranges(self) -> None:
        for i in range(len(self.tokens)):
            end = self._range_at(i)
            if end is not None:
                self.consume(i, end)

    def _range_at(self, i: int) -> int | None:
        if self.word(i, ("between", "בין")):
            first = self.num(i + 1)
            j = self.optional(i + 2, ("and", "to", "ל", "עד", "ו"))
            j = self.optional_dash(j)
            second = self.num(j)
            if _is_year(first) and _is_year(second) and j > i + 2:
                self._years(first, second)
                return j + 1
            return None
        first = self.num(i)
        if not _is_year(first):
            return None
        if self.kind(i + 1, "dash") and _is_year(self.num(i + 2)):
            self._years(first, self.num(i + 2))
            return i + 3
        if self.word(i + 1, ("to", "until", "through", "thru", "till", "עד", "ל")):
            j = self.optional_dash(i + 2)
            if _is_year(self.num(j)):
                self._years(first, self.num(j))
                return j + 1
        return None

    def _years(self, first: int | None, second: int | None) -> None:
        self.reading.year_from, self.reading.year_to = first, second
        self.reading.years_stated = True

    # -- 2b. batch sizes -------------------------------------------------------------
    def batch_sizes(self) -> None:
        for i in range(len(self.tokens)):
            end = self._batch_at(i)
            if end is not None:
                self.consume(i, end)

    def _batch_at(self, i: int) -> int | None:
        if self.word(i, ("batch", "batches")):
            j = self.optional(i + 1, ("size", "sized", "sizes"))
            j = self.optional(j, ("of", "is"))
            if self.num(j) is not None:
                self.reading.batch_size = self.num(j)
                return j + 1
            return None
        if self.word(i, ("אצוות", "באצוות", "אצווה", "באצווה", "מנות", "במנות", "מנה")):
            j = self.optional(i + 1, ("של",))
            if self.num(j) is not None:
                self.reading.batch_size = self.num(j)
                return j + 1
            return None
        if self.word(i, ("גודל",)) and self.word(i + 1, ("אצווה", "האצווה", "מנה", "המנה")):
            j = self.optional(i + 2, ("של",))
            if self.num(j) is not None:
                self.reading.batch_size = self.num(j)
                return j + 1
            return None
        value = self.num(i)
        if value is None:
            return None
        j = self.optional(i + 1, ITEM_NOUNS)
        if self.word(j, ("per", "each", "every", "a")) and self.word(j + 1, ("batch",)):
            self.reading.batch_size = value
            return j + 2
        if self.word(j, ("בכל", "לכל")) and self.word(j + 1, ("אצווה", "מנה")):
            self.reading.batch_size = value
            return j + 2
        return None

    # -- 2c. limits stated with a limit word or an item noun -----------------------------
    def explicit_limits(self) -> None:
        for i in range(len(self.tokens)):
            end = self._limit_at(i)
            if end is not None:
                self.consume(i, end)

    def _limit(self, j: int) -> int | None:
        """A number at `j`, optionally followed by an item noun: the end index."""
        if self.num(j) is None:
            return None
        self.reading.max_items = self.num(j)
        return self.optional(j + 1, ITEM_NOUNS)

    def _limit_at(self, i: int) -> int | None:
        if self.word(i, ("stop", "עצור", "תעצור", "לעצור")):
            j = self.optional(i + 1, ("after", "at", "אחרי", "לאחר", "אחר", "ב"))
            j = self.optional_dash(j)
            return self._limit(j) if j > i + 1 else None
        if self.word(i, ("max", "maximum", "cap", "מקסימום", "מקס")):
            j = self.optional(i + 1, ("of", "at"))
            return self._limit(j)
        if self.word(i, ("limit", "limited", "capped", "הגבל", "הגבלה")):
            j = self.optional(i + 1, ("to", "of", "at", "ל"))
            j = self.optional_dash(j)
            return self._limit(j)
        if self.word(i, ("at",)) and self.word(i + 1, ("most",)):
            return self._limit(i + 2)
        if self.word(i, ("no",)) and self.word(i + 1, ("more",)) and self.word(i + 2, ("than",)):
            return self._limit(i + 3)
        if self.word(i, ("לכל",)) and self.word(i + 1, ("היותר",)):
            return self._limit(i + 2)
        if self.word(i, ("לא",)) and self.word(i + 1, ("יותר",)):
            j = self.optional(i + 2, ("מ",))
            j = self.optional_dash(j)
            return self._limit(j)
        # "up to 800 variants" / "עד 800 דגמים": a limit only WITH its noun here;
        # the noun-less form waits until the year rules have had their turn.
        if self.word(i, ("up",)) and self.word(i + 1, ("to",)):
            return self._noun_limit(i + 2)
        if self.word(i, ("עד",)):
            return self._noun_limit(i + 1)
        value = self.num(i)
        if value is not None and not _plausible_year(value) and self.word(i + 1, ITEM_NOUNS):
            self.reading.max_items = value
            return i + 2
        return None

    def _noun_limit(self, j: int) -> int | None:
        if self.num(j) is not None and self.word(j + 1, ITEM_NOUNS):
            self.reading.max_items = self.num(j)
            return j + 2
        return None

    # -- 2d. single year bounds --------------------------------------------------------
    def year_bounds(self) -> None:
        for i in range(len(self.tokens)):
            end = self._bound_at(i)
            if end is not None:
                self.consume(i, end)

    def _from(self, year: int | None, end: int) -> int:
        self._years(year, None)
        return end + 1 if self.kind(end, "plus") else end

    def _bound_at(self, i: int) -> int | None:
        value = self.num(i)
        if _is_year(value):
            if self.kind(i + 1, "plus"):
                return self._from(value, i + 1)
            j = self.optional(i + 1, ("and", "or"))
            if self.word(j, ("later", "newer", "onward", "onwards", "up", "above", "beyond",
                             "forward")):
                return self._from(value, j + 1)
            if self.word(i + 1, ("ומעלה", "והלאה", "ואילך", "ואלך")):
                return self._from(value, i + 2)
            return None
        if self.word(i, ("from", "since", "starting", "beginning", "start", "begin")):
            j = self.optional(i + 1, ("with", "from", "in", "at"))
            j = self.optional(j, ("model",))
            j = self.optional(j, ("year", "years"))
            if _is_year(self.num(j)):
                return self._from(self.num(j), j + 1)
            return None
        if self.word(i, ("החל",)):
            j = self.optional(i + 1, ("מ", "משנת", "משנה"))
            j = self.optional(j, ("שנת",))
            j = self.optional_dash(j)
            if j > i + 1 and _is_year(self.num(j)):
                return self._from(self.num(j), j + 1)
            return None
        if self.word(i, ("מ", "משנת", "משנה", "מאז", "מתחילת")):
            j = self.optional(i + 1, ("שנת",))
            j = self.optional_dash(j)
            if _is_year(self.num(j)):
                return self._from(self.num(j), j + 1)
            return None
        if self.word(i, ("until", "through", "thru", "till", "to", "before", "לפני", "עד")) \
                or (self.word(i, ("up",)) and self.word(i + 1, ("to",))):
            before = self.word(i, ("before", "לפני"))
            j = i + 2 if self.word(i, ("up",)) else i + 1
            j = self.optional(j, ("model", "שנת"))
            j = self.optional(j, ("year",))
            j = self.optional_dash(j)
            value = self.num(j)
            if _plausible_year(value) and not self.word(j + 1, ITEM_NOUNS):
                self._years(None, value - 1 if before else value)
                return j + 1
        return None

    # -- 2e. a bare "up to N" that is not a year -----------------------------------
    def bare_limits(self) -> None:
        for i in range(len(self.tokens)):
            if self.word(i, ("up",)) and self.word(i + 1, ("to",)) and self.num(i + 2) is not None:
                self.reading.max_items = self.num(i + 2)
                self.consume(i, i + 3)
            elif self.word(i, ("עד",)) and self.num(i + 1) is not None:
                self.reading.max_items = self.num(i + 1)
                self.consume(i, i + 2)

    # -- 2f. "all years" ---------------------------------------------------------------
    def year_resets(self) -> None:
        for i in range(len(self.tokens)):
            if self.word(i, ("all", "any", "every")):
                j = self.optional(i + 1, ("model",))
                if self.word(j, ("years", "year")):
                    self._years(None, None)
                    self.consume(i, j + 1)
            elif self.word(i, ("כל",)) and self.word(i + 1, ("השנים", "שנות", "השנתונים")):
                self._years(None, None)
                self.consume(i, i + 2)

    # -- 2g. "not mapped yet" -----------------------------------------------------------
    def unmapped_qualifiers(self) -> None:
        for i in range(len(self.tokens)):
            for words in UNMAPPED_PHRASES:
                if all(self.word(i + k, (word,)) for k, word in enumerate(words)):
                    end = self.optional(i + len(words), UNMAPPED_TRAILERS)
                    self.reading.unmapped_only = True
                    self.consume(i, end)
                    break

    # -- 3. manufacturers and groups ------------------------------------------------------
    def mentions(self) -> None:
        i = 0
        while i < len(self.tokens):
            end = self._mention_at(i)
            i = end if end is not None else i + 1

    def _forms(self, index: int) -> tuple[str, ...]:
        """The spellings one free word may be matched under."""
        token = self.free(index)
        if token is None or token.kind not in ("lat", "heb"):
            return ()
        if token.kind == "heb":
            return _hebrew_forms(token.text)
        return (token.text, token.text[:-2]) if token.text.endswith("'s") else (token.text,)

    def _alias_at(self, i: int) -> tuple[str, int] | None:
        """The longest alias starting at `i`, and where it ends."""
        for size in range(MAX_ALIAS_WORDS, 1, -1):
            rest = [self.free(i + k) for k in range(1, size)]
            if not all(t is not None and t.kind in ("lat", "heb", "num") for t in rest):
                continue
            tail = tuple(t.text for t in rest)  # type: ignore[union-attr]
            for form in self._forms(i):
                key = ALIASES.get((form, *tail))
                if key is not None:
                    return key, i + size
        for form in self._forms(i):
            key = ALIASES.get((form,))
            if key is not None:
                return key, i + 1
        return None

    def _origin_at(self, i: int) -> str | None:
        return next((ORIGIN_WORDS[form] for form in self._forms(i) if form in ORIGIN_WORDS),
                    None)

    def _group_noun(self, i: int) -> bool:
        return any(form in GROUP_NOUNS for form in self._forms(i))

    def _mention_at(self, i: int) -> int | None:
        alias = self._alias_at(i)
        if alias is not None:
            key, end = alias
            self.reading.mentions.append(_Mention(keys=(key,), position=i, group=False))
            self.consume(i, end)
            return end
        origin = self._origin_at(i)
        if origin is not None:
            start = i - 1 if self._group_noun(i - 1) else i
            end = i + 2 if self._group_noun(i + 1) else i + 1
            keys = tuple(entry.key for entry in mdir.entries_of_origin(origin))
            self.reading.mentions.append(_Mention(keys=keys, position=i, group=True))
            self.consume(start, end)
            return end
        if self.word(i, ALL_WORDS) and self._group_noun(i + 1):
            keys = tuple(entry.key for entry in mdir.DIRECTORY)
            self.reading.mentions.append(_Mention(keys=keys, position=i, group=True))
            self.consume(i, i + 2)
            return i + 2
        return None

    # -- 4. verbs and clauses ---------------------------------------------------------------
    def roles(self) -> None:
        """Give every mention its role, clause by clause."""
        mode: str | None = None
        clause = 0
        clause_of: dict[int, int] = {}
        firsts: list[tuple[int, int]] = []
        by_position = {mention.position: mention for mention in self.reading.mentions}
        i = 0
        while i < len(self.tokens):
            if i in by_position:
                by_position[i].mode = mode or "set"
                clause_of[i] = clause
                i += 1
                continue
            token = self.free(i)
            step = 1
            if token is None:
                pass
            elif token.kind == "sep":
                mode, clause = None, clause + 1
            elif (width := self._phrase_at(i, BOUNDARY_PHRASES)) or self.word(i, BOUNDARY_WORDS):
                mode, clause, step = None, clause + 1, width or 1
            elif width := self._phrase_at(i, NEGATED_VERB_PHRASES):
                mode, step = "remove", width
            elif self.word(i, SET_VERBS):
                mode = "set"
            elif self.word(i, ADD_VERBS):
                mode = "add"
            elif self.word(i, REMOVE_VERBS):
                mode = "remove"
            elif self.word(i, FIRST_VERBS):
                firsts.append((i, clause))
            else:
                step = 0
            if step:
                self.consume(i, i + step)
            i += max(step, 1)
        for index, verb_clause in firsts:
            earlier = [p for p, c in clause_of.items() if c == verb_clause and p < index]
            later = [p for p in clause_of if p > index]
            target = max(earlier) if earlier else (min(later) if later else None)
            if target is not None and by_position[target].mode != "remove":
                by_position[target].first = True

    def _phrase_at(self, i: int, phrases: Iterable[tuple[str, ...]]) -> int:
        """The length of the first fixed phrase starting at `i`, or 0."""
        for words in phrases:
            if all(self.word(i + k, (word,)) for k, word in enumerate(words)):
                return len(words)
        return 0

    # -- 5. filler and what is left --------------------------------------------------------
    def leftovers(self) -> None:
        terms: list[str] = []
        for index, token in enumerate(self.tokens):
            if token.used or token.kind in ("dash", "plus", "sep"):
                continue
            if token.kind != "num" and any(form in FILLER_WORDS for form in self._forms(index)):
                token.used = True
                continue
            term = token.text[:MAX_TERM_CHARS]
            if term not in terms:
                terms.append(term)
        self.reading.unrecognized = tuple(terms[:MAX_REPORTED_TERMS])


def read_instruction(text: Any) -> Reading:
    """What one instruction states. Refuses text that is not an instruction."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INSTRUCTION_CHARS \
            or any(not (ch.isprintable() or ch in "\n\t") for ch in text):
        raise InstructionError("WORK_SCOPE_INSTRUCTION_INVALID")
    scanner = _Scanner(_tokens(text))
    scanner.year_ranges()
    scanner.batch_sizes()
    scanner.explicit_limits()
    scanner.year_bounds()
    scanner.bare_limits()
    scanner.year_resets()
    scanner.unmapped_qualifiers()
    scanner.mentions()
    scanner.roles()
    scanner.leftovers()
    reading = scanner.reading
    if not reading.recognized:
        raise InstructionError("WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD")
    return reading


@dataclass(frozen=True)
class Interpretation:
    """The complete fields an instruction produces over a plan, and its notes."""

    fields: dict[str, Any]
    notes: tuple[Note, ...]


def _ordered_unique(keys: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return seen


def apply_reading(reading: Reading, current: wsc.WorkScope | None,
                  coverage: Mapping[str, int | None] | None = None) -> Interpretation:
    """Apply one reading to the current plan (or to nothing, for a new plan).

    `coverage` maps a directory key to the number of canonical variants the
    catalog holds for it, or `None` where that cannot be stated. It is
    required exactly when `reading.needs_coverage`, and read nowhere else.
    """
    if reading.needs_coverage and coverage is None:
        raise ValueError("a 'not mapped yet' reading needs the catalog coverage")
    notes: list[Note] = []

    def admitted(mention: _Mention) -> list[str]:
        if not reading.unmapped_only or mention.mode not in ("set", "add"):
            return list(mention.keys)
        return [key for key in mention.keys
                if not (isinstance((coverage or {}).get(key), int)
                        and (coverage or {})[key] > 0)]

    kept: list[str] = []
    for mention in reading.mentions:
        kept.extend(admitted(mention))
    if reading.unmapped_only:
        named = _ordered_unique(key for m in reading.mentions if m.mode in ("set", "add")
                                for key in m.keys)
        dropped = [key for key in named if key not in kept]
        unknown = [key for key in named if key in kept and (coverage or {}).get(key) is None]
        if dropped:
            notes.append(Note("WORK_SCOPE_NOTE_ALREADY_MAPPED", units=tuple(dropped)))
        if unknown:
            notes.append(Note("WORK_SCOPE_NOTE_COVERAGE_UNKNOWN", units=tuple(unknown)))

    set_mentions = [m for m in reading.mentions if m.mode == "set"]
    replaces = any(not m.first for m in set_mentions)
    base = (_ordered_unique(key for m in set_mentions for key in admitted(m)) if replaces
            else list(current.units if current is not None else ()))
    added = _ordered_unique(key for m in reading.mentions if m.mode == "add"
                            for key in admitted(m))
    removed = _ordered_unique(key for m in reading.mentions if m.mode == "remove"
                              for key in m.keys)
    front = _ordered_unique(key for m in reading.mentions if m.first and m.mode != "remove"
                            for key in admitted(m))
    units = _ordered_unique([*base, *added])
    absent = _ordered_unique(key for m in reading.mentions if m.mode == "remove" and not m.group
                             for key in m.keys if key not in units and key not in front)
    if absent:
        notes.append(Note("WORK_SCOPE_NOTE_NOT_IN_PLAN", units=tuple(absent)))
    units = [key for key in units if key not in removed]
    front = [key for key in front if key not in removed]
    units = [*front, *[key for key in units if key not in front]]

    fields = (current.fields() if current is not None else
              {"units": [], "model_year_from": None, "model_year_to": None,
               "max_items": wsc.DEFAULT_MAX_ITEMS, "batch_size": wsc.DEFAULT_BATCH_SIZE})
    fields["units"] = units
    if reading.years_stated:
        fields["model_year_from"], fields["model_year_to"] = reading.year_from, reading.year_to
    if reading.max_items is not None:
        fields["max_items"] = reading.max_items
    elif current is None:
        notes.append(Note("WORK_SCOPE_NOTE_DEFAULT_LIMIT"))
    if reading.batch_size is not None:
        fields["batch_size"] = reading.batch_size
    if reading.unrecognized:
        notes.append(Note("WORK_SCOPE_NOTE_UNRECOGNIZED", terms=reading.unrecognized))
    return Interpretation(fields=fields, notes=tuple(notes))


__all__ = ["ALIASES", "INSTRUCTION_REASONS", "Interpretation", "InstructionError",
           "MAX_INSTRUCTION_CHARS", "MAX_REPORTED_TERMS", "MAX_TERM_CHARS", "NOTE_CODES",
           "Note", "Reading", "apply_reading", "normalize", "phrase", "read_instruction"]
