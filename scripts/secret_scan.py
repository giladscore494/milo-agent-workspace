import re
from pathlib import Path

patterns = [re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"service_role[a-zA-Z0-9_.-]{20,}", re.I), re.compile(r"-----BEGIN PRIVATE KEY-----")]
skip = {".git", "node_modules", "legacy", "__pycache__", ".next", ".next-e2e-disabled", ".next-e2e-enabled", "test-results", "playwright-report"}
findings = []
for path in Path(".").rglob("*"):
    if not path.is_file() or any(part in skip for part in path.parts) or path == Path("scripts/secret_scan.py"):
        continue
    try:
        text = path.read_text(errors="ignore")
    except UnicodeDecodeError:
        continue
    if any(pattern.search(text) for pattern in patterns):
        findings.append(str(path))
if findings:
    raise SystemExit("potential secrets found: " + ", ".join(findings))
print("secret scan passed")
