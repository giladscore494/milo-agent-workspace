"""Metadata v3: closed schema, strict parsing, atomic private writing.

Unknown keys, duplicate keys, deprecated keys and secret-looking keys are
rejected. Values must be clean UTF-8 without control characters or NUL,
size-bounded. Files must be regular (no symlinks). Metadata is written only
after final audit success; the writer is atomic (0600 temp file, fsync,
rename) inside a private directory.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from ..model import Finding, MetadataV3, METADATA_V3_KEYS, Severity, Stage

SCHEMA_VERSION = "3"

FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "MILO_RELEASE_SHA",
        "UPSTASH_REDIS_REST_TOKEN",
        "UPSTASH_API_KEY",
        "KIMI_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "VERCEL_TOKEN",
    }
)

_SECRET_LOOKING_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PRIVATE|CREDENTIAL|APIKEY|API_KEY)", re.IGNORECASE
)

#: Allowlisted keys that contain a secret-looking substring but hold
#: non-secret resource *names* or fingerprints, never values.
_SECRET_LOOKING_ALLOWED: frozenset[str] = frozenset(
    {
        "SUPABASE_SERVICE_KEY_SECRET_NAME",
        "PROVIDER_KEY_SECRET_NAME",
        "REDIS_TOKEN_SECRET_NAME",
        "SUPABASE_URL_SECRET_NAME",
        "MILO_REDIS_TOKEN_FINGERPRINT",
    }
)

MAX_VALUE_LENGTH = 512
MAX_FILE_SIZE = 64 * 1024

_ALLOWED_KEYS = frozenset(METADATA_V3_KEYS)


def _blocked(code: str, message: str) -> Finding:
    return Finding(
        code=code,
        severity=Severity.BLOCKED,
        message=message,
        stage=Stage.METADATA_COMMITTED,
    )


def _value_problems(key: str, value: str) -> list[str]:
    problems: list[str] = []
    if "\x00" in value:
        problems.append(f"{key}: NUL byte in value")
    if any(ord(ch) < 0x20 for ch in value):
        problems.append(f"{key}: control character in value")
    if len(value) > MAX_VALUE_LENGTH:
        problems.append(f"{key}: value exceeds {MAX_VALUE_LENGTH} characters")
    try:
        value.encode("utf-8").decode("utf-8")
    except UnicodeError:
        problems.append(f"{key}: value is not clean UTF-8")
    return problems


def validate_metadata(metadata: MetadataV3) -> tuple[Finding, ...]:
    """Validate a constructed MetadataV3 object (keys are closed by type)."""

    findings: list[Finding] = []
    mapping = metadata.as_mapping()

    if mapping["MILO_METADATA_SCHEMA_VERSION"] != SCHEMA_VERSION:
        findings.append(
            _blocked(
                "METADATA_WRONG_SCHEMA_VERSION",
                "MILO_METADATA_SCHEMA_VERSION must be exactly "
                f"'{SCHEMA_VERSION}'",
            )
        )
    for key, value in mapping.items():
        if not value:
            findings.append(_blocked("METADATA_MISSING_VALUE", f"{key} is empty"))
        for problem in _value_problems(key, value):
            findings.append(_blocked("METADATA_BAD_VALUE", problem))
    return tuple(findings)


def parse_metadata_text(text: str) -> tuple[dict[str, str], tuple[Finding, ...]]:
    """Parse KEY=VALUE metadata text against the closed v3 schema."""

    findings: list[Finding] = []
    parsed: dict[str, str] = {}
    if len(text.encode("utf-8", errors="replace")) > MAX_FILE_SIZE:
        return {}, (_blocked("METADATA_TOO_LARGE", "metadata exceeds size cap"),)

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            findings.append(
                _blocked(
                    "METADATA_MALFORMED_LINE", f"line {line_number} is not KEY=VALUE"
                )
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in FORBIDDEN_KEYS:
            findings.append(
                _blocked(
                    "METADATA_FORBIDDEN_KEY",
                    f"forbidden or deprecated key {key} present",
                )
            )
            continue
        if key not in _ALLOWED_KEYS:
            findings.append(
                _blocked(
                    "METADATA_UNKNOWN_KEY",
                    f"unknown key {key} rejected by closed schema",
                )
            )
            continue
        if _SECRET_LOOKING_RE.search(key) and key not in _SECRET_LOOKING_ALLOWED:
            findings.append(
                _blocked("METADATA_SECRET_LOOKING_KEY", f"secret-looking key {key}")
            )
            continue
        if key in parsed:
            findings.append(
                _blocked("METADATA_DUPLICATE_KEY", f"duplicate key {key}")
            )
            continue
        for problem in _value_problems(key, value):
            findings.append(_blocked("METADATA_BAD_VALUE", problem))
        parsed[key] = value

    missing = _ALLOWED_KEYS - parsed.keys()
    if missing:
        findings.append(
            _blocked(
                "METADATA_MISSING_KEYS",
                "missing required keys: " + ", ".join(sorted(missing)),
            )
        )
    return parsed, tuple(findings)


def read_metadata_file(path: Path) -> tuple[dict[str, str], tuple[Finding, ...]]:
    """Strict file read: regular file only, no symlink, size-capped."""

    try:
        st = os.lstat(path)
    except OSError as exc:
        return {}, (_blocked("METADATA_UNREADABLE", f"cannot stat metadata: {exc.strerror}"),)
    if stat.S_ISLNK(st.st_mode):
        return {}, (_blocked("METADATA_SYMLINK", "metadata file is a symlink"),)
    if not stat.S_ISREG(st.st_mode):
        return {}, (_blocked("METADATA_NOT_REGULAR", "metadata file is not a regular file"),)
    if st.st_size > MAX_FILE_SIZE:
        return {}, (_blocked("METADATA_TOO_LARGE", "metadata exceeds size cap"),)
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeError:
        return {}, (_blocked("METADATA_BAD_UTF8", "metadata is not valid UTF-8"),)
    return parse_metadata_text(text)


def render_metadata(metadata: MetadataV3) -> str:
    lines = [f"{key}={value}" for key, value in metadata.as_mapping().items()]
    return "\n".join(lines) + "\n"


def write_metadata_atomically(metadata: MetadataV3, output_dir: Path) -> Path:
    """Write metadata as a 0600 file via fsynced temp file + atomic rename.

    The output directory is created private (0700). Callers must only invoke
    this after final audit success; on any failure the temporary candidate
    is removed and no artifact remains.
    """

    findings = validate_metadata(metadata)
    if findings:
        raise ValueError(
            "refusing to write invalid metadata: "
            + "; ".join(f.message for f in findings)
        )

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    final_path = output_dir / "bootstrap-metadata-v3.env"

    fd, tmp_name = tempfile.mkstemp(dir=output_dir, prefix=".metadata-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_metadata(metadata))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    dir_fd = os.open(output_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final_path
