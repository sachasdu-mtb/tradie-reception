"""Demo line (0485 067 607) must let Joe step out of the fictional plumber
role for real non-customer contacts and alert Sacha. Other lines unchanged.

Run: python tests/test_demo_line_escape.py
"""
import os
import sys

for k in ("GOOGLE_SHEET_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(k, "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

DEMO = {"phone_number": "+61485067607", "business_name": "Sunshine Plumbing & Gas (Demo)",
        "trade_type": "plumber", "service_area": "Sunshine Coast", "active": "TRUE"}
CLIENT = dict(DEMO, phone_number="+61400000000", business_name="Real Plumbing")
WB = {"phone_number": "+61485050078", "business_name": "Workbench", "active": "TRUE"}

failures = []
def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)

for ch in ("sms", "voice"):
    d = main._build_system_prompt(DEMO, ch)
    check(f"demo {ch} has escape block", main.DEMO_LINE_ESCAPE in d)
    check(f"demo {ch} gives front desk number", "0485 050 078" in d)
    check(f"demo {ch} flags Sacha via urgent tag", main.URGENT_TAG in main.DEMO_LINE_ESCAPE)
    check(f"client {ch} has no escape block", main.DEMO_LINE_ESCAPE not in main._build_system_prompt(CLIENT, ch))
    check(f"workbench {ch} has no escape block", main.DEMO_LINE_ESCAPE not in main._build_system_prompt(WB, ch))
check("no em dash in escape block", "\u2014" not in main.DEMO_LINE_ESCAPE)

sys.exit(1 if failures else 0)
