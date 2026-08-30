"""Automated Submission Verification Suite.

Executes all 5 pre-submission verification gates:
1. Unit Test Suite (100% pass rate)
2. Code formatting and linting via ruff
3. Ledger byte-fingerprint stability check (seed 42)
4. Benchmark experiment execution (run_experiment.py)
5. Sensitive data & private planning document leak scanning
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    print("=" * 80)
    print("  RUNNING 5-GATE SUBMISSION VERIFICATION SUITE")
    print("=" * 80)

    repo_root = Path(__file__).parent.resolve()

    # Gate 1: Unit Tests
    print("\n[Gate 1/5] Running unit test suite (pytest / unittest discover)...")
    res = subprocess.run([sys.executable, "-m", "pytest", "-v"], cwd=repo_root)
    if res.returncode != 0:
        print("\nFAIL: Unit tests failed!")
        sys.exit(1)
    print("PASS: All unit tests passed.")

    # Gate 2: Ruff Linting
    print("\n[Gate 2/5] Running ruff lint check...")
    res = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=repo_root)
    if res.returncode != 0:
        print("\nFAIL: Ruff lint issues detected!")
        sys.exit(1)
    print("PASS: Ruff clean with zero lint warnings.")

    # Gate 3: Ledger Fingerprint
    print("\n[Gate 3/5] Verifying synthetic ledger fingerprint (seed 42)...")
    try:
        from app.ledger import LEDGER_FINGERPRINT, fingerprint, generate

        ledger = generate(seed=42)
        fp = fingerprint(ledger)
        if fp != LEDGER_FINGERPRINT:
            print(
                f"\nFAIL: Ledger fingerprint mismatch! Expected {LEDGER_FINGERPRINT}, got {fp}"
            )
            sys.exit(1)
        print(f"PASS: Ledger fingerprint verified ({fp}).")
    except Exception as e:
        print(f"\nFAIL: Error generating ledger: {e}")
        sys.exit(1)

    # Gate 4: Benchmark Reproducibility
    print("\n[Gate 4/5] Verifying benchmark reproducibility (run_experiment.py)...")
    res = subprocess.run(
        [sys.executable, "run_experiment.py", "--seed", "42"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"\nFAIL: Benchmark experiment execution failed:\n{res.stderr}")
        sys.exit(1)
    print("PASS: Benchmark experiment executed successfully.")

    # Gate 5: Leak Detection
    print("\n[Gate 5/5] Scanning for private strategy document leaks & secrets...")
    prohibited = [
        "Product.md",
        "strategy-verdict",
        "openitems",
        "CLAUDE.md",
        "plan-day-",
        "razorpay-buildathon-timeline",
    ]
    leaks = []
    scanned_suffixes = {".py", ".md", ".html", ".json", ".toml", ".txt"}
    for p in repo_root.rglob("*"):
        if (
            p.is_file()
            and p.suffix in scanned_suffixes
            and p.name not in ("verify_all.py", "test_hygiene.py")
            and not any(part.startswith((".", "__pycache__", "venv")) for part in p.parts)
        ):
            text = p.read_text(encoding="utf-8", errors="ignore")
            for term in prohibited:
                if term in text:
                    leaks.append(f"{p.relative_to(repo_root)}: contains reference to '{term}'")

    if leaks:
        print("\nFAIL: Private document reference leaks detected:")
        for leak in leaks:
            print(f"  - {leak}")
        sys.exit(1)
    print("PASS: Zero private strategy leaks detected.")

    print("\n" + "=" * 80)
    print("  ALL 5 GATES PASSED! SUBMISSION REPOSITORY IS FREEZE-READY.")
    print("=" * 80)


if __name__ == "__main__":
    main()
