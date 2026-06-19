#!/usr/bin/env python3
"""
wa-claude: adversarial prompt tester for LLM-powered endpoints.
Named after Wario. Evil Claude.
"""

import argparse
import sys
from wa_claude.runner import run


def main():
    parser = argparse.ArgumentParser(
        prog="wa-claude",
        description="Adversarial prompt tester for LLM-powered endpoints",
    )
    parser.add_argument("target", help="URL of the LLM endpoint to test")
    parser.add_argument(
        "--category",
        choices=["injection", "jailbreak", "extraction", "all"],
        default="all",
        help="Attack category to run (default: all)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show full responses"
    )

    args = parser.parse_args()
    run(target=args.target, category=args.category, verbose=args.verbose)


if __name__ == "__main__":
    main()
