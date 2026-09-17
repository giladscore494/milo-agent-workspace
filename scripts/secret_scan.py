"""Repository secret scan.

Heuristic and deliberately strict: a match is a finding. There is no
allowlist, and in particular nothing in this repository is exempt because of
what a file is NAMED — filenames are contributor-controlled input, so a
filename-derived exemption is a way for anyone who can add a file to switch
off detection for text elsewhere in the tree.

The one false positive this scanner ever had came from the `service_role`
heuristic firing on a legitimate migration filename
(`20260706192500_grant_service_role_schema_privileges.sql`), which
docs/production-readiness/MIGRATIONS.md must name exactly because its strict
apply order is machine-checked against the directory. That is fixed by
making the pattern describe credential material more precisely rather than by
excusing a file: key material is high-entropy and contains a long UNBROKEN
alphanumeric run, while a snake_case identifier or filename is words joined
by `_`, `-` or `.`.

    service_role_schema_privileges.sql   longest run: "privileges" (10) -> not a secret
    service_role_eyJhbGciOiJIUzI1Ni...   longest run: 36+             -> a secret

The refinement only narrows what `service_role` matches; it does not relax
the other patterns, and it still fires on a credential that merely happens to
be spelled like a filename.
"""

import re
import sys
from pathlib import Path

# Credential material contains a long unbroken alphanumeric run. Separators
# (`_`, `-`, `.`) are what make an identifier an identifier.
CREDENTIAL_RUN = re.compile(r"[A-Za-z0-9]{20,}")

# (pattern, requires_credential_run)
#
# `requires_credential_run` is set only where the marker text also occurs in
# ordinary identifiers. `sk-` and the PEM header do not, so they stay exact.
PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), False),
    (re.compile(r"service_role[a-zA-Z0-9_.-]{20,}", re.I), True),
    (re.compile(r"-----BEGIN PRIVATE KEY-----"), False),
]

# Generated, gitignored build and test caches. These are not repository
# content: `.pytest_cache` in particular records test node IDs, so a test that
# deliberately names credential-shaped fixtures would otherwise make the scan
# fail on its own cache rather than on anything a contributor wrote.
SKIP = {
    ".git", "node_modules", "legacy", "__pycache__", ".next",
    ".next-e2e-disabled", ".next-e2e-enabled", "test-results", "playwright-report",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
}

SELF = Path(__file__).resolve()


def scanned_files(root="."):
    """Every file this scan covers, under `root`."""
    for path in Path(root).rglob("*"):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        if path.resolve() == SELF:
            continue
        yield path


def findings_for(text):
    """Credential-shaped matches in `text`. Nothing is exempt by filename."""
    found = []
    for pattern, requires_run in PATTERNS:
        for match in pattern.finditer(text):
            if requires_run and not CREDENTIAL_RUN.search(match.group(0)):
                continue
            found.append(match.group(0))
    return found


def scan(root="."):
    """Paths under `root` that contain credential-shaped text."""
    findings = []
    for path in scanned_files(root):
        try:
            text = path.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        if findings_for(text):
            findings.append(str(path))
    return findings


def main():
    findings = scan(".")
    if findings:
        raise SystemExit("potential secrets found: " + ", ".join(findings))
    print("secret scan passed")


if __name__ == "__main__":
    sys.exit(main())
