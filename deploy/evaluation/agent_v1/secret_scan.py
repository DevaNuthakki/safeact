"""Scan committed evaluation files for anything shaped like a secret.

Flags, anywhere in a file:
- OpenAI-style API keys (``sk-`` followed by 16+ key characters),
- SafeAct agent keys (``sak_`` followed by 16+ hex characters),
- Stripe keys (``sk_``/``rk_``/``pk_`` + ``test_``/``live_``) and webhook
  secrets (``whsec_``),
- 64-hex tokens (the shape of a session secret or CSRF token).

A 64-hex value is allowed only as a content-addressed digest in a named
digest field: in JSON or JSONL, a string value whose key ends in
``sha256``, ``digest`` or ``_hash``; in any other text, on a line that
names such a field. The allowance is by field name, never by pattern,
so a secret pasted anywhere else is caught.

Sealed evidence written before that convention cannot be edited, so it
has an exact allowlist: one repository path and the precise digests it
may contain (SEALED_DIGESTS). The same values anywhere else, or any
other value in that file, are still flagged.
"""

import json
import re
import sys

from pathlib import Path


# A key starts at a token boundary: "task-8afca4cf-retry-1" contains
# "sk-..." but is an intent reference, not a key.
OPENAI_KEY = re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{16,}")
AGENT_KEY = re.compile(r"(?<![A-Za-z0-9_\-])sak_[0-9a-fA-F]{16,}")
# Stripe API keys (secret, restricted, publishable; test and live) and
# webhook signing secrets.
STRIPE_KEY = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:(?:sk|rk|pk)_(?:test|live)_|whsec_)[A-Za-z0-9]{16,}"
)
HEX64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
DIGEST_FIELD = re.compile(r"(sha256|digest|_hash)\"?\s*[:=]", re.IGNORECASE)
DIGEST_KEY = re.compile(r"(sha256|digest|_hash)$", re.IGNORECASE)
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".csv", ".py", ".log"}

ROOT = Path(__file__).resolve().parents[3]
# Sealed M5.8 Stripe evidence review (commit 3269385): artifact and runner
# digests on lines that do not name a digest field.
SEALED_DIGESTS = {
    "docs/phase-d/stripe-sandbox-v1/REVIEW-M58.md": frozenset({
        "fb7160a5f2314fe8a8912477c791380a3352811ceabba64dca452b2c5bdfa29f",  # sha256: runner sorted JSON
        "3000f1cd89f06a9d5ba69aead4a04190a91f209fc3d061ed882e41e149598922",  # sha256: runner source
        "62740d9feb2d7611dbd9738c06d606526af4890b819e0d6774fb285c92a4495d",  # sha256: summary.json
        "76e88dd73ccf18f5557f9b044c935760cf2fca67477408475c57947aeb63e970",  # sha256: results.jsonl
        "c1e65a59bc153cd584ad723330d04d22938b81c4cdcbb7aaeb3cc4f4cfa1b8b3",  # sha256: calls.jsonl
    }),
}


def _sealed_digests(path):
    try:
        relative = Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return frozenset()
    return SEALED_DIGESTS.get(relative, frozenset())


def _allowed_json_values(value, key=None, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            _allowed_json_values(v, str(k), out)
    elif isinstance(value, list):
        for v in value:
            _allowed_json_values(v, key, out)
    elif isinstance(value, str) and key and DIGEST_KEY.search(key):
        if HEX64.fullmatch(value):
            out.add(value)
    return out


def scan_text(text, *, suffix="", sealed=frozenset()):
    findings = []
    allowed = set(sealed)
    if suffix in (".json", ".jsonl"):
        documents = [text] if suffix == ".json" else text.splitlines()
        for document in documents:
            try:
                allowed |= _allowed_json_values(json.loads(document))
            except ValueError:
                pass
    for number, line in enumerate(text.splitlines(), 1):
        for pattern, kind in ((OPENAI_KEY, "openai_key"),
                              (AGENT_KEY, "agent_key"),
                              (STRIPE_KEY, "stripe_key")):
            if pattern.search(line):
                findings.append((number, kind))
        for match in HEX64.finditer(line):
            if match.group(0) in allowed:
                continue
            if suffix not in (".json", ".jsonl") and DIGEST_FIELD.search(line):
                continue
            findings.append((number, "hex64_secret_shape"))
    return findings


def files_scanned(paths):
    """Every file the scan reads under the given paths (console.log included)."""

    found = []
    for root in map(Path, paths):
        candidates = [root] if root.is_file() else sorted(
            p for p in root.rglob("*") if p.is_file())
        found += [p for p in candidates
                  if p.suffix in TEXT_SUFFIXES and "__pycache__" not in p.parts]
    return found


def scan(paths):
    """{file: [(line, kind)]} for every file under the given paths."""

    results = {}
    for path in files_scanned(paths):
        found = scan_text(path.read_text("utf-8", errors="replace"),
                          suffix=path.suffix, sealed=_sealed_digests(path))
        if found:
            results[str(path)] = found
    return results


def main(argv=None):
    paths = argv if argv is not None else sys.argv[1:]
    results = scan(paths)
    for path, found in results.items():
        for number, kind in found:
            print(f"{path}:{number}: {kind}")
    return 1 if results else 0


if __name__ == "__main__":
    sys.exit(main())
