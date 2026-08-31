"""Repository leak scanner: one implementation, two callers.

`test_hygiene.py` and `verify_all.py` Gate 5 both enforce the same property — that no
private planning document is named anywhere in this public repository. They previously
carried byte-identical copies of the term list, the suffix set and the ignored-directory
set, so the two could drift and the submission would pass one check while failing the
other. The constants live here and both callers read them.

Deliberately imports nothing from `app.config`: this must run before credentials are
provisioned, since a missing key is exactly the state a fresh clone is in.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Private planning documents that live in the parent repository and must never be named
# here. Naming one leaks its existence even when it leaks no content.
PROHIBITED_TERMS = (
    "Product.md",
    "strategy-verdict",
    "openitems",
    "CLAUDE.md",
    "plan-day-",
    "razorpay-buildathon-timeline",
)

# Dotfiles are scanned by name rather than by suffix. The historical leak was a .gitignore
# listing every planning document, and an extensionless dot-prefixed file is precisely what
# a .gitignore is, so a suffix-only filter is blind to the one file class that has actually
# leaked here before.
SCANNED_SUFFIXES = frozenset({".py", ".md", ".html", ".json", ".toml", ".txt", ".example", ""})

IGNORED_DIRS = frozenset(
    {".git", ".pytest_cache", ".ruff_cache", ".venv", "venv", "__pycache__", ".worktrees"}
)

# The three files that state the prohibited terms in order to search for them. Excluded by
# name, listed once, so adding a fourth cannot be done in one scanner and forgotten in the
# other.
SELF_EXCLUDED = frozenset({"hygiene.py", "test_hygiene.py", "verify_all.py"})


def files_to_scan(repo_root: Path | None = None) -> list[Path]:
    """Every file in the repository whose contents are subject to the leak scan."""
    root = repo_root or REPO_ROOT
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and (p.suffix in SCANNED_SUFFIXES or p.name.startswith("."))
        and p.name not in SELF_EXCLUDED
        and not any(part in IGNORED_DIRS for part in p.parts[:-1])
    ]


def scan_for_private_references(repo_root: Path | None = None) -> list[str]:
    """Return one human-readable line per leaked reference. Empty means clean."""
    root = repo_root or REPO_ROOT
    leaks: list[str] = []
    for file_path in files_to_scan(root):
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        for term in PROHIBITED_TERMS:
            # `.gitignore` naming CLAUDE.md is the one intended reference: Claude Code
            # creates that file in place, so it has to be ignored rather than merely
            # absent. Every other term is still a leak in that file.
            if file_path.name == ".gitignore" and term == "CLAUDE.md":
                continue
            if term in content:
                leaks.append(f"{file_path.relative_to(root)}: contains reference to '{term}'")
    return leaks
