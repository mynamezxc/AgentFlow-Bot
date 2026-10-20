#!/usr/bin/env python3
"""Pre-push safety check (stdlib only): refuses to publish secrets or private runtime data.

    python scripts/check_publish.py            # scan what git tracks or would add (else the working tree minus .gitignore)
    python scripts/check_publish.py --root DIR # scan another checkout

Exit code 0 = clean, 1 = findings (a list of "path:line: kind"; secret values are never printed in full).
A line that carries the marker `publish-ok` is skipped (for documented placeholders).
"""
import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000
MARKER = "publish-ok"

# Data that must never be tracked: (description, matcher on the posix path relative to the root).
FORBIDDEN_DIRS = (".agentflow", "workspaces", "artifacts", ".openai", ".codex", ".claude")
FORBIDDEN_NAMES = ("*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.sqlite3-journal", "*.sqlite", "*.db", "credentials.bin", "*.pem", "*.key", "*.p12", "*.pfx")
SECRET_ENV_NAMES = ("GATEWAY_API_KEY", "FACEBOOK_PAGE_TOKEN", "FACEBOOK_APP_SECRET", "META_VERIFY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
                    "SEO_WEBHOOK_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "SLACK_BOT_TOKEN")
PLACEHOLDER = re.compile(r"^(?:\.{2,}|x+|\*+|<.*|\$\{?.*|your[-_].*|example.*|changeme|change-me|placeholder.*|fake.*|dummy.*|test.*|none|null|todo|redacted|secret)$", re.I)

SECRET_PATTERNS = (
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("OpenAI/Anthropic style key", re.compile(r"\bsk-(?:proj-|ant-|live-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("PEM private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Authorization header with a value", re.compile(r"Authorization[\"']?\s*[:=]\s*[\"']?(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
)
ENV_LINE = re.compile(r"^\s*(?:export\s+)?(%s)\s*[=:]\s*[\"']?([^\s\"'#]+)" % "|".join(SECRET_ENV_NAMES))


def redact(text):
    text = str(text)
    return text[:4] + "..." + "(%d chars)" % len(text) if len(text) > 8 else "***"


def git(root, *args):
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def parse_ignore(root):
    rules = []
    try:
        lines = (Path(root) / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rules
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def ignored(path, rules):
    """Small .gitignore matcher for the fallback walk: last matching rule wins; supports !, /anchor, dir/ and globs."""
    result = False
    parts = path.split("/")
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        directory = pattern.endswith("/")
        pattern = pattern.strip("/")
        if anchored:
            candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
            hit = any(fnmatch.fnmatchcase(c, pattern) for c in (candidates[:-1] if directory else candidates))
        else:
            names = parts[:-1] if directory else parts
            hit = any(fnmatch.fnmatchcase(n, pattern) for n in names)
        if hit:
            result = not negate
    return result


def listing(root, use_git=True):
    """Relative posix paths that would be published: tracked + untracked-but-not-ignored files, else the working tree minus .gitignore."""
    root = Path(root)
    out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard") if use_git and (root / ".git").exists() else None
    if out is not None:
        names = [n for n in out.split("\0") if n]
        return sorted({n for n in names if (root / n).is_file()})
    rules, names = parse_ignore(root), []
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if ".git/" not in "/" + relative and not ignored(relative, rules):
                names.append(relative)
    return sorted(names)


def forbidden(relative):
    parts = relative.split("/")
    name = parts[-1]
    if parts[0] in FORBIDDEN_DIRS or ".agentflow" in parts[:-1]:
        return "private runtime data must not be published (%s/)" % parts[0]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "environment file with secrets"
    if re.fullmatch(r"premium-[^/]*\.json", name) and len(parts) == 1:
        return "local tooling output"
    for pattern in FORBIDDEN_NAMES:
        if fnmatch.fnmatchcase(name.lower(), pattern):
            return "data or key file (%s)" % pattern
    return None


def scan_text(relative, text):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if MARKER in line:
            continue
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append((relative, number, "%s: %s" % (kind, redact(match.group(0)))))
        match = ENV_LINE.match(line)
        if match and not PLACEHOLDER.match(match.group(2)):
            findings.append((relative, number, "%s has a value: %s" % (match.group(1), redact(match.group(2)))))
    return findings


def check(root=ROOT, files=None, use_git=True):
    """Return [(path, line or 0, message)] for everything that must not be published."""
    root = Path(root)
    findings = []
    for relative in (files if files is not None else listing(root, use_git)):
        reason = forbidden(relative)
        if reason:
            findings.append((relative, 0, reason))
            continue
        path = root / relative
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        findings.extend(scan_text(relative, data.decode("utf-8", "replace")))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that nothing secret or private is about to be published.")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)
    findings = check(args.root)
    if findings:
        print("NOT safe to publish: %d finding(s)" % len(findings))
        for relative, number, message in findings:
            print("  %s%s: %s" % (relative, ":%d" % number if number else "", message))
        print("Fix: remove the secret, or untrack private files, e.g. git rm -r --cached workspaces .openai premium-audit.json premium-ui.json")
        return 1
    print("OK: nothing secret or private found in %d files." % len(listing(args.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
