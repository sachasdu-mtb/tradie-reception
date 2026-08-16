"""Tone and AI-disclosure rules must be in every prompt, client lines included.

Sacha's rule (17 Aug 2026): no casual slang like "nah mate", and if anyone
asks whether Joe is AI, she says "yes, I am AI" straight up.

Run: GOOGLE_SHEET_ID=x TWILIO_ACCOUNT_SID=x TWILIO_AUTH_TOKEN=x \
     ANTHROPIC_API_KEY=x python tests/test_tone_and_disclosure.py
"""

import os
import sys

for k in ("GOOGLE_SHEET_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
          "ANTHROPIC_API_KEY"):
    os.environ.setdefault(k, "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

WB = {"phone_number": "+61485050078", "business_name": "Workbench", "active": "TRUE"}
CLIENT = {
    "phone_number": "+61400000000", "business_name": "Sunshine Plumbing & Gas",
    "trade_type": "plumber", "service_area": "Sunshine Coast",
    "services_offered": "Hot water systems, blocked drains", "active": "TRUE",
}

failures = []


def check(label, condition):
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        failures.append(label)


def run():
    for name, tradie in (("workbench", WB), ("client", CLIENT)):
        for channel in ("sms", "voice"):
            p = main._build_system_prompt(tradie, channel)
            check(f"[{name}/{channel}] tone block present",
                  "TONE AND DISCLOSURE" in p)
            check(f"[{name}/{channel}] bans slang", "'mate'" in p and "'nah'" in p)
            check(f"[{name}/{channel}] discloses AI",
                  "yes, you are AI" in p and "Never claim to be human" in p)
            check(f"[{name}/{channel}] first-ask disclosure, no deflection",
                  "answer straight away and without hedging" in p
                  and "first sentence" in p and "do not deflect" in p)
            check(f"[{name}/{channel}] bans the word honest",
                  "'honest'" in p and "honest" not in p.replace(
                      "the word 'honest' or 'honestly'", ""))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
