"""Smoke tests for the public /api/lead endpoint.

Run locally with:
    GOOGLE_SHEET_ID=x TWILIO_ACCOUNT_SID=x TWILIO_AUTH_TOKEN=x \
    ANTHROPIC_API_KEY=x python tests/test_api_lead.py

These exist because /api/lead once returned HTTP 500 on every submission for
weeks (jsonify was never imported) while the homepage and the CORS preflight
both stayed healthy, so nothing caught it. Any change to the endpoint should
keep these passing.
"""

import os
import sys

os.environ.setdefault("GOOGLE_SHEET_ID", "test")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

ORIGIN = {"Origin": "https://www.workbenchhq.com.au"}
OK_STATUSES = {200, 400, 429, 503}


def check(label, condition):
    print(("PASS " if condition else "FAIL ") + label)
    return condition


def main_tests():
    client = main.app.test_client()
    results = []

    # No path may ever return a bare 500 HTML page: the browser fetch() treats
    # that as a network failure because Flask error pages carry no CORS header.
    cases = [
        ("honeypot", {"hp_company": "bot", "name": "x"}),
        ("empty", {}),
        ("missing fields", {"name": "a"}),
        ("bad mobile", {"name": "a", "mobile": "123", "business": "b", "trade": "t"}),
        ("landline", {"name": "a", "mobile": "0712345678", "business": "b", "trade": "t"}),
        ("valid", {"name": "a", "mobile": "0402 585 413", "business": "b", "trade": "t"}),
    ]
    for label, data in cases:
        resp = client.post("/api/lead", data=data, headers=ORIGIN)
        results.append(check(f"{label}: status {resp.status_code} not 500", resp.status_code in OK_STATUSES))
        results.append(check(f"{label}: JSON body", resp.is_json))
        results.append(check(
            f"{label}: CORS header present",
            resp.headers.get("Access-Control-Allow-Origin") == ORIGIN["Origin"],
        ))

    # Preflight and origin gating
    pre = client.open("/api/lead", method="OPTIONS", headers=ORIGIN)
    results.append(check("preflight 204", pre.status_code == 204))
    bad = client.post("/api/lead", data={"name": "a"}, headers={"Origin": "https://evil.example"})
    results.append(check("unknown origin blocked", bad.status_code == 403))

    # AU mobile normalisation
    norm = main._normalise_au_mobile
    for raw in ["0402585413", "0402 585 413", "+61402585413", "61402585413", "(04)0258-5413"]:
        results.append(check(f"normalise {raw}", norm(raw) == "+61402585413"))
    for raw in ["0212345678", "abc", "", "0402585"]:
        results.append(check(f"reject {raw!r}", norm(raw) == ""))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main_tests())
