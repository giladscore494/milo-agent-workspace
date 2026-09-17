"""Repository secret scan.

Heuristic, and deliberately strict: a match is a finding unless it can be
PROVEN to be something else. The one proof accepted is that the matched text
is the tail of a real filename in this repository — a path reference is not
credential material, and no credential is also a file on disk. That carve-out
exists because migration filenames legitimately contain `service_role`
(`20260706192500_grant_service_role_schema_privileges.sql`), and documentation
that names the file exactly — as docs/production-readiness/MIGRATIONS.md must,
since its strict apply order is machine-checked against the directory — would
otherwise be reported as a leaked key.
"""

import re
from pathlib import Path

patterns = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"service_role[a-zA-Z0-9_.-]{20,}", re.I),
    re.compile(r"-----BEGIN PRIVATE KEY-----"),
]
skip = {".git", "node_modules", "legacy", "__pycache__", ".next", ".next-e2e-disabled", ".next-e2e-enabled", "test-results", "playwright-report"}


def scanned_files(root="."):
    for path in Path(root).rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        if path == Path("scripts/secret_scan.py"):
            continue
        yield path


def filename_tails(files):
    """Every suffix-matchable filename in the repository."""
    return {path.name for path in files}


def is_filename_reference(candidate, names):
    """True when `candidate` is the tail of a real repository filename."""
    return any(name.endswith(candidate) for name in names)


def findings_for(text, names):
    found = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if is_filename_reference(match.group(0), names):
                continue
            found.append(match.group(0))
    return found


def main():
    files = list(scanned_files())
    names = filename_tails(files)
    findings = []
    for path in files:
        try:
            text = path.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        if findings_for(text, names):
            findings.append(str(path))
    if findings:
        raise SystemExit("potential secrets found: " + ", ".join(findings))
    print("secret scan passed")


if __name__ == "__main__":
    main()
