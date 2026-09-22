"""The reviewed manufacturer DIRECTORY a work scope selects from.

What a directory entry is
-------------------------

One marque a person can ask MILO to map, named four ways, each for a different
reader:

*   ``key`` -- the ASCII identity a work scope STORES. It is what the digest
    covers, what a browser sends back and what a later batch is bound to, so
    it is short, stable and never localized;
*   ``name`` / ``name_he`` -- how the workspace SHOWS the marque. Display text
    only: nothing on an execution path reads either;
*   ``aliases`` -- the spellings the deterministic instruction reader
    (`interpret.py`) RECOGNIZES in a typed request. Recognition only: an alias
    maps a person's words onto a ``key`` and never reaches a Government query;
*   ``register_marque`` -- the exact `tozar` text the Israeli vehicle register
    itself writes for the marque. It is the ONLY value a scoped Government read
    may filter on, and it is set only where committed register evidence shows
    that spelling. Everywhere else it is ``None``.

Why ``register_marque`` is almost always absent
-----------------------------------------------

The register publishes no code for the marque (`vocabulary.py` says so), so a
filter on it is an exact text match, and a guessed spelling is a silent empty
read. The committed capture this repository reviewed -- the R5 `q=RAV4` pages,
233 rows -- states exactly one marque, `טויוטה`, on every row. That is the only
register spelling there is evidence for, so it is the only one recorded. The
Hebrew display names below are how Israeli readers commonly write the other
marques; they are deliberately NOT promoted to register spellings, because
"commonly written" is not "what the register states". A scope may still name
an unverified marque -- the plan is the user's intent -- and the gap is stated
on every read (`register_marque_verified`), so preparation can refuse it
explicitly instead of reading nothing.

Extending this table is a reviewed edit: a new register spelling needs source
evidence, and any change to an entry's meaning changes ``DIRECTORY_VERSION``,
which every stored work scope records. A scope validated against an older
directory is then visibly not current rather than silently reinterpreted.

Pure module: constants and lookups. No I/O, no clock, no environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: The version of this table. Recorded inside every work scope, so the digest
#: of a plan commits to the directory its keys were validated against.
DIRECTORY_VERSION = "milo-manufacturer-directory/1"

#: The closed origin vocabulary: where a marque comes from, as a person groups
#: them ("Japanese manufacturers"). Brand origin, not assembly plant -- the
#: register's own `tozeret_nm` states the plant ("טויוטה קנדה") and is not
#: what a person means by a Japanese manufacturer.
ORIGIN_LABELS: Mapping[str, str] = {
    "china": "China",
    "czechia": "Czechia",
    "france": "France",
    "germany": "Germany",
    "italy": "Italy",
    "japan": "Japan",
    "romania": "Romania",
    "south_korea": "South Korea",
    "spain": "Spain",
    "sweden": "Sweden",
    "uk": "United Kingdom",
    "usa": "United States",
}

#: The register spellings there is committed evidence for, and nothing else.
#:
#: `טויוטה`: every one of the 233 rows of the R5 capture
#: (`backend/testing/r5_proof/fixtures/government/wltp_page_00000{1,2,3}.json`,
#: `q=RAV4` over the pinned WLTP resource) states `tozar` exactly so, across
#: four assembly plants.
VERIFIED_REGISTER_MARQUES: Mapping[str, str] = {"toyota": "טויוטה"}


@dataclass(frozen=True)
class DirectoryEntry:
    """One marque a work scope may name. See the module docstring."""

    key: str
    name: str
    name_he: str
    origin: str
    aliases: tuple[str, ...]

    @property
    def register_marque(self) -> str | None:
        return VERIFIED_REGISTER_MARQUES.get(self.key)

    @property
    def register_marque_verified(self) -> bool:
        return self.register_marque is not None


def _entry(key: str, name: str, name_he: str, origin: str, *aliases: str) -> DirectoryEntry:
    return DirectoryEntry(key=key, name=name, name_he=name_he, origin=origin,
                          aliases=(name, name_he, *aliases))


#: The directory, in the order it is shown and in which a group ("Japanese
#: manufacturers") expands: alphabetical by key, so the order is a property of
#: the table rather than of whoever last edited it.
DIRECTORY: tuple[DirectoryEntry, ...] = (
    _entry("alfa_romeo", "Alfa Romeo", "אלפא רומיאו", "italy", "alfa", "אלפא רומאו"),
    _entry("audi", "Audi", "אאודי", "germany", "אודי"),
    _entry("bmw", "BMW", "ב.מ.וו", "germany", "במוו"),
    _entry("byd", "BYD", "בי.וואי.די", "china"),
    _entry("chery", "Chery", "צ'רי", "china"),
    _entry("chevrolet", "Chevrolet", "שברולט", "usa", "chevy"),
    _entry("citroen", "Citroën", "סיטרואן", "france", "citroen"),
    _entry("cupra", "Cupra", "קופרה", "spain"),
    _entry("dacia", "Dacia", "דאצ'יה", "romania", "דאציה"),
    _entry("ds", "DS", "די.אס", "france", "ds automobiles"),
    _entry("fiat", "Fiat", "פיאט", "italy"),
    _entry("ford", "Ford", "פורד", "usa"),
    _entry("geely", "Geely", "ג'ילי", "china"),
    _entry("genesis", "Genesis", "ג'נסיס", "south_korea"),
    _entry("honda", "Honda", "הונדה", "japan"),
    _entry("hyundai", "Hyundai", "יונדאי", "south_korea", "יונדיי"),
    _entry("isuzu", "Isuzu", "איסוזו", "japan"),
    _entry("jaguar", "Jaguar", "יגואר", "uk"),
    _entry("jeep", "Jeep", "ג'יפ", "usa"),
    _entry("kia", "Kia", "קיה", "south_korea"),
    _entry("land_rover", "Land Rover", "לנד רובר", "uk", "landrover"),
    _entry("lexus", "Lexus", "לקסוס", "japan"),
    _entry("mazda", "Mazda", "מאזדה", "japan", "מזדה"),
    _entry("mercedes_benz", "Mercedes-Benz", "מרצדס-בנץ", "germany",
           "mercedes benz", "mercedes", "benz", "מרצדס", "מרצדס בנץ"),
    _entry("mitsubishi", "Mitsubishi", "מיצובישי", "japan"),
    _entry("nissan", "Nissan", "ניסאן", "japan", "ניסן"),
    _entry("opel", "Opel", "אופל", "germany"),
    _entry("peugeot", "Peugeot", "פיג'ו", "france", "פיגו", "פז'ו"),
    _entry("porsche", "Porsche", "פורשה", "germany"),
    _entry("renault", "Renault", "רנו", "france"),
    _entry("seat", "SEAT", "סיאט", "spain"),
    _entry("skoda", "Škoda", "סקודה", "czechia", "skoda"),
    _entry("ssangyong", "SsangYong", "סאנגיונג", "south_korea", "ssang yong", "kgm",
           "סאנג יונג"),
    _entry("subaru", "Subaru", "סובארו", "japan"),
    _entry("suzuki", "Suzuki", "סוזוקי", "japan"),
    _entry("tesla", "Tesla", "טסלה", "usa"),
    _entry("toyota", "Toyota", "טויוטה", "japan"),
    _entry("volkswagen", "Volkswagen", "פולקסווגן", "germany", "vw", "פולקסוואגן"),
    _entry("volvo", "Volvo", "וולוו", "sweden"),
)

#: Lookup by key. Built once from the tuple above, never edited separately.
DIRECTORY_BY_KEY: Mapping[str, DirectoryEntry] = {entry.key: entry for entry in DIRECTORY}

#: The most units one scope may name: every entry once, and never more.
MAX_DIRECTORY_ENTRIES = len(DIRECTORY)


def entry_for(key: object) -> DirectoryEntry | None:
    """The entry an exact key names, or None. No case folding, no trimming:
    a key is an identity, and a near miss is not the same identity."""
    return DIRECTORY_BY_KEY.get(key) if isinstance(key, str) else None


def entries_of_origin(origin: str) -> tuple[DirectoryEntry, ...]:
    """Every entry of one origin, in directory order."""
    return tuple(entry for entry in DIRECTORY if entry.origin == origin)


def verified_register_marques() -> tuple[tuple[str, str], ...]:
    """`(key, register marque)` for every verified entry, in directory order."""
    return tuple((entry.key, entry.register_marque) for entry in DIRECTORY
                 if entry.register_marque is not None)


def _check_table() -> None:
    """Refuse to import a directory that contradicts itself.

    Every key is ASCII and unique, every origin is in the closed vocabulary,
    and every verified marque belongs to a real entry. A broken table is an
    import error, not a wrong answer at request time.
    """
    keys = [entry.key for entry in DIRECTORY]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise RuntimeError("the manufacturer directory keys must be unique and sorted")
    for entry in DIRECTORY:
        if not entry.key.isascii() or not entry.key.replace("_", "").isalnum() \
                or entry.key != entry.key.lower():
            raise RuntimeError("a manufacturer directory key must be lowercase ASCII")
        if entry.origin not in ORIGIN_LABELS:
            raise RuntimeError("a manufacturer directory origin must be in the closed vocabulary")
    if not set(VERIFIED_REGISTER_MARQUES) <= set(keys):
        raise RuntimeError("a verified register marque must belong to a directory entry")


_check_table()


__all__ = ["DIRECTORY", "DIRECTORY_BY_KEY", "DIRECTORY_VERSION", "DirectoryEntry",
           "MAX_DIRECTORY_ENTRIES", "ORIGIN_LABELS", "VERIFIED_REGISTER_MARQUES",
           "entries_of_origin", "entry_for", "verified_register_marques"]
