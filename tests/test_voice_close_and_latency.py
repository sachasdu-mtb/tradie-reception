"""Tests for voice call turn-taking speed and a proper call close.

Run locally with:
    GOOGLE_SHEET_ID=x TWILIO_ACCOUNT_SID=x TWILIO_AUTH_TOKEN=x \\
    ANTHROPIC_API_KEY=x python tests/test_voice_close_and_latency.py

Exists because (29 Sep 2026) the demo line had ~9 seconds of silence before
Joe's last line, then hung up the moment she finished "text us your details".
"""

import os
import sys
import time

os.environ.setdefault("GOOGLE_SHEET_ID", "test")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

LINE = "+61485067607"
CALLER = "+61400000001"
TRADIE = {"business_name": "Sunshine Plumbing and Gas", "phone_number": LINE,
          "owner_mobile": "+61400000999"}


def check(label, condition):
    print(("PASS " if condition else "FAIL ") + label)
    return condition


def main_tests():
    results = []
    sent_owner = []
    finalise_calls = []

    main.find_tradie = lambda to: dict(TRADIE)
    main.send_to_owner = lambda t, body: sent_owner.append(body) or True

    def slow_log(**kw):
        time.sleep(2)
        finalise_calls.append(kw)
    main.log_conversation = slow_log
    main.send_voice_summary = lambda *a, **k: None
    main.send_caller_handoff = lambda *a, **k: None

    replies = iter([
        "We handle blocked drains all the time. Could you text this number with your name, suburb and the job? The tradie will call back within the hour.\n##END##",
        "Yes, Saturday call outs are available.",
    ])
    main.generate_reply = lambda *a, **k: next(replies)

    c = main.app.test_client()
    sid = "CA_test_close"
    r = c.post("/voice", data={"CallSid": sid, "From": CALLER, "To": LINE})
    x = r.get_data(as_text=True)
    results.append(check("gather uses deepgram_nova-3", 'speechModel="deepgram_nova-3"' in x))
    results.append(check("speechTimeout is 1s", 'speechTimeout="1"' in x))
    results.append(check("no experimental_conversations", "experimental_conversations" not in x))

    t0 = time.monotonic()
    r = c.post("/voice/turn", data={"CallSid": sid, "SpeechResult": "My drain is blocked, can you send someone out?"})
    elapsed = time.monotonic() - t0
    x = r.get_data(as_text=True)
    results.append(check(f"END turn does not wait on Sheets logging ({elapsed:.2f}s)", elapsed < 1.0))
    results.append(check("END turn asks anything else", "anything else I can help you with" in x))
    results.append(check("END turn keeps listening (Gather)", x.index("<Gather") < x.index("anything else")))
    results.append(check("END turn falls back to goodbye then hangup",
                         "Thanks for calling Sunshine Plumbing and Gas. Have a great day. Goodbye." in x
                         and x.rstrip().endswith("<Hangup/></Response>")))
    results.append(check("##END## tag not spoken", "##END##" not in x))
    results.append(check("closing gather has short timeout", 'timeout="4"' in x))

    r = c.post("/voice/turn", data={"CallSid": sid, "SpeechResult": "No, that's all, thanks."})
    x = r.get_data(as_text=True)
    results.append(check("'no thanks' gets goodbye", "Have a great day. Goodbye." in x))
    results.append(check("goodbye has pause before hangup", '<Pause length="1"/><Hangup/>' in x))
    results.append(check("no second Gather after goodbye", "<Gather" not in x))

    # Caller has one more question at the close.
    sid2 = "CA_test_more"
    replies2 = iter([
        "Sure, could you text your name and suburb to this number? The tradie will call back.\n##END##",
        "Yes, we do Saturday call outs.",
    ])
    main.generate_reply = lambda *a, **k: next(replies2)
    c.post("/voice", data={"CallSid": sid2, "From": CALLER, "To": LINE})
    c.post("/voice/turn", data={"CallSid": sid2, "SpeechResult": "Hot water is out"})
    r = c.post("/voice/turn", data={"CallSid": sid2, "SpeechResult": "Actually, do you work Saturdays?"})
    x = r.get_data(as_text=True)
    results.append(check("follow-up question answered", "Saturday call outs" in x))
    results.append(check("then goodbye and hangup", "Goodbye." in x and "<Hangup/>" in x))
    results.append(check("follow-up passed to owner", any("do you work Saturdays" in b for b in sent_owner)))

    # Silence at "anything else?" -> goodbye.
    sid3 = "CA_test_silent"
    main.generate_reply = lambda *a, **k: "Text us your details please.\n##END##"
    c.post("/voice", data={"CallSid": sid3, "From": CALLER, "To": LINE})
    c.post("/voice/turn", data={"CallSid": sid3, "SpeechResult": "Leaking tap"})
    r = c.post("/voice/turn", data={"CallSid": sid3, "SpeechResult": ""})
    x = r.get_data(as_text=True)
    results.append(check("silence at close gets goodbye", "Goodbye." in x and "<Gather" not in x))

    # Closing classifier.
    for s, want in [("no", True), ("Nah that's it cheers", True), ("thank you bye", True),
                    ("ok but can you come Tuesday", False), ("what time will he come?", False),
                    ("my hot water system is also leaking under the house", False)]:
        results.append(check(f"closing done {s!r} -> {want}", main._is_closing_done(s) is want))

    time.sleep(2.5)
    results.append(check("call still logged to Sheets in background", len(finalise_calls) == 3))

    prompt = main._build_system_prompt(dict(TRADIE), "voice")
    results.append(check("voice prompt tells Joe not to say goodbye herself", "do NOT say goodbye" in prompt))

    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main_tests() else 1)
