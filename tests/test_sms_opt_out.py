"""Tests for SMS opt-out handling on /sms.

Run locally with:
    GOOGLE_SHEET_ID=x TWILIO_ACCOUNT_SID=x TWILIO_AUTH_TOKEN=x \\
    ANTHROPIC_API_KEY=x python tests/test_sms_opt_out.py

Exists because a real person texted "Stop" to the Workbench line (24 Sep 2026)
and the webhook passed it straight to Joe for an AI reply. Under the Spam Act
a STOP must be honoured: no AI reply, no further texts from that line.
"""

import os
import sys

os.environ.setdefault("GOOGLE_SHEET_ID", "test")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

LINE = "+61485050078"
CUSTOMER = "+61400000001"
OWNER = "+61400000999"


class FakeTab:
    def __init__(self, headers):
        self.headers = headers
        self.rows = []

    def append_row(self, row, value_input_option=None):
        self.rows.append(row)

    def get_all_records(self):
        return [dict(zip(self.headers, r)) for r in self.rows]


class FakeMessages:
    def __init__(self):
        self.sent = []

    def create(self, body, from_, to):
        self.sent.append((from_, to, body))


class FakeTwilio:
    def __init__(self):
        self.messages = FakeMessages()


def check(label, condition):
    print(("PASS " if condition else "FAIL ") + label)
    return condition


def main_tests():
    results = []
    opt_tab = FakeTab(main.OPT_OUTS_HEADERS)
    tabs = {main.OPT_OUTS_TAB: opt_tab}
    main._ensure_tab = lambda name, headers: tabs.get(name)
    main.find_tradie = lambda to: {"business_name": "Workbench", "phone_number": LINE,
                                   "owner_mobile": OWNER}
    main.log_conversation = lambda *a, **k: None
    main.is_muted = lambda *a, **k: False
    main.get_conversation_history = lambda *a, **k: []
    ai_calls = []
    main.generate_reply = lambda *a, **k: ai_calls.append(a) or "AI reply"
    fake = FakeTwilio()
    main.twilio_client = fake
    client = main.app.test_client()

    def sms(body, frm=CUSTOMER):
        return client.post("/sms", data={"From": frm, "To": LINE, "Body": body,
                                         "MessageSid": "SMtest"}).get_data(as_text=True)

    for kw in ["STOP", "Stop", "stop.", " unsubscribe ", "Opt out"]:
        results.append(check(f"keyword {kw!r} detected", main.is_opt_out_keyword(kw)))
    for kw in ["stop by at 3", "Can you stop the leak", "cancel", "start"]:
        results.append(check(f"not an opt-out: {kw!r}", not main.is_opt_out_keyword(kw)))

    out = sms("Stop")
    results.append(check("STOP gets confirmation", "unsubscribed" in out))
    results.append(check("STOP does not call the AI", not ai_calls))
    results.append(check("STOP recorded", main.is_opted_out(LINE, CUSTOMER)))
    results.append(check("owner told", any(to == OWNER for _, to, _ in fake.messages.sent)))

    fake.messages.sent.clear()
    results.append(check("outbound to opted-out number suppressed",
                         main._send_sms(LINE, CUSTOMER, "hello") is False and not fake.messages.sent))
    results.append(check("other lines unaffected", main._send_sms("+61485067607", CUSTOMER, "hi")))

    out = sms("Actually I need a quote")
    results.append(check("opted-out message gets no reply", "<Message>" not in out and not ai_calls))

    out = sms("START")
    results.append(check("START opts back in", "opted back in" in out and not main.is_opted_out(LINE, CUSTOMER)))

    out = sms("Hi, need a plumber")
    results.append(check("normal message reaches the AI after opt-in", len(ai_calls) == 1 and "AI reply" in out))

    out = sms("START", frm="+61400000002")
    results.append(check("START from never-opted-out number goes to AI", len(ai_calls) == 2))

    results.append(check("24 Sep 'Stop' sender is opted out",
                         main.is_opted_out("+61485050078", "+61457292901")))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main_tests())
