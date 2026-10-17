"""Every text to the Workbench front desk is forwarded to the owner.

Run: python tests/test_frontdesk_sms_forward.py

Joe's front desk SMS script says "I'll get Sacha to come back to you today",
so Sacha must see every inbound text, not only ##URGENT## ones. Client lines
and the demo line keep the old behaviour (urgent only).
"""

import os
import sys

for k in ("GOOGLE_SHEET_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(k, "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

FRONT = "+61485050078"
OTHER = "+61400000555"
CUSTOMER = "+61400000001"
OWNER = "+61400000999"


class FakeMessages:
    def __init__(self):
        self.sent = []

    def create(self, body, from_, to):
        self.sent.append((from_, to, body))


class FakeTwilio:
    def __init__(self):
        self.messages = FakeMessages()


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    return cond


def run():
    results = []
    line = {"n": FRONT}
    main.find_tradie = lambda to: {"business_name": "Biz", "phone_number": line["n"], "owner_mobile": OWNER}
    main.log_conversation = lambda *a, **k: None
    main.is_muted = lambda *a, **k: False
    main.is_opted_out = lambda *a, **k: False
    main.get_conversation_history = lambda *a, **k: []
    reply = {"r": "Thanks, what's the business?"}
    main.generate_reply = lambda *a, **k: reply["r"]
    fake = FakeTwilio()
    main.twilio_client = fake
    c = main.app.test_client()

    def sms(body, frm=CUSTOMER):
        fake.messages.sent.clear()
        return c.post("/sms", data={"From": frm, "To": line["n"], "Body": body, "MessageSid": "SM1"}).get_data(as_text=True)

    out = sms("Do you build websites?")
    to_owner = [b for _, to, b in fake.messages.sent if to == OWNER]
    results.append(check("front desk text forwarded to owner", len(to_owner) == 1))
    results.append(check("forward has sender, message and Joe reply",
                         to_owner and CUSTOMER in to_owner[0] and "websites" in to_owner[0] and "business" in to_owner[0]))
    results.append(check("customer still gets Joe's reply", "<Message>" in out))

    reply["r"] = "On it\n" + main.URGENT_TAG
    sms("Urgent, call me")
    results.append(check("urgent sends exactly one alert", len([1 for _, to, _ in fake.messages.sent if to == OWNER]) == 1))

    reply["r"] = "Thanks"
    sms("hello", frm=OWNER)
    results.append(check("owner texting own line is not forwarded back", not fake.messages.sent))

    line["n"] = OTHER
    sms("Need a plumber")
    results.append(check("client lines not forwarded for normal texts", not fake.messages.sent))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
