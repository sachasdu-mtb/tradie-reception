"""US demo line (+1 469 518 6052): US voice, US English, US$ pricing,
no live transfer to Sacha's Australian mobile. AU lines unchanged.

Run: python tests/test_us_demo_line.py
"""
import os
import sys

for k in ("GOOGLE_SHEET_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(k, "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

US = {"phone_number": "+14695186052", "business_name": "Lone Star Plumbing (Demo)",
      "trade_type": "plumber", "service_area": "Dallas", "active": "TRUE",
      "owner_mobile": "+61402585413"}
AU = {"phone_number": "+61485067607", "business_name": "Sunshine Plumbing & Gas (Demo)",
      "trade_type": "plumber", "service_area": "Sunshine Coast", "active": "TRUE"}

failures = []
def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)

us_p = main._build_system_prompt(US, "voice")
au_p = main._build_system_prompt(AU, "voice")
check("US prompt has locale block", "American English" in us_p)
check("US prompt has US demo escape", "UNITED STATES" in us_p and "150 US dollars" in us_p)
check("US prompt has no AU front desk number", "0485 050 078" not in us_p)
check("AU prompt unchanged: no US locale", "American English" not in au_p and "UNITED STATES" not in au_p)
check("AU prompt keeps AU escape", "0485 050 078" in au_p)

app = main.app
with app.test_request_context("/voice", method="POST", data={"To": "+14695186052"}):
    say = main._voice_say("hi"); g = main._voice_gather("/voice/turn", "hi")
    check("US Say uses en-US voice", "en-US-Neural2-F" in say and 'language="en-US"' in say)
    check("US Gather uses en-US", g.count('language="en-US"') == 2)
with app.test_request_context("/voice", method="POST", data={"To": "+61485067607"}):
    say = main._voice_say("hi")
    check("AU Say keeps en-AU voice", "en-AU-Neural2-C" in say)
check("No request context falls back to AU", "en-AU" in main._voice_say("hi"))

main.find_tradie = lambda to: US if to == "+14695186052" else AU
with app.test_client() as c:
    r = c.post("/voice", data={"CallSid": "CAus1", "From": "+12145550100", "To": "+14695186052"})
    body = r.get_data(as_text=True)
    check("US greeting has no G'day", "G'day" not in body and "Hi, you" in body)

main.send_urgent_alert = lambda *a, **k: None
main._finalise_call = lambda *a, **k: None
state = {"tradie": US, "from": "+12145550100", "to": "+14695186052", "history": [],
         "transcript_lines": [], "turn_count": 1, "ended": False}
with app.test_request_context("/voice/reply", method="POST", data={"To": "+14695186052"}):
    resp = main._voice_closing_reply("CAus2", dict(state), "burst pipe", "On it. " + main.URGENT_TAG)
    check("US urgent does not Dial Australia", "<Dial" not in resp.get_data(as_text=True))

if failures:
    sys.exit(1)
print("ALL PASS")
