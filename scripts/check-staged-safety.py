#!/usr/bin/env python3
"""Heuristic staged-source review. Prints finding locations, never matched values.

This is a safety aid, not a guarantee that all secrets or personal data are absent.
It reads the Git index so unstaged changes cannot hide what would be committed.
"""
from pathlib import PurePosixPath
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args])


PATTERNS = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "github-token": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "api-key": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
    "aws-access-key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "deployment-project": r"appgprj_[A-Za-z0-9_]{16,}",
    "slack-token": r"\bxox[baprs]-[A-Za-z0-9-]{20,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}",
    "credential-in-url": r"https?://[^\s/@\"']+:[^\s/@\"']+@[^\s/\"']+",
    "email-review": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "literal-secret-review": r'''(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\s*[=:]\s*["'][A-Za-z0-9_+/=-]{16,}["']''',
}

# Exact synthetic fixtures and upstream local-development identity only.
ALLOWED = {
    ("python/tests/test_core.py", "credential-in-url", "https://name:secret@example.com"),
    ("python/tests/test_core.py", "email-review", "secret@example.com"),
    ("web/tests/engine.test.mjs", "credential-in-url", "https://u:p@x.test"),
    ("web/tests/engine.test.mjs", "email-review", "p@x.test"),
    ("web/tests/engine.test.mjs", "email-review", "owner@example.com"),
    ("web/build/sites-vite-plugin.ts", "email-review", "seedy@sites.test"),
}

FORBIDDEN_PARTS = {
    "node_modules", ".git", ".wrangler", ".sites-runtime", ".npm-cache",
    ".next", ".vinext", "dist", "__pycache__", ".venv", "venv", ".aws",
    ".agents", ".codex", "artifacts",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".apk", ".pyc", ".log", ".zip", ".tsbuildinfo"}


def main():
    entries = [entry for entry in git("ls-files", "--stage", "-z").split(b"\0") if entry]
    if not entries:
        print("No staged/tracked files found; run git add before this check.")
        return 2
    findings = []
    reviewed_fixtures = 0
    for entry in entries:
        meta, raw_name = entry.split(b"\t", 1)
        mode, _oid, stage = meta.decode().split()
        name = raw_name.decode()
        path = PurePosixPath(name)
        if mode == "120000" or stage != "0":
            findings.append((name, 0, "symlink-or-unmerged-index-entry"))
        if (any(part in FORBIDDEN_PARTS for part in path.parts)
                or path.suffix in FORBIDDEN_SUFFIXES
                or path.name.startswith(".env") and path.name != ".env.example"
                or name.startswith("python/data/")
                or re.search(r"\.(?:db|sqlite3?)(?:-wal|-shm|-journal)$", name)):
            findings.append((name, 0, "excluded-runtime-or-sensitive-file"))
        blob = git("show", f":{name}")
        if b"\0" in blob:
            findings.append((name, 0, "unexpected-binary-review"))
            continue
        text = blob.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                # This scanner contains its own exact reviewed allowlist literals.
                if name == "scripts/check-staged-safety.py":
                    if any(value == match.group() for _, _, value in ALLOWED):
                        continue
                if (name, label, match.group()) in ALLOWED:
                    reviewed_fixtures += 1
                    continue
                findings.append((name, text.count("\n", 0, match.start()) + 1, label))
    if findings:
        for name, line, label in findings:
            print(f"{name}:{line}: {label}: [REDACTED]")
        print(f"REVIEW REQUIRED: {len(findings)} findings in {len(entries)} files.")
        return 1
    print(f"PASS: {len(entries)} index files; no unreviewed findings; {reviewed_fixtures} exact synthetic fixtures allowed.")
    print("Heuristic review only. Inspect diffs and data provenance before publication.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
