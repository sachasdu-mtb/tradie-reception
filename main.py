"""
Tradie Receptionist - SMS webhook handler
Version 0.3 - clean rebuild with type-safe phone matching
"""

import logging
import os
from typing import Optional

import gspread
from flask import Flask, request, Response
from google.oauth2.service_account import Credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("receptionist")

app = Flask(__name__)

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_PATH = "/etc/secrets/google-credentials.json"


# ---------------------------------------------------------------------------
# Sheets client (initialised once at startup)
# ---------------------------------------------------------------------------

def _build_sheets_client() -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    return gspread.authorize(creds)


try:
    sheets_client = _build_sheets_client()
    spreadsheet = sheets_client.open_by_key(SHEET_ID)
    log.info("Sheets client initialised. Spreadsheet: %s", spreadsheet.title)
except Exception as exc:
    log.exception("Failed to initialise Sheets client: %s", exc)
    sheets_client = None
    spreadsheet = None


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

def _normalise_phone(value) -> str:
    """
    Convert any cell value to a clean E.164-ish string for comparison.

    Handles:
      - ints (when Sheets stores phone as number, dropping leading +)
      - strings with or without leading +
      - stray whitespace

    Returns a string with leading + if the value looks like a number.
    """
    s = str(value).strip()
    if s and not s.startswith("+") and s.isdigit():
        s = "+" + s
    return s


# ---------------------------------------------------------------------------
# Tradie lookup
# ---------------------------------------------------------------------------

def find_tradie(to_number: str) -> Optional[dict]:
    """Look up the tradie row whose phone_number matches to_number."""
    if spreadsheet is None:
        log.error("Sheets client not initialised; cannot look up tradie")
        return None

    try:
        client_tab = spreadsheet.worksheet("Client")
        rows = client_tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Client tab: %s", exc)
        return None

    target = _normalise_phone(to_number)
    log.info("find_tradie searching for to_number=%r among %d rows", target, len(rows))

    for i, row in enumerate(rows):
        sheet_phone = _normalise_phone(row.get("phone_number", ""))
        match = sheet_phone == target
        log.info(
            "  row %d: phone_number=%s business_name=%r match=%s",
            i, sheet_phone, row.get("business_name", ""), match,
        )
        if match:
            return row

    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health() -> str:
    return "Tradie Receptionist v0.3 - alive (clean rebuild)"


@app.route("/test", methods=["GET"])
def test() -> str:
    to = request.args.get("to", "")
    if not to:
        return "Pass ?to=+61... to look up a tradie"
    tradie = find_tradie(to)
    if tradie:
        return f"Matched: {tradie.get('business_name', '(no name)')}"
    return f"No tradie matched for {to}"


@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    sid = request.form.get("MessageSid", "")
    body = request.form.get("Body", "")
    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, sid, body,
    )

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie found in Client tab for To=%s", to_number)
        twiml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Response><Message>Sorry, this number isn't configured yet.</Message></Response>"
        )
        return Response(twiml, mimetype="application/xml")

    business = tradie.get("business_name", "your tradie")
    log.info("Tradie matched: %s (%s)", business, to_number)
    reply = f"Hi, I'm Alex from {business}. (v0.3 - real LLM responses in Layer 3.)"
    twiml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{reply}</Message></Response>"
    )
    return Response(twiml, mimetype="application/xml")