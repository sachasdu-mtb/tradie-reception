"""Tests for the Workbench main-line (0485 050 078) front-desk script.

That number is published on workbenchhq.com.au and /labs, so it must run the
Workbench Labs script (reception, websites, AI builds, TaxBench) and NOT the
client-receptionist playbook that pushes every caller to text details for a
"tradie callback". Any prompt change should keep these passing.

Run: GOOGLE_SHEET_ID=x TWILIO_ACCOUNT_SID=x TWILIO_AUTH_TOKEN=x \
     ANTHROPIC_API_KEY=x python tests/test_workbench_line_prompt.py
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
    assert main._is_workbench_line(WB)
    assert not main._is_workbench_line(CLIENT)

    for channel in ("sms", "voice"):
        p = main._build_system_prompt(WB, channel)
        check(f"[{channel}] identifies as Workbench", "for Workbench" in p)
        check(f"[{channel}] offers websites", "Websites" in p)
        check(f"[{channel}] offers Labs builds", "Workbench Labs" in p)
        check(f"[{channel}] offers TaxBench", "TaxBench" in p)
        check(f"[{channel}] Joe price correct", "$150 a month plus $400 setup" in p)
        check(f"[{channel}] no legacy pricing", "$100" not in p and "$249" not in p)
        check(f"[{channel}] no founding/discount offers",
              not any(w in p.lower() for w in
                      ("founding", "half price", "limited spots", "pilot rate", "discount ")))
        check(f"[{channel}] avoids the banned word",
              "honest" not in p.lower())
        check(f"[{channel}] keeps conversation open", "open question" in p)
        check(f"[{channel}] no tradie-callback handoff", "call you back within" not in p)
        check(f"[{channel}] routes to Sacha", "Sacha" in p)

    v = main._build_system_prompt(WB, "voice")
    check("[voice] spoken prices only", "a hundred and fifty" in v)
    check("[voice] no verbal detail capture", "transcription mangles" in v)
    s = main._build_system_prompt(WB, "sms")
    check("[sms] length cap present", "320" in s)
    check("[sms] demo line offered", "485 067 607" in s)

    c = main._build_system_prompt(CLIENT, "sms")
    check("client line unchanged: names the business", "Sunshine Plumbing & Gas" in c)
    check("client line unchanged: no Labs pitch", "Workbench Labs" not in c)

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
