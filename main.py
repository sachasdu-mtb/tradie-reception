"""
Tradie Receptionist - SMS webhook handler
Version 0.2.1 - Sheets lookup with diagnostic logging

Adds debug logging to find_tradie() to expose what gspread is actually
reading from the Client tab. Once the lookup is verified working,
the verbose logging gets removed.
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

    # DIAGNOSTIC: log what we received and what we're comparing against.
    log.info("find_tradie searching for to_number=%r among %d rows",
             to_number, len(rows))
    for i, row in enumerate(rows):
        sheet_phone = row.get("phone_number", "")
        log.info("  row %d: phone_number=%r business_name=%r match=%s",
                 i, sheet_phone, row.get("business_name", ""),
                 sheet_phone.strip() == to_number.strip())
        if sheet_phone.strip() == to_number.strip():
            return row

    return None


@app.route("/", methods=["GET"])
def health() -> str:
    return "Tradie Receptionist v0.2.1 - alive (Sheets lookup with diagnostics)"


@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    body = request.form.get("Body", "")
    message_sid = request.form.get("MessageSid", "")

    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, message_sid, body,
    )

    tradie = find_tradie(to_number)

    if tradie is None:
        log.warning("No tradie found in Client tab for To=%s", to_number)
        reply_text = (
            "Sorry, this number isn't currently set up. "
            "Please try a different number."
        )
    else:
        log.info(
            "Tradie matched: %s (%s)",
            tradie.get("business_name", "<no name>"),
            to_number,
        )
        reply_text = (
            f"Hi, I'm Alex from {tradie.get('business_name', 'this business')}. "
            "(v0.2 plumbing - real responses in Layer 3.)"
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Message>{reply_text}</Message>'
        '</Response>'
    )
    return Response(twiml, mimetype="application/xml")


@app.route("/test", methods=["GET"])
def test() -> str:
    """Visit /test?to=+61485050078 to simulate a tradie lookup."""
    to_number = request.args.get("to", "")
    if not to_number:
        return "Pass ?to=+614xxxxxxxx to test a lookup"
    tradie = find_tradie(to_number)
    if tradie is None:
        return f"No tradie matched for {to_number}"
    return f"Matched: {tradie.get('business_name', '<no name>')}"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Tradie Receptionist on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
