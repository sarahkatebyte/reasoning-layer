"""
report.py - collects attack results and prints a summary.
"""


class Report:
    def __init__(self):
        self._results = []

    def add(self, attack, result, response: str):
        self._results.append({
            "name": attack.name,
            "vulnerable": result.vulnerable,
            "response": response,
        })

    def print_summary(self):
        total = len(self._results)
        vulnerable = sum(1 for r in self._results if r["vulnerable"])
        safe = total - vulnerable

        print(f"Summary: {total} tests | {vulnerable} vulnerable | {safe} safe")
        if vulnerable:
            print("\nVulnerable:")
            for r in self._results:
                if r["vulnerable"]:
                    print(f"  - {r['name']}")
