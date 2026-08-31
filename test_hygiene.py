"""Hygiene and secret leak verification test.

Ensures no private planning documents or internal scratchpad files are referenced across
any public repository file. The scan itself lives in `app/hygiene.py` so this test and
`verify_all.py` Gate 5 cannot enforce different rules.
"""

import unittest

from app.hygiene import scan_for_private_references


class TestRepositoryHygiene(unittest.TestCase):
    def test_no_private_strategy_references(self) -> None:
        leaks = scan_for_private_references()
        self.assertEqual(
            len(leaks),
            0,
            f"Found {len(leaks)} prohibited internal reference(s):\n" + "\n".join(leaks),
        )


if __name__ == "__main__":
    unittest.main()
