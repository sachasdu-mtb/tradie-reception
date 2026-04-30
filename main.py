"""
Tradie Receptionist - SMS webhook handler
Version 0.1 - Plumbing only

This first version receives Twilio SMS webhooks and replies with a
hardcoded message. No Claude or Sheets integration yet - we're proving
the webhook plumbing works on Replit before adding layers.

Run with: python3 main.py
The Flask server starts on port 8080 (Replit's default exposed port).
"""

import logging
import os

from flask import Flask, request, Response            

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("receptionist")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health() -> str:
    """Visiting the Repl URL in a browser hits this. Confirms the app is up."""
    return "Tradie Receptionist v0.1 - alive"


# ---------------------------------------------------------------------------
# Twilio SMS webhook
# ---------------------------------------------------------------------------

@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    """
    Receives an incoming SMS from Twilio.

    Twilio sends form-encoded POST data with fields like:
      From   - the customer's number  (e.g. +61402585413)
      To     - the tradie's number    (e.g. +61485050078)
      Body   - the message text
      MessageSid - Twilio's unique ID for this message

    We respond with TwiML (Twilio's XML format) that tells Twilio what to
    SMS back to the customer.
    """
    # Read incoming fields
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    body = request.form.get("Body", "")
    message_sid = request.form.get("MessageSid", "")

    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, message_sid, body,
    )

    # For v0.1 we just reply with a hardcoded greeting.
    # Later we'll: lookup tradie -> fetch history -> call Claude -> reply.
    reply_text = "Hi, I'm Alex. (This is v0.1 plumbing - real responses coming soon.)"

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Message>{reply_text}</Message>'
        '</Response>'
    )

    return Response(twiml, mimetype="application/xml")


# ---------------------------------------------------------------------------
# Local test endpoint (optional, for development without Twilio)
# ---------------------------------------------------------------------------

@app.route("/test", methods=["GET"])
def test() -> str:
    """
    Lets you simulate a webhook from a browser without needing Twilio.
    Visit:  https://<your-repl-url>/test?body=hello
    """
    fake_body = request.args.get("body", "test message")
    log.info("Test endpoint hit with body=%r", fake_body)
    return f"Test OK. You sent: {fake_body!r}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Replit exposes port 8080 by default; binding to 0.0.0.0 lets external
    # traffic reach the server (critical - without this Twilio can't connect).
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Tradie Receptionist on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)