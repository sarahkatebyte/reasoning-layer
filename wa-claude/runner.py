"""
runner.py - orchestrates attack runs against a target.

Loads config, picks the right sender, fires attacks, collects results.
"""

import yaml
from wa_claude.senders import OpenAISender, HTTPSender
from wa_claude.attacks import load_attacks
from wa_claude.report import Report


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_sender(target_config: dict):
    """
    Returns the right sender based on config mode.
    Sender contract: takes a prompt string, returns a response string.
    """
    mode = target_config.get("mode", "http")
    url = target_config["url"]
    headers = target_config.get("headers", {})

    if mode == "openai":
        return OpenAISender(url=url, headers=headers)
    elif mode == "http":
        field = target_config.get("field", "message")
        return HTTPSender(url=url, headers=headers, field=field)
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'openai' or 'http'.")


def run(config_path: str, verbose: bool = False):
    config = load_config(config_path)

    target = config["target"]
    categories = config.get("attacks", {}).get("categories", ["all"])

    print(f"\n🎯 Target: {target['url']}")
    print(f"📂 Categories: {', '.join(categories)}\n")

    sender = get_sender(target)
    attacks = load_attacks(categories)
    report = Report()

    for attack in attacks:
        print(f"  Running: {attack.name}...", end=" ", flush=True)
        response = sender.send(attack.prompt)
        result = attack.evaluate(response)
        report.add(attack, result, response)
        status = "🔴 VULNERABLE" if result.vulnerable else "🟢 SAFE"
        print(status)

    print()
    report.print_summary()
