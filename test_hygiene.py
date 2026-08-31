"""Hygiene and secret leak verification test.

Ensures no private planning documents, internal scratchpad files, or raw secrets
are referenced across any public repository files in `recovery-agent/`.
"""

import unittest
from pathlib import Path


class TestRepositoryHygiene(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_dir = Path(__file__).parent.resolve()
        self.prohibited_terms = [
            "Product.md",
            "strategy-verdict",
            "openitems",
            "CLAUDE.md",
            "plan-day-",
            "razorpay-buildathon-timeline",
        ]

    def test_no_private_strategy_references(self) -> None:
        scanned_suffixes = {".py", ".md", ".html", ".json", ".toml", ".txt"}
        files_to_scan = [
            p
            for p in self.repo_dir.rglob("*")
            if p.is_file()
            and p.suffix in scanned_suffixes
            and p.name not in ("test_hygiene.py", "verify_all.py")
            and not any(part.startswith((".", "__pycache__", "venv")) for part in p.parts)
        ]

        leaks: list[str] = []
        for file_path in files_to_scan:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for term in self.prohibited_terms:
                if term in content:
                    rel_path = file_path.relative_to(self.repo_dir)
                    leaks.append(f"{rel_path}: contains forbidden reference '{term}'")

        self.assertEqual(
            len(leaks),
            0,
            f"Found {len(leaks)} prohibited internal reference(s):\n" + "\n".join(leaks),
        )


if __name__ == "__main__":
    unittest.main()
