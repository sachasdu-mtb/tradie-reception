"""
Tradie Receptionist + Quotes + Xero - SMS + Voice + Quote portal + Invoicing
Version 1.1.3 - fix column letter calc for tabs with >26 columns
"""

import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape as xml_escape
from threading import Lock, Timer
from typing import Optional

import gspread
import requests
from anthropic import Anthropic
from flask import (
    Flask, request, Response, render_template, redirect, url_for,
    session, flash, abort,
)
from google.oauth2.service_account import Credentials
from twilio.rest import Client as TwilioClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("workbench")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_CREDS_PATH = "/etc/secrets/google-credentials.json"

ASSISTANT_NAME = "Joe"

VOICE_NAME = "Google.en-AU-Neural2-C"
VOICE_LANG = "en-AU"
SPEECH_TIMEOUT_SECONDS = "2"  # seconds of silence before Twilio considers speech over

CONVERSATION_LOG_TAB = "Conversation Log"
CONVERSATION_LOG_HEADERS = [
    "timestamp", "business_name", "from_number", "to_number",
    "message", "reply", "urgent",
]

MUTES_TAB = "Mutes"
MUTES_HEADERS = ["timestamp", "business_name", "customer_number", "expires_at"]

URGENT_TAG = "##URGENT##"
END_TAG = "##END##"
MUTE_HOURS = 24

HISTORY_TURN_LIMIT = 6
HISTORY_HOURS_LIMIT = 24

VOICE_TURN_LIMIT = 5

# Random delay before the post-call SMS reaches the caller. A real receptionist
# wouldn't text you 0.4 seconds after hanging up — randomising avoids the
# tell-tale "robot ping" cadence. Range is in seconds.
CALLER_HANDOFF_DELAY_MIN_SECONDS = 60
CALLER_HANDOFF_DELAY_MAX_SECONDS = 90

# ============================================================================
# Quotes — Workbench Quotes MVP
# ============================================================================

QUOTES_TAB = "Quotes"
QUOTES_HEADERS = [
    "quote_id", "tradie_phone", "business_name",
    "customer_name", "customer_phone", "customer_email",
    "job_description",
    "subtotal", "gst", "total",
    "status",  # draft / sent / viewed / accepted / declined
    "created_at", "sent_at", "viewed_at", "responded_at",
    "tradie_terms", "notes",
]

QUOTE_ITEMS_TAB = "QuoteItems"
QUOTE_ITEMS_HEADERS = [
    "item_id", "quote_id", "line_order",
    "description", "quantity", "unit", "unit_price", "line_total",
]

SESSIONS_TAB = "QuoteSessions"
SESSIONS_HEADERS = [
    "token", "tradie_phone", "created_at", "expires_at", "status",
]

GST_RATE = 0.10
SESSION_PENDING_HOURS = 1   # how long the magic link is valid
SESSION_ACTIVE_DAYS = 30    # how long after redemption the session stays signed in
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://tradie-reception.onrender.com"
)


# ============================================================================
# Xero — accounting integration (draft-and-tradie-sends model)
# ============================================================================

# Tabs
INVOICES_TAB = "Invoices"
INVOICES_HEADERS = [
    "invoice_id",            # Workbench UUID
    "quote_id",              # link back to source quote
    "tradie_phone",
    "external_invoice_id",   # Xero (or other platform) invoice ID
    "external_platform",     # 'xero' / 'myob' / 'quickbooks'
    "external_url",          # deep link back to invoice in their system
    "invoice_number",        # human-readable (Xero assigns INV-0001 etc.)
    "status",                # draft / sent / paid / void / overdue / failed
    "customer_name",
    "customer_email",
    "customer_phone",
    "customer_external_id",  # contact ID in platform
    "currency",              # default AUD
    "issue_date",
    "due_date",
    "subtotal",
    "tax_total",
    "total",
    "amount_paid",
    "amount_due",
    "paid_date",
    "payment_terms_days",
    "reference",
    "notes_internal",
    "notes_customer",
    "account_code",
    "tax_treatment",         # Inclusive / Exclusive / NoTax
    "branding_theme_id",     # Xero specific, null-safe
    "tracking_categories",
    "created_at",
    "updated_at",
    "last_sync_at",
    "last_sync_status",      # ok / failed / pending
]

INVOICE_LINES_TAB = "InvoiceLines"
INVOICE_LINES_HEADERS = [
    "line_id", "invoice_id", "line_order",
    "description", "quantity", "unit",
    "unit_price", "tax_rate_code", "account_code",
    "tracking_category_1", "tracking_category_2",
    "line_total",
]

# Leads (signups from workbenchhq.org/start)
LEADS_TAB = "Leads"
LEADS_HEADERS = [
    "lead_id", "created_at", "source",
    "name", "mobile", "email", "business_name", "trade_type", "location",
    "notes", "status", "first_contacted_at", "converted_at",
    "user_agent", "ip_address",
]
LEAD_NOTIFY_NUMBER = os.environ.get("LEAD_NOTIFY_NUMBER", "+61402585413")
LEAD_FROM_NUMBER = os.environ.get("LEAD_FROM_NUMBER", "+61485050078")
ALLOWED_LEAD_ORIGINS = {"https://workbenchhq.com.au", "https://www.workbenchhq.com.au", "https://workbenchhq.org", "https://www.workbenchhq.org"}

# Xero API
XERO_CLIENT_ID = os.environ.get("XERO_CLIENT_ID", "")
XERO_CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET", "")
XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"
XERO_SCOPES = (
    "offline_access "
    "accounting.invoices "
    "accounting.contacts "
    "accounting.settings"
)
XERO_WEBHOOK_KEY = os.environ.get("XERO_WEBHOOK_KEY", "")  # for signing verification

# OAuth state cache (in-memory; survives a single OAuth flow only)
_xero_oauth_state: dict[str, dict] = {}
_xero_oauth_state_lock = Lock()


# ============================================================================
# Encryption — Fernet symmetric encryption for secrets at rest
# ============================================================================
#
# Secrets (Xero access + refresh tokens) are encrypted before writing to the
# Sheet. Encrypted values are tagged with a "fern:" prefix so the decrypt
# helper can recognise them. Plaintext values (from before encryption was
# enabled, or if the prefix is missing) pass through unchanged — backward-
# compatible for the existing pilot data.
#
# Key is read once at startup from WORKBENCH_ENCRYPTION_KEY env var. If
# missing, we log loudly and fall back to plaintext storage. Generated via:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

from cryptography.fernet import Fernet, InvalidToken

WORKBENCH_ENCRYPTION_KEY = os.environ.get("WORKBENCH_ENCRYPTION_KEY", "").strip()
ENCRYPTED_PREFIX = "fern:"

_fernet: Optional[Fernet] = None
if WORKBENCH_ENCRYPTION_KEY:
    try:
        _fernet = Fernet(WORKBENCH_ENCRYPTION_KEY.encode())
        log.info("Encryption initialised (Fernet)")
    except Exception as exc:
        log.error("WORKBENCH_ENCRYPTION_KEY is malformed: %s. "
                  "Secrets will be stored as plaintext until fixed.", exc)
else:
    log.warning("WORKBENCH_ENCRYPTION_KEY not set. Secrets will be stored "
                "as plaintext. Set the env var to enable encryption.")


def encrypt_secret(value: str) -> str:
    """Encrypt a value for at-rest storage. Returns tagged ciphertext.
    If encryption is unavailable, returns the value unchanged (logged warning)."""
    if not value:
        return ""
    if _fernet is None:
        return value
    try:
        token = _fernet.encrypt(value.encode()).decode()
        return f"{ENCRYPTED_PREFIX}{token}"
    except Exception as exc:
        log.exception("encrypt_secret failed (storing plaintext): %s", exc)
        return value


def decrypt_secret(value: str) -> str:
    """Decrypt a value. Tolerates plaintext (legacy data, encryption-off,
    or accidentally-written plaintext) by returning it unchanged."""
    if not value:
        return ""
    if not value.startswith(ENCRYPTED_PREFIX):
        return value  # plaintext, return as-is
    if _fernet is None:
        log.error("decrypt_secret called but encryption is not initialised. "
                  "Returning empty string to avoid leaking ciphertext.")
        return ""
    try:
        return _fernet.decrypt(value[len(ENCRYPTED_PREFIX):].encode()).decode()
    except InvalidToken:
        log.error("decrypt_secret: InvalidToken — wrong key or corrupted data. "
                  "Returning empty string.")
        return ""
    except Exception as exc:
        log.exception("decrypt_secret failed: %s", exc)
        return ""


# Field names that are encrypted at rest. Helper functions below auto-
# encrypt on write and auto-decrypt on read for these specific fields.
_ENCRYPTED_FIELDS = {
    "accounting_access_token",
    "accounting_refresh_token",
}


def client_set_secret(tradie_phone: str, field_name: str, value: str) -> bool:
    """Write an encrypted value to the Client tab for a known-sensitive field."""
    if field_name not in _ENCRYPTED_FIELDS:
        log.warning("client_set_secret called for non-secret field %s; "
                    "writing as plaintext via client_update_field", field_name)
        return client_update_field(tradie_phone, field_name, value)
    return client_update_field(tradie_phone, field_name, encrypt_secret(value))


def client_get_secret(tradie_phone: str, field_name: str) -> str:
    """Read and decrypt a value from the Client tab. Returns plaintext."""
    if field_name not in _ENCRYPTED_FIELDS:
        return client_get_field(tradie_phone, field_name)
    return decrypt_secret(client_get_field(tradie_phone, field_name))

_voice_state: dict[str, dict] = {}
_voice_state_lock = Lock()


# ===========================================================================
# Prompts
# ===========================================================================

GUARDRAILS = (
    "STRICT RULES — these override anything else:\n"
    "- Use ONLY the services explicitly listed in 'Services we offer'. "
    "Do NOT expand abbreviations, do NOT infer related services, do NOT "
    "guess at what the trade type might include. If a service isn't on the "
    "list, you don't offer it.\n"
    "- If a customer asks about something not on the list, say you'll need "
    "to check with the tradie and offer to take their details.\n"
    "- Do NOT invent prices, fees, timeframes, qualifications, or guarantees "
    "that aren't written above.\n"
    "- Do NOT speculate about the meaning of trade names or abbreviations. "
    "If unsure what the customer means, ask them rather than guess."
)

SMS_INSTRUCTION = (
    "IMPORTANT: This conversation is over SMS. Keep every reply under "
    "320 characters (about 2 SMS segments). Be concise but warm. Each reply "
    "should move the customer toward either (a) a quoted price, or "
    "(b) a booked on-site visit via the booking link. Ask only for the "
    "minimum info needed.\n"
    "\n"
    "Do NOT use emojis. Do NOT use exclamation-heavy or overly casual "
    "language. Plain professional Aussie tone — friendly but no theatrics."
)

SMS_PLAYBOOK = (
    "When a customer messages, your job is to:\n"
    "1. Greet warmly and confirm we cover what they need (only on the FIRST "
    "message in a conversation — if there is prior history, skip the greeting "
    "and continue naturally)\n"
    "2. Either provide a price/range if it's a standard job, OR send the "
    "booking link for an on-site quote\n"
    "3. Collect: their name, suburb, brief description of the job\n"
    "4. If genuinely urgent (gas leak, flooding, no power, electrical "
    "danger, etc.), end your reply with ##URGENT## on its own line\n"
    "\n"
    "Be honest. If you don't know something, say you'll get the tradie to confirm."
)

VOICE_INSTRUCTION = (
    "IMPORTANT: You are on a phone call with the customer right now. Speak "
    "naturally and concisely — 1 to 2 short sentences per turn, never more. "
    "Aussie tone, friendly and professional.\n"
    "\n"
    "Do NOT use formatting, lists, asterisks, headers, or symbols. Plain "
    "spoken English only. No emojis or special characters — your words will "
    "be read aloud.\n"
    "\n"
    "Speak numbers, prices, and times as a person would say them out loud "
    "(e.g. 'one hundred dollars' not '$100'; 'nine o'clock' not '9:00').\n"
    "\n"
    "Do NOT try to capture customer names, suburbs, addresses, or specific "
    "job details over the phone — phone speech recognition gets these wrong. "
    "Get them via the SMS handoff (see playbook)."
)

VOICE_PLAYBOOK = (
    "You are answering an inbound phone call. The system has already greeted "
    "the caller — do NOT greet them again. Your job is to:\n"
    "\n"
    "1. Listen to the customer explain their issue\n"
    "2. Briefly confirm we cover that kind of work (one short sentence)\n"
    "3. Ask them to text this same number with their name, suburb, and a "
    "brief description of the job, and tell them the tradie will call them "
    "back within the hour\n"
    "4. End your reply with ##END## on its own line so the call wraps cleanly\n"
    "\n"
    "Example reply: 'Yep, we handle hot water systems all the time. To make "
    "sure we get your details right, could you text us at this number with "
    "your name, suburb, and a quick description of the job? The tradie will "
    "give you a call back within the hour. ##END##'\n"
    "\n"
    "If the customer asks something that isn't on the services list, say you'll "
    "check with the tradie and ask them to text the same details — then ##END##.\n"
    "\n"
    "If genuinely urgent (gas leak, flooding, no power, electrical danger, "
    "sewerage overflow, etc.) end your reply with ##URGENT## instead of "
    "##END## — the system will try to transfer the caller live.\n"
    "\n"
    "Do NOT try to collect details verbally. Do NOT quote prices. Do NOT "
    "promise specific times beyond 'within the hour'."
)


def _build_system_prompt(tradie: dict, channel: str) -> str:
    biz = (tradie.get("business_name") or "the business").strip()
    trade = (tradie.get("trade_type") or "tradie").strip()
    area = (tradie.get("service_area") or "the local area").strip()

    parts = [f"You are {ASSISTANT_NAME}, the receptionist for {biz}, a {trade} servicing {area}."]

    def add_if(label: str, value) -> None:
        if value and str(value).strip():
            parts.append(f"{label}: {str(value).strip()}")

    add_if("Services we offer", tradie.get("services_offered"))
    add_if("What we don't do", tradie.get("does_not_service"))
    add_if("Hours", tradie.get("hours"))
    add_if("After hours", tradie.get("after_hours_policy"))
    add_if("Callout fee", tradie.get("callout_fee"))
    add_if("Pricing", tradie.get("pricing_notes"))
    if channel == "sms":
        add_if("Booking link", tradie.get("cal_link"))

    parts.append("")
    parts.append(GUARDRAILS)
    parts.append("")
    if channel == "voice":
        parts.append(VOICE_PLAYBOOK)
        parts.append("")
        parts.append(VOICE_INSTRUCTION)
    else:
        parts.append(SMS_PLAYBOOK)
        parts.append("")
        parts.append(SMS_INSTRUCTION)

    return "\n".join(parts)


# ===========================================================================
# Sheets / Anthropic / Twilio clients
# ===========================================================================

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

try:
    anthropic_client = Anthropic()
    log.info("Anthropic client initialised.")
except Exception as exc:
    log.exception("Failed to initialise Anthropic client: %s", exc)
    anthropic_client = None

try:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    log.info("Twilio client initialised.")
except Exception as exc:
    log.exception("Failed to initialise Twilio client: %s", exc)
    twilio_client = None


# ===========================================================================
# Phone normalisation
# ===========================================================================

def _normalise_phone(value) -> str:
    s = str(value).strip().lstrip("'")  # strip gspread apostrophe prefix
    if s and not s.startswith("+") and s.isdigit():
        s = "+" + s
    return s


# ===========================================================================
# Worksheet helpers
# ===========================================================================

def _col_letter(n: int) -> str:
    """Convert a 1-indexed column number to its A1-notation letter.
    1=A, 26=Z, 27=AA, 28=AB, etc. Critical for tabs with >26 columns:
    chr(64+n) breaks at n=27 (produces '[' and beyond)."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _ensure_tab(name: str, headers: list[str]):
    if spreadsheet is None:
        return None
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        log.info("%s tab not found; creating it", name)
        try:
            tab = spreadsheet.add_worksheet(title=name, rows=1000, cols=len(headers))
            end_col = _col_letter(len(headers))
            tab.update(values=[headers], range_name=f"A1:{end_col}1")
            log.info("Created %s tab with %d headers (A1:%s1)", name, len(headers), end_col)
            return tab
        except Exception as exc:
            log.exception("Failed to create %s tab: %s", name, exc)
            return None
    except Exception as exc:
        log.exception("Failed to access %s tab: %s", name, exc)
        return None


# ===========================================================================
# Conversation Log + history
# ===========================================================================

def get_conversation_history(from_number: str, to_number: str) -> list[dict]:
    tab = _ensure_tab(CONVERSATION_LOG_TAB, CONVERSATION_LOG_HEADERS)
    if tab is None:
        return []

    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Conversation Log for history: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HISTORY_HOURS_LIMIT)
    target_from = _normalise_phone(from_number)
    target_to = _normalise_phone(to_number)

    matching = []
    for row in rows:
        row_from = _normalise_phone(row.get("from_number", ""))
        row_to = _normalise_phone(row.get("to_number", ""))
        if row_from != target_from or row_to != target_to:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("timestamp", "")).strip())
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        matching.append(row)

    matching = matching[-HISTORY_TURN_LIMIT:]

    messages = []
    for row in matching:
        msg = str(row.get("message", "")).strip()
        rep = str(row.get("reply", "")).strip()
        if msg and rep:
            messages.append({"role": "user", "content": msg})
            messages.append({"role": "assistant", "content": rep})

    log.info("Loaded %d historical turns for %s -> %s",
             len(messages) // 2, target_from, target_to)
    return messages


def log_conversation(
    business_name: str,
    from_number: str,
    to_number: str,
    message: str,
    reply: str,
    is_urgent: bool,
) -> None:
    tab = _ensure_tab(CONVERSATION_LOG_TAB, CONVERSATION_LOG_HEADERS)
    if tab is None:
        log.error("Cannot log conversation: tab unavailable")
        return

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        tab.append_row(
            [timestamp, business_name, from_number, to_number, message, reply,
             "TRUE" if is_urgent else "FALSE"],
            value_input_option="RAW",
        )
        log.info("Conversation logged (urgent=%s)", is_urgent)
    except Exception as exc:
        log.exception("Failed to append to Conversation Log: %s", exc)


# ===========================================================================
# Mutes
# ===========================================================================

def is_muted(business_name: str, customer_number: str) -> bool:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return False
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Mutes: %s", exc)
        return False
    target_customer = _normalise_phone(customer_number)
    now = datetime.now(timezone.utc)
    for row in rows:
        if str(row.get("business_name", "")).strip() != business_name:
            continue
        if _normalise_phone(row.get("customer_number", "")) != target_customer:
            continue
        try:
            expires = datetime.fromisoformat(str(row.get("expires_at", "")).strip())
        except (ValueError, TypeError):
            continue
        if expires > now:
            return True
    return False


def add_mute(business_name: str, customer_number: str) -> Optional[datetime]:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return None
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=MUTE_HOURS)
    try:
        tab.append_row(
            [now.isoformat(timespec="seconds"), business_name, customer_number,
             expires.isoformat(timespec="seconds")],
            value_input_option="RAW",
        )
        log.info("Mute added: %s for %s until %s", business_name, customer_number, expires)
        return expires
    except Exception as exc:
        log.exception("Failed to add mute: %s", exc)
        return None


def expire_mutes(business_name: str, customer_number: str) -> int:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return 0
    try:
        rows = tab.get_all_values()
    except Exception as exc:
        log.exception("Failed to read Mutes for expiry: %s", exc)
        return 0
    if not rows or len(rows) < 2:
        return 0
    target_customer = _normalise_phone(customer_number)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    count = 0
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) < 4:
            continue
        if str(row[1]).strip() != business_name:
            continue
        if _normalise_phone(row[2]) != target_customer:
            continue
        try:
            expires = datetime.fromisoformat(str(row[3]).strip())
        except (ValueError, TypeError):
            continue
        if expires <= now:
            continue
        try:
            tab.update_cell(idx, 4, now_iso)
            count += 1
        except Exception as exc:
            log.exception("Failed to expire mute on row %d: %s", idx, exc)
    log.info("Expired %d active mute(s) for %s/%s", count, business_name, target_customer)
    return count


# ===========================================================================
# Tradie SMS commands
# ===========================================================================

MUTE_HELP = (
    "Commands:\n"
    "MUTE +614xxxxxxxx — silence Joe for that customer for 24h\n"
    "UNMUTE +614xxxxxxxx — re-enable Joe immediately"
)


def handle_tradie_command(tradie: dict, command_body: str) -> str:
    business = tradie.get("business_name") or "the business"
    parts = command_body.strip().split(maxsplit=1)
    if not parts:
        return MUTE_HELP

    verb = parts[0].upper()
    arg = _normalise_phone(parts[1]) if len(parts) > 1 else ""

    if verb == "MUTE":
        if not arg:
            return f"MUTE needs a customer number, e.g. MUTE +61402585413\n\n{MUTE_HELP}"
        expires = add_mute(business, arg)
        if expires is None:
            return f"Sorry, couldn't apply the mute for {arg}. Try again."
        return (f"Joe muted for {arg} until {expires.isoformat(timespec='minutes')} UTC. "
                f"Their messages will be forwarded to you. Reply UNMUTE {arg} to re-enable.")

    if verb == "UNMUTE":
        if not arg:
            return f"UNMUTE needs a customer number, e.g. UNMUTE +61402585413\n\n{MUTE_HELP}"
        n = expire_mutes(business, arg)
        if n == 0:
            return f"No active mute found for {arg}."
        return f"Joe re-enabled for {arg}. Auto-replies will resume."

    return MUTE_HELP


# ===========================================================================
# Outbound SMS helpers
# ===========================================================================

def _send_sms(from_number: str, to_number: str, body: str) -> bool:
    """Generic outbound SMS via Twilio. Best-effort."""
    if twilio_client is None:
        log.error("Cannot send SMS: Twilio client not initialised")
        return False
    try:
        twilio_client.messages.create(body=body, from_=from_number, to=to_number)
        log.info("SMS sent from %s to %s", from_number, to_number)
        return True
    except Exception as exc:
        log.exception("Failed to send SMS: %s", exc)
        return False


def send_to_owner(tradie: dict, body: str) -> bool:
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    if not owner or not twilio_from:
        log.error("Cannot SMS owner: missing owner_mobile or phone_number")
        return False
    return _send_sms(twilio_from, owner, body)


def send_urgent_alert(tradie: dict, customer_number: str, customer_message: str, joe_reply: str, channel: str = "sms") -> None:
    biz = tradie.get("business_name") or "the business"
    body = (
        f"URGENT ({channel.upper()}) — {biz}\n"
        f"Customer: {customer_number}\n"
        f"Said: {customer_message}\n"
        f"Joe replied: {joe_reply}"
    )
    send_to_owner(tradie, body)


def send_voice_summary(tradie: dict, customer_number: str, transcript_lines: list[str]) -> None:
    biz = tradie.get("business_name") or "the business"
    transcript = "\n".join(transcript_lines)
    body = f"Voice call summary — {biz}\nFrom: {customer_number}\n\n{transcript}"
    send_to_owner(tradie, body)


def send_caller_handoff(tradie: dict, caller_number: str) -> None:
    """After a non-urgent voice call ends, prompt the caller via SMS to text
    their details. The caller will receive the SMS from the same Twilio
    number they just rang, so replying continues naturally with Joe-SMS."""
    biz = tradie.get("business_name") or "the business"
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    if not twilio_from or not caller_number:
        log.error("Cannot send caller handoff: missing phone_number or caller_number")
        return
    body = (
        f"Hi, it's {ASSISTANT_NAME} from {biz}. "
        f"Please reply to this message with your name, suburb, and a brief "
        f"description of the job. The tradie will give you a call back within "
        f"the hour."
    )
    _send_sms(twilio_from, _normalise_phone(caller_number), body)


def send_muted_forward(tradie: dict, customer_number: str, customer_message: str) -> None:
    biz = tradie.get("business_name") or "the business"
    body = f"[Muted — {biz}]\nFrom: {customer_number}\n{customer_message}"
    send_to_owner(tradie, body)


# ===========================================================================
# Tradie lookup
# ===========================================================================

def find_tradie(to_number: str) -> Optional[dict]:
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
        if sheet_phone == target:
            if str(row.get("active", "")).strip().upper() != "TRUE":
                log.info("  matched row %d but active!=TRUE; ignoring", i)
                return None
            log.info("  matched row %d: %r", i, row.get("business_name", ""))
            return row

    log.info("  no row matched")
    return None


# ===========================================================================
# Reply generation
# ===========================================================================

def generate_reply(tradie: dict, user_message: str, history: list[dict], channel: str) -> str:
    business = tradie.get("business_name") or "the business"
    fallback = f"Hi, thanks for messaging {business}. We'll get back to you shortly."
    if channel == "voice":
        fallback = "Sorry, I'm having a bit of trouble. The tradie will call you back shortly."

    if anthropic_client is None:
        log.error("Anthropic client not initialised; using fallback reply")
        return fallback

    system_prompt = _build_system_prompt(tradie, channel)
    messages = history + [{"role": "user", "content": user_message}]

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120 if channel == "voice" else 200,
            system=system_prompt,
            messages=messages,
        )
        text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        reply = "".join(text_parts).strip()
        log.info("Claude %s reply (%d chars): %r", channel, len(reply), reply)
        return reply or fallback
    except Exception as exc:
        log.exception("Anthropic API call failed: %s", exc)
        return fallback


# ===========================================================================
# TwiML builders
# ===========================================================================

def _twiml_message(text: str) -> Response:
    twiml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{xml_escape(text)}</Message></Response>"
    )
    return Response(twiml, mimetype="application/xml")


def _twiml_empty() -> Response:
    twiml = "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"
    return Response(twiml, mimetype="application/xml")


def _voice_say(text: str) -> str:
    return (
        f'<Say voice="{xml_escape(VOICE_NAME)}" language="{xml_escape(VOICE_LANG)}">'
        f'{xml_escape(text)}</Say>'
    )


def _voice_gather(action: str, prompt: Optional[str] = None) -> str:
    inner = _voice_say(prompt) if prompt else ""
    return (
        f'<Gather input="speech" action="{xml_escape(action)}" method="POST" '
        f'speechTimeout="{xml_escape(SPEECH_TIMEOUT_SECONDS)}" '
        f'language="{xml_escape(VOICE_LANG)}" '
        f'speechModel="experimental_conversations">{inner}</Gather>'
    )


def _twiml_voice(*elements: str) -> Response:
    body = "".join(elements)
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"
    return Response(twiml, mimetype="application/xml")


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/", methods=["GET"])
def health() -> str:
    return "Workbench v1.2.0 - alive (Reception + Quotes + Xero invoices + public lead form)"


@app.route("/test", methods=["GET"])
def test() -> str:
    to = request.args.get("to", "")
    if not to:
        return "Pass ?to=+61... to look up a tradie"
    tradie = find_tradie(to)
    if tradie:
        return f"Matched: {tradie.get('business_name', '(no name)')}"
    return f"No tradie matched for {to}"


@app.route("/test/prompt", methods=["GET"])
def test_prompt() -> Response:
    to = request.args.get("to", "")
    channel = request.args.get("channel", "sms")
    if channel not in ("sms", "voice"):
        channel = "sms"
    if not to:
        return Response("Pass ?to=+61...&channel=sms|voice", mimetype="text/plain")
    tradie = find_tradie(to)
    if not tradie:
        return Response(f"No tradie matched for {to}", mimetype="text/plain")
    return Response(_build_system_prompt(tradie, channel), mimetype="text/plain")


@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    sid = request.form.get("MessageSid", "")
    body = request.form.get("Body", "") or ""
    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, sid, body,
    )

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie found in Client tab for To=%s", to_number)
        return _twiml_message("Sorry, this number isn't configured yet.")

    business = tradie.get("business_name", "your tradie")
    log.info("Tradie matched: %s (%s)", business, to_number)

    sender = _normalise_phone(from_number)
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    body_stripped = body.strip()
    body_upper = body_stripped.upper()
    if sender == owner and (body_upper.startswith("MUTE") or body_upper.startswith("UNMUTE")):
        log.info("Tradie command from owner: %r", body_stripped)
        confirmation = handle_tradie_command(tradie, body_stripped)
        return _twiml_message(confirmation)

    if is_muted(business, from_number):
        log.info("Customer %s is muted for %s; forwarding only", from_number, business)
        send_muted_forward(tradie, from_number, body)
        log_conversation(business, from_number, to_number, body, "", False)
        return _twiml_empty()

    history = get_conversation_history(from_number, to_number)
    raw_reply = generate_reply(tradie, body, history, channel="sms")

    is_urgent = URGENT_TAG in raw_reply
    clean_reply = raw_reply.replace(URGENT_TAG, "").replace(END_TAG, "").strip()

    log_conversation(business, from_number, to_number, body, clean_reply, is_urgent)

    if is_urgent:
        send_urgent_alert(tradie, from_number, body, clean_reply, channel="sms")

    return _twiml_message(clean_reply)


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

@app.route("/voice", methods=["POST"])
def voice_incoming() -> Response:
    call_sid = request.form.get("CallSid", "")
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    log.info("Inbound CALL | CallSid=%s From=%s To=%s", call_sid, from_number, to_number)

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie for inbound call To=%s", to_number)
        return _twiml_voice(
            _voice_say("Sorry, this number isn't configured yet. Please try again later."),
            "<Hangup/>",
        )

    business = tradie.get("business_name", "the business")

    with _voice_state_lock:
        _voice_state[call_sid] = {
            "tradie": tradie,
            "from": from_number,
            "to": to_number,
            "history": [],
            "transcript_lines": [],
            "turn_count": 0,
            "ended": False,
        }

    greeting = (
        f"G'day, you've reached {business}. "
        f"I'm {ASSISTANT_NAME}. What can I help you with?"
    )
    return _twiml_voice(
        _voice_gather(action="/voice/turn", prompt=greeting),
        _voice_say("Sorry, I didn't catch that. The tradie will call you back."),
        "<Hangup/>",
    )


@app.route("/voice/turn", methods=["POST"])
def voice_turn() -> Response:
    call_sid = request.form.get("CallSid", "")
    speech = (request.form.get("SpeechResult", "") or "").strip()
    log.info("Voice turn | CallSid=%s SpeechResult=%r", call_sid, speech)

    with _voice_state_lock:
        state = _voice_state.get(call_sid)

    if not state:
        log.warning("No state for CallSid=%s; ending call", call_sid)
        return _twiml_voice(
            _voice_say("Sorry, something went wrong. Please call back."),
            "<Hangup/>",
        )

    tradie = state["tradie"]
    state["turn_count"] += 1

    if not speech:
        if state["turn_count"] < 2:
            return _twiml_voice(
                _voice_gather(action="/voice/turn",
                              prompt="Sorry, I didn't catch that. Could you say that again?"),
                _voice_say("Right, I'll have the tradie call you back. Thanks for ringing."),
                "<Hangup/>",
            )
        else:
            _finalise_call(call_sid, state, ended_reason="silence")
            return _twiml_voice(
                _voice_say("No worries, I'll have the tradie call you back. Thanks for ringing."),
                "<Hangup/>",
            )

    state["transcript_lines"].append(f"Customer: {speech}")
    raw_reply = generate_reply(tradie, speech, state["history"], channel="voice")

    is_urgent = URGENT_TAG in raw_reply
    is_end = END_TAG in raw_reply
    clean_reply = raw_reply.replace(URGENT_TAG, "").replace(END_TAG, "").strip()

    state["transcript_lines"].append(f"Joe: {clean_reply}")
    state["history"].append({"role": "user", "content": speech})
    state["history"].append({"role": "assistant", "content": clean_reply})

    if is_urgent:
        log.info("URGENT detected on call %s — attempting transfer", call_sid)
        owner = _normalise_phone(tradie.get("owner_mobile", ""))
        send_urgent_alert(tradie, state["from"], speech, clean_reply, channel="voice")
        _finalise_call(call_sid, state, ended_reason="urgent_transfer")

        if owner:
            return _twiml_voice(
                _voice_say(clean_reply),
                _voice_say("Putting you through to the tradie now. Hold on."),
                f'<Dial timeout="20" callerId="{xml_escape(_normalise_phone(state["to"]))}">'
                f'{xml_escape(owner)}</Dial>',
                _voice_say("They didn't pick up just then, but they've been alerted by text and will call you straight back."),
                "<Hangup/>",
            )
        else:
            return _twiml_voice(
                _voice_say(clean_reply),
                _voice_say("I've alerted the tradie and they'll call you straight back."),
                "<Hangup/>",
            )

    if is_end or state["turn_count"] >= VOICE_TURN_LIMIT:
        _finalise_call(call_sid, state, ended_reason=("end_tag" if is_end else "turn_limit"))
        return _twiml_voice(
            _voice_say(clean_reply),
            "<Hangup/>",
        )

    return _twiml_voice(
        _voice_gather(action="/voice/turn", prompt=clean_reply),
        _voice_say("Sorry, I didn't catch that. The tradie will call you back."),
        "<Hangup/>",
    )


@app.route("/voice/status", methods=["POST"])
def voice_status() -> Response:
    call_sid = request.form.get("CallSid", "")
    call_status = request.form.get("CallStatus", "")
    log.info("Voice status | CallSid=%s Status=%s", call_sid, call_status)

    with _voice_state_lock:
        state = _voice_state.get(call_sid)

    if state and not state.get("ended"):
        _finalise_call(call_sid, state, ended_reason=f"twilio_status:{call_status}")

    return _twiml_empty()


def _finalise_call(call_sid: str, state: dict, ended_reason: str) -> None:
    if state.get("ended"):
        return
    state["ended"] = True

    tradie = state["tradie"]
    business = tradie.get("business_name", "the business")
    transcript_lines = state["transcript_lines"]
    transcript = "\n".join(transcript_lines)

    log.info("Finalising call %s (reason=%s, turns=%d)",
             call_sid, ended_reason, state["turn_count"])

    if transcript_lines:
        log_conversation(
            business_name=business,
            from_number=state["from"],
            to_number=state["to"],
            message=f"[VOICE CALL]\n{transcript}",
            reply=f"[ended: {ended_reason}]",
            is_urgent=(ended_reason == "urgent_transfer"),
        )
        if ended_reason != "urgent_transfer":
            send_voice_summary(tradie, state["from"], transcript_lines)
            # Auto-SMS the caller asking for details, but delay it (random
            # range) so it feels like a real person sent the text — not a bot
            # firing instantly. Skips if call was urgent (urgent path transfers
            # live).
            delay = random.randint(
                CALLER_HANDOFF_DELAY_MIN_SECONDS,
                CALLER_HANDOFF_DELAY_MAX_SECONDS,
            )
            Timer(
                delay,
                send_caller_handoff,
                args=(tradie, state["from"]),
            ).start()
            log.info(
                "Caller handoff SMS scheduled in %ds for %s",
                delay, state["from"],
            )

    with _voice_state_lock:
        _voice_state.pop(call_sid, None)


# ============================================================================
# ============================================================================
#  WORKBENCH QUOTES
# ============================================================================
# ============================================================================

# ----------------------------------------------------------------------------
# Sheet helpers — Quotes tabs
# ----------------------------------------------------------------------------

def _quotes_tab():
    return _ensure_tab(QUOTES_TAB, QUOTES_HEADERS)

def _items_tab():
    return _ensure_tab(QUOTE_ITEMS_TAB, QUOTE_ITEMS_HEADERS)

def _sessions_tab():
    return _ensure_tab(SESSIONS_TAB, SESSIONS_HEADERS)


def _find_row_index(tab, col_name: str, value: str) -> Optional[int]:
    """Return 1-indexed row number of first matching row (after header), or None."""
    if tab is None:
        return None
    try:
        rows = tab.get_all_records()
        for i, row in enumerate(rows, start=2):
            if str(row.get(col_name, "")).strip() == value:
                return i
    except Exception as exc:
        log.exception("find_row_index failed: %s", exc)
    return None


def _row_to_dict(headers: list[str], row: list) -> dict:
    """Pad row to header length, return dict."""
    padded = list(row) + [""] * (len(headers) - len(row))
    return dict(zip(headers, padded))


# ----------------------------------------------------------------------------
# Quotes — CRUD
# ----------------------------------------------------------------------------

def quote_create(
    tradie: dict,
    customer_name: str,
    customer_phone: str,
    customer_email: str,
    job_description: str,
    items: list[dict],
    tradie_terms: str = "",
) -> Optional[str]:
    """Create a new draft quote + its items. Returns quote_id on success."""
    quotes = _quotes_tab()
    items_tab = _items_tab()
    if quotes is None or items_tab is None:
        log.error("Cannot create quote: tabs unavailable")
        return None

    quote_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    subtotal = sum(float(it.get("quantity", 0)) * float(it.get("unit_price", 0)) for it in items)
    gst = round(subtotal * GST_RATE, 2)
    total = round(subtotal + gst, 2)
    subtotal = round(subtotal, 2)

    quote_row = [
        quote_id,
        _normalise_phone(tradie.get("phone_number", "")),
        tradie.get("business_name", ""),
        customer_name,
        _normalise_phone(customer_phone),
        customer_email,
        job_description,
        subtotal, gst, total,
        "draft",
        now, "", "", "",
        tradie_terms,
        "",
    ]
    try:
        quotes.append_row(quote_row, value_input_option="RAW")
    except Exception as exc:
        log.exception("Failed to append quote: %s", exc)
        return None

    for idx, it in enumerate(items):
        qty = float(it.get("quantity", 0))
        price = float(it.get("unit_price", 0))
        item_row = [
            uuid.uuid4().hex,
            quote_id,
            idx,
            it.get("description", ""),
            qty,
            it.get("unit", ""),
            price,
            round(qty * price, 2),
        ]
        try:
            items_tab.append_row(item_row, value_input_option="RAW")
        except Exception as exc:
            log.exception("Failed to append quote item: %s", exc)

    log.info("Quote created: %s for tradie %s, total $%.2f",
             quote_id, tradie.get("business_name"), total)
    return quote_id


def quote_get(quote_id: str) -> Optional[dict]:
    tab = _quotes_tab()
    if tab is None:
        return None
    try:
        rows = tab.get_all_records()
        for row in rows:
            if str(row.get("quote_id", "")).strip() == quote_id:
                for k in ("subtotal", "gst", "total"):
                    try:
                        row[k] = float(row.get(k, 0) or 0)
                    except (TypeError, ValueError):
                        row[k] = 0.0
                return row
    except Exception as exc:
        log.exception("quote_get failed: %s", exc)
    return None


def quote_items(quote_id: str) -> list[dict]:
    tab = _items_tab()
    if tab is None:
        return []
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("quote_items read failed: %s", exc)
        return []
    matched = []
    for row in rows:
        if str(row.get("quote_id", "")).strip() != quote_id:
            continue
        for k in ("quantity", "unit_price", "line_total"):
            try:
                row[k] = float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                row[k] = 0.0
        try:
            row["line_order"] = int(row.get("line_order", 0) or 0)
        except (TypeError, ValueError):
            row["line_order"] = 0
        matched.append(row)
    matched.sort(key=lambda r: r.get("line_order", 0))
    return matched


def quote_list_for_tradie(tradie_phone: str) -> list[dict]:
    tab = _quotes_tab()
    if tab is None:
        return []
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("quote_list_for_tradie failed: %s", exc)
        return []
    target = _normalise_phone(tradie_phone)
    out = []
    for row in rows:
        if _normalise_phone(row.get("tradie_phone", "")) != target:
            continue
        for k in ("subtotal", "gst", "total"):
            try:
                row[k] = float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                row[k] = 0.0
        out.append(row)
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def quote_update_status(quote_id: str, status: str, timestamp_field: Optional[str] = None) -> bool:
    """Update status; optionally also stamp a timestamp field (sent_at / viewed_at / responded_at)."""
    tab = _quotes_tab()
    if tab is None:
        return False
    row_idx = _find_row_index(tab, "quote_id", quote_id)
    if row_idx is None:
        log.warning("quote_update_status: quote_id %s not found", quote_id)
        return False
    try:
        status_col = QUOTES_HEADERS.index("status") + 1
        tab.update_cell(row_idx, status_col, status)
        if timestamp_field and timestamp_field in QUOTES_HEADERS:
            ts_col = QUOTES_HEADERS.index(timestamp_field) + 1
            tab.update_cell(row_idx, ts_col, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        log.info("Quote %s -> %s", quote_id, status)
        return True
    except Exception as exc:
        log.exception("quote_update_status failed: %s", exc)
        return False


def quote_replace_items(quote_id: str, items: list[dict]) -> bool:
    """Wipe and replace items for a quote. Used when tradie saves edits."""
    tab = _items_tab()
    if tab is None:
        return False
    try:
        # Find all matching item rows and delete them (in reverse to keep indices stable).
        all_rows = tab.get_all_records()
        to_delete = [i + 2 for i, r in enumerate(all_rows)
                     if str(r.get("quote_id", "")).strip() == quote_id]
        for row_idx in sorted(to_delete, reverse=True):
            tab.delete_rows(row_idx)
        # Re-append.
        new_subtotal = 0.0
        for idx, it in enumerate(items):
            qty = float(it.get("quantity", 0))
            price = float(it.get("unit_price", 0))
            line_total = round(qty * price, 2)
            new_subtotal += line_total
            tab.append_row([
                uuid.uuid4().hex, quote_id, idx,
                it.get("description", ""), qty, it.get("unit", ""), price, line_total,
            ], value_input_option="RAW")
        # Update parent quote totals.
        quotes = _quotes_tab()
        row_idx = _find_row_index(quotes, "quote_id", quote_id)
        if row_idx:
            gst = round(new_subtotal * GST_RATE, 2)
            total = round(new_subtotal + gst, 2)
            subtotal_col = QUOTES_HEADERS.index("subtotal") + 1
            gst_col = QUOTES_HEADERS.index("gst") + 1
            total_col = QUOTES_HEADERS.index("total") + 1
            quotes.update_cell(row_idx, subtotal_col, round(new_subtotal, 2))
            quotes.update_cell(row_idx, gst_col, gst)
            quotes.update_cell(row_idx, total_col, total)
        return True
    except Exception as exc:
        log.exception("quote_replace_items failed: %s", exc)
        return False


def quote_update_terms(quote_id: str, terms: str) -> bool:
    tab = _quotes_tab()
    if tab is None:
        return False
    row_idx = _find_row_index(tab, "quote_id", quote_id)
    if row_idx is None:
        return False
    try:
        col = QUOTES_HEADERS.index("tradie_terms") + 1
        tab.update_cell(row_idx, col, terms)
        return True
    except Exception as exc:
        log.exception("quote_update_terms failed: %s", exc)
        return False


# ----------------------------------------------------------------------------
# Auth — magic link via SMS
# ----------------------------------------------------------------------------

def session_create(tradie_phone: str) -> Optional[str]:
    tab = _sessions_tab()
    if tab is None:
        return None
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=SESSION_PENDING_HOURS)
    try:
        tab.append_row([
            token, _normalise_phone(tradie_phone),
            now.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            "pending",
        ], value_input_option="RAW")
        return token
    except Exception as exc:
        log.exception("session_create failed: %s", exc)
        return None


def session_redeem(token: str) -> Optional[str]:
    """Validate a magic-link token. Returns tradie_phone on success.
    Tolerates multiple redemptions within the validity window — SMS apps
    and carriers commonly prefetch URLs for previews, which would otherwise
    consume the token before the user's actual click."""
    tab = _sessions_tab()
    if tab is None:
        return None
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("session_redeem read failed: %s", exc)
        return None
    now = datetime.now(timezone.utc)
    for i, row in enumerate(rows, start=2):
        if str(row.get("token", "")).strip() != token:
            continue
        try:
            expires = datetime.fromisoformat(str(row.get("expires_at", "")).strip())
        except (ValueError, TypeError):
            log.info("session_redeem: bad expires_at: %r", row.get("expires_at"))
            return None
        if expires < now:
            log.info("session_redeem: token expired")
            return None
        # Mark as used (idempotent — re-clicks are fine).
        if str(row.get("status", "")).strip() == "pending":
            try:
                status_col = SESSIONS_HEADERS.index("status") + 1
                tab.update_cell(i, status_col, "used")
            except Exception as exc:
                log.exception("session_redeem status update failed: %s", exc)
        return _normalise_phone(row.get("tradie_phone", ""))
    log.info("session_redeem: token not found")
    return None


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tradie_phone"):
            return redirect(url_for("q_login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    """Make 'user' available in all templates if signed in."""
    phone = session.get("tradie_phone")
    if not phone:
        return {"user": None}
    tradie = find_tradie(phone)
    if not tradie:
        return {"user": None}
    return {"user": {"phone": phone, "business_name": tradie.get("business_name", "")}}


# ----------------------------------------------------------------------------
# LLM — draft line items from tradie description
# ----------------------------------------------------------------------------

QUOTE_DRAFTER_PROMPT = (
    "You are a quote-drafting assistant for an Australian trades business. "
    "Given a job description and the tradie's services/pricing notes, you draft "
    "a list of line items for a customer quote. RULES:\n"
    "- ONLY include items that are reasonable for the described job.\n"
    "- ONLY use pricing that's grounded in the tradie's pricing notes. If a "
    "specific price isn't given, use a sensible estimate based on the notes "
    "(e.g., if hourly rate is $145, multiply by hours for labour lines).\n"
    "- ALWAYS include labour and any obvious materials.\n"
    "- Quantities should be realistic (e.g., 2.5 hours, not 'a few hours').\n"
    "- Units: 'hour', 'each', 'm', 'kg', or leave blank for whole items.\n"
    "- Descriptions should be specific and customer-friendly. No jargon.\n"
    "- Output JSON only, no commentary."
)


def draft_quote_items(tradie: dict, job_description: str) -> list[dict]:
    """Use Claude to draft line items. Returns a list of dicts with
    description / quantity / unit / unit_price."""
    if anthropic_client is None:
        log.error("No Anthropic client for quote drafting; returning empty draft")
        return []

    context_parts = []
    for label, key in [
        ("Trade", "trade_type"),
        ("Services we offer", "services_offered"),
        ("What we don't do", "does_not_service"),
        ("Callout fee", "callout_fee"),
        ("Pricing notes", "pricing_notes"),
    ]:
        val = tradie.get(key)
        if val and str(val).strip():
            context_parts.append(f"{label}: {val}")
    context = "\n".join(context_parts)

    user_msg = (
        f"Tradie context:\n{context}\n\n"
        f"Job description:\n{job_description}\n\n"
        f"Draft line items as a JSON array. Each item: "
        f'{{\"description\": str, \"quantity\": number, \"unit\": str, \"unit_price\": number}}. '
        f"Use AUD ex-GST prices (GST is added separately). Return ONLY the JSON array, no other text."
    )

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=QUOTE_DRAFTER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        # Tolerate the model wrapping JSON in ```json fences.
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        items = json.loads(text)
        cleaned = []
        for it in items:
            try:
                cleaned.append({
                    "description": str(it.get("description", "")).strip(),
                    "quantity": float(it.get("quantity", 0)),
                    "unit": str(it.get("unit", "")).strip(),
                    "unit_price": float(it.get("unit_price", 0)),
                })
            except (TypeError, ValueError):
                continue
        log.info("Drafted %d line items for job: %r", len(cleaned), job_description[:60])
        return cleaned
    except Exception as exc:
        log.exception("draft_quote_items failed: %s", exc)
        return []


# ----------------------------------------------------------------------------
# Outbound SMS — quote-related
# ----------------------------------------------------------------------------

def send_magic_link_sms(tradie: dict, token: str) -> bool:
    """SMS the tradie a one-tap sign-in link from their own Twilio number."""
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    if not twilio_from or not owner:
        return False
    link = f"{PUBLIC_BASE_URL}/q/auth/{token}"
    body = (
        f"Workbench sign-in link (valid 1 hour):\n{link}\n\n"
        f"If you didn't request this, ignore this message."
    )
    return _send_sms(twilio_from, owner, body)


def send_quote_to_customer(tradie: dict, quote: dict) -> bool:
    """SMS the customer the link to view + accept the quote."""
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    to = _normalise_phone(quote.get("customer_phone", ""))
    if not twilio_from or not to:
        return False
    link = f"{PUBLIC_BASE_URL}/quote/{quote['quote_id']}"
    biz = tradie.get("business_name", "your tradie")
    body = (
        f"Hi {quote.get('customer_name', '').split()[0] if quote.get('customer_name') else ''}, "
        f"your quote from {biz} is ready: {link}\n\n"
        f"Reply to this message if you have any questions."
    )
    return _send_sms(twilio_from, to, body)


def notify_tradie_of_response(tradie: dict, quote: dict, accepted: bool) -> bool:
    """SMS the tradie when a customer accepts or declines."""
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    if not twilio_from or not owner:
        return False
    verb = "ACCEPTED" if accepted else "declined"
    body = (
        f"Quote {verb}: {quote.get('customer_name', 'customer')} "
        f"({quote.get('customer_phone', '')}) "
        f"— ${quote.get('total', 0):.2f}.\n"
        f"Ref: {quote['quote_id'][:8].upper()}"
    )
    return _send_sms(twilio_from, owner, body)


# ----------------------------------------------------------------------------
# Routes — tradie portal
# ----------------------------------------------------------------------------

@app.route("/q", methods=["GET"])
def q_root():
    if session.get("tradie_phone"):
        return redirect(url_for("q_dashboard"))
    return redirect(url_for("q_login"))


@app.route("/q/login", methods=["GET", "POST"])
def q_login():
    if request.method == "POST":
        mobile = _normalise_phone(request.form.get("mobile", ""))
        if not mobile:
            flash("Enter a valid mobile number.", "error")
            return redirect(url_for("q_login"))
        # Find tradie by owner_mobile (since they may not know their Twilio number).
        # We look up by either phone_number OR owner_mobile.
        tradie = None
        if spreadsheet is not None:
            try:
                rows = spreadsheet.worksheet("Client").get_all_records()
                for row in rows:
                    if (_normalise_phone(row.get("owner_mobile", "")) == mobile
                            or _normalise_phone(row.get("phone_number", "")) == mobile):
                        if str(row.get("active", "")).strip().upper() == "TRUE":
                            tradie = row
                            break
            except Exception as exc:
                log.exception("q_login lookup failed: %s", exc)
        if not tradie:
            # Generic message — don't confirm or deny existence.
            flash("If that mobile is registered, a sign-in link is on its way.", "info")
            return redirect(url_for("q_login"))
        token = session_create(tradie.get("phone_number", ""))
        if not token:
            flash("Couldn't create a sign-in link. Try again in a minute.", "error")
            return redirect(url_for("q_login"))
        sent = send_magic_link_sms(tradie, token)
        if not sent:
            flash("Couldn't send the sign-in SMS. Contact support.", "error")
            return redirect(url_for("q_login"))
        flash("If that mobile is registered, a sign-in link is on its way.", "info")
        return redirect(url_for("q_login"))
    return render_template("q_login.html")


@app.route("/q/auth/<token>", methods=["GET"])
def q_auth(token):
    tradie_phone = session_redeem(token)
    if not tradie_phone:
        flash("That sign-in link is invalid or expired. Request a new one.", "error")
        return redirect(url_for("q_login"))
    session.permanent = True
    session["tradie_phone"] = tradie_phone
    flash("Signed in.", "success")
    return redirect(url_for("q_dashboard"))


@app.route("/q/logout", methods=["GET"])
def q_logout():
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("q_login"))


@app.route("/q/dashboard", methods=["GET"])
@require_login
def q_dashboard():
    quotes = quote_list_for_tradie(session["tradie_phone"])
    return render_template("q_dashboard.html", quotes=quotes)


@app.route("/q/new", methods=["GET", "POST"])
@require_login
def q_new():
    tradie = find_tradie(session["tradie_phone"])
    if not tradie:
        session.clear()
        flash("Your account couldn't be found. Sign in again.", "error")
        return redirect(url_for("q_login"))

    if request.method == "POST":
        customer_name = request.form.get("customer_name", "").strip()
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_email = request.form.get("customer_email", "").strip()
        job_description = request.form.get("job_description", "").strip()

        if not (customer_name and customer_phone and job_description):
            flash("Customer name, mobile, and job description are required.", "error")
            return render_template("q_new.html")

        items = draft_quote_items(tradie, job_description)
        if not items:
            # Fallback: give the tradie an empty row to fill themselves.
            items = [{"description": "Labour", "quantity": 1.0, "unit": "hour", "unit_price": 0.0}]
            flash("Couldn't auto-draft items. Start from a blank row and edit.", "info")

        quote_id = quote_create(
            tradie=tradie,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            job_description=job_description,
            items=items,
        )
        if not quote_id:
            flash("Couldn't save the quote. Try again.", "error")
            return render_template("q_new.html")
        return redirect(url_for("q_review", quote_id=quote_id))

    return render_template("q_new.html")


@app.route("/q/<quote_id>/review", methods=["GET"])
@require_login
def q_review(quote_id):
    quote = quote_get(quote_id)
    if not quote or _normalise_phone(quote.get("tradie_phone", "")) != session["tradie_phone"]:
        abort(404)
    items = quote_items(quote_id)
    customer_url = f"{PUBLIC_BASE_URL}/quote/{quote_id}"
    return render_template("q_review.html", quote=quote, items=items, customer_url=customer_url)


@app.route("/q/<quote_id>/edit", methods=["POST"])
@require_login
def q_edit(quote_id):
    quote = quote_get(quote_id)
    if not quote or _normalise_phone(quote.get("tradie_phone", "")) != session["tradie_phone"]:
        abort(404)

    # Reassemble items from form fields. Keep existing item order by matching item_id prefix.
    existing = quote_items(quote_id)
    new_items = []
    for it in existing:
        item_id = it["item_id"]
        new_items.append({
            "description": request.form.get(f"desc_{item_id}", it["description"]).strip(),
            "quantity": float(request.form.get(f"qty_{item_id}", it["quantity"]) or 0),
            "unit": request.form.get(f"unit_{item_id}", it["unit"]).strip(),
            "unit_price": float(request.form.get(f"price_{item_id}", it["unit_price"]) or 0),
        })
    quote_replace_items(quote_id, new_items)
    quote_update_terms(quote_id, request.form.get("tradie_terms", "").strip())

    action = request.form.get("action", "save")
    if action == "send" and quote.get("status") == "draft":
        # Refresh quote (totals will have just been recalculated).
        quote = quote_get(quote_id)
        tradie = find_tradie(session["tradie_phone"])
        if tradie and send_quote_to_customer(tradie, quote):
            quote_update_status(quote_id, "sent", timestamp_field="sent_at")
            flash("Quote sent to customer.", "success")
        else:
            flash("Saved, but the SMS didn't go through. Check the customer mobile and try again.", "error")
    else:
        flash("Changes saved.", "success")

    return redirect(url_for("q_review", quote_id=quote_id))


# ----------------------------------------------------------------------------
# Routes — customer-facing
# ----------------------------------------------------------------------------

@app.route("/quote/<quote_id>", methods=["GET"])
def customer_view(quote_id):
    quote = quote_get(quote_id)
    if not quote:
        abort(404)
    # Mark viewed (only first time).
    if quote.get("status") == "sent":
        quote_update_status(quote_id, "viewed", timestamp_field="viewed_at")
        quote["status"] = "viewed"
    items = quote_items(quote_id)
    return render_template("customer_quote.html", quote=quote, items=items)


@app.route("/quote/<quote_id>/accept", methods=["POST"])
def customer_accept(quote_id):
    quote = quote_get(quote_id)
    if not quote:
        abort(404)
    if quote.get("status") in ("accepted", "declined"):
        return redirect(url_for("customer_view", quote_id=quote_id))
    quote_update_status(quote_id, "accepted", timestamp_field="responded_at")
    tradie_phone = _normalise_phone(quote.get("tradie_phone", ""))
    tradie = find_tradie(tradie_phone) if tradie_phone else None
    if tradie:
        notify_tradie_of_response(tradie, quote, accepted=True)
    return redirect(url_for("customer_view", quote_id=quote_id))


@app.route("/quote/<quote_id>/decline", methods=["POST"])
def customer_decline(quote_id):
    quote = quote_get(quote_id)
    if not quote:
        abort(404)
    if quote.get("status") in ("accepted", "declined"):
        return redirect(url_for("customer_view", quote_id=quote_id))
    quote_update_status(quote_id, "declined", timestamp_field="responded_at")
    tradie_phone = _normalise_phone(quote.get("tradie_phone", ""))
    tradie = find_tradie(tradie_phone) if tradie_phone else None
    if tradie:
        notify_tradie_of_response(tradie, quote, accepted=False)
    return redirect(url_for("customer_view", quote_id=quote_id))


# ============================================================================
# ============================================================================
#  XERO INTEGRATION
# ============================================================================
# ============================================================================
#
# Architecture notes:
# - Canonical Invoice model (Workbench-internal) is platform-agnostic.
# - XeroAdapter maps canonical → Xero API. Future MyobAdapter/QbAdapter slot in
#   the same way.
# - Tradie connects via OAuth 2.0. Refresh token stored in Client tab.
# - Quote acceptance does NOT auto-fire invoice creation. Tradie reviews
#   accepted quotes and clicks "Create invoice" — creates a Xero DRAFT.
# - Xero webhook on invoice paid → Workbench updates Invoice status → SMS tradie.
# - No payment data ever stored by Workbench (avoids AFSL territory entirely).

# ----------------------------------------------------------------------------
# Sheet helpers — Invoices, InvoiceLines, extended Client tab
# ----------------------------------------------------------------------------

def _invoices_tab():
    return _ensure_tab(INVOICES_TAB, INVOICES_HEADERS)

def _invoice_lines_tab():
    return _ensure_tab(INVOICE_LINES_TAB, INVOICE_LINES_HEADERS)


# Client tab gets extended columns N-V. They may not exist on legacy sheets.
# We address columns by header name so missing columns are tolerated — empty
# values are returned. To write, we look up the column index dynamically.

def _client_tab():
    if spreadsheet is None:
        return None
    try:
        return spreadsheet.worksheet("Client")
    except Exception as exc:
        log.exception("Failed to access Client tab: %s", exc)
        return None


def _client_col_index(field_name: str) -> Optional[int]:
    """Look up the 1-indexed column number for a Client tab header.
    Returns None if not present. The first row of the Client tab is the
    header row."""
    tab = _client_tab()
    if tab is None:
        return None
    try:
        headers = tab.row_values(1)
        for i, h in enumerate(headers, start=1):
            if h.strip() == field_name:
                return i
    except Exception as exc:
        log.exception("_client_col_index failed: %s", exc)
    return None


def client_update_field(tradie_phone: str, field_name: str, value: str) -> bool:
    """Update a single field on a tradie's Client row, addressing by header name.
    Creates the column if it doesn't exist yet."""
    tab = _client_tab()
    if tab is None:
        return False
    target = _normalise_phone(tradie_phone)
    try:
        # Find the row
        rows = tab.get_all_records()
        row_idx = None
        for i, row in enumerate(rows, start=2):
            if _normalise_phone(row.get("phone_number", "")) == target:
                row_idx = i
                break
        if row_idx is None:
            log.warning("client_update_field: tradie %s not found", target)
            return False

        # Find or create the column
        col_idx = _client_col_index(field_name)
        if col_idx is None:
            # Append a new header to the end of row 1
            headers = tab.row_values(1)
            col_idx = len(headers) + 1
            tab.update_cell(1, col_idx, field_name)
            log.info("Added new Client column %d: %s", col_idx, field_name)

        tab.update_cell(row_idx, col_idx, value)
        return True
    except Exception as exc:
        log.exception("client_update_field failed: %s", exc)
        return False


def client_get_field(tradie_phone: str, field_name: str) -> str:
    """Read a field from the tradie's row, returns empty string if missing."""
    tab = _client_tab()
    if tab is None:
        return ""
    target = _normalise_phone(tradie_phone)
    try:
        rows = tab.get_all_records()
        for row in rows:
            if _normalise_phone(row.get("phone_number", "")) == target:
                return str(row.get(field_name, "")).strip()
    except Exception as exc:
        log.exception("client_get_field failed: %s", exc)
    return ""


# ----------------------------------------------------------------------------
# Canonical Invoice — CRUD
# ----------------------------------------------------------------------------

def invoice_create_from_quote(quote: dict, items: list[dict], tradie: dict) -> Optional[str]:
    """Create a Workbench Invoice row from an accepted quote. Returns invoice_id.
    Does NOT push to Xero yet — that's a separate step (so we can preview before send)."""
    inv_tab = _invoices_tab()
    line_tab = _invoice_lines_tab()
    if inv_tab is None or line_tab is None:
        log.error("Cannot create invoice: tabs unavailable")
        return None

    invoice_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    issue_date = datetime.now(timezone.utc).date().isoformat()
    payment_terms = int(client_get_field(tradie.get("phone_number", ""),
                                          "accounting_payment_terms_days") or "14")
    due_date = (datetime.now(timezone.utc) + timedelta(days=payment_terms)).date().isoformat()

    subtotal = float(quote.get("subtotal", 0))
    tax_total = float(quote.get("gst", 0))
    total = float(quote.get("total", 0))

    inv_row = [
        invoice_id,
        quote.get("quote_id", ""),
        _normalise_phone(tradie.get("phone_number", "")),
        "",  # external_invoice_id — filled after push to Xero
        client_get_field(tradie.get("phone_number", ""), "accounting_platform") or "xero",
        "",  # external_url
        "",  # invoice_number
        "draft",
        quote.get("customer_name", ""),
        quote.get("customer_email", ""),
        quote.get("customer_phone", ""),
        "",  # customer_external_id
        "AUD",
        issue_date,
        due_date,
        round(subtotal, 2),
        round(tax_total, 2),
        round(total, 2),
        0.0,
        round(total, 2),
        "",  # paid_date
        payment_terms,
        f"Workbench Quote {quote.get('quote_id', '')[:8].upper()}",
        "",  # notes_internal
        quote.get("tradie_terms", ""),  # notes_customer
        client_get_field(tradie.get("phone_number", ""), "accounting_default_account") or "200",
        client_get_field(tradie.get("phone_number", ""), "accounting_default_tax_treatment") or "Exclusive",
        "",  # branding_theme_id
        "",  # tracking_categories
        now,
        now,
        "",  # last_sync_at
        "pending",
    ]
    try:
        inv_tab.append_row(inv_row, value_input_option="RAW")
    except Exception as exc:
        log.exception("Failed to append invoice: %s", exc)
        return None

    # Copy line items
    for idx, it in enumerate(items):
        qty = float(it.get("quantity", 0))
        price = float(it.get("unit_price", 0))
        line_row = [
            uuid.uuid4().hex,
            invoice_id,
            idx,
            it.get("description", ""),
            qty,
            it.get("unit", ""),
            price,
            "OUTPUT",  # Xero default GST 10% — verify per tradie's chart of accounts
            "",  # account_code per-line override
            "",  # tracking 1
            "",  # tracking 2
            round(qty * price, 2),
        ]
        try:
            line_tab.append_row(line_row, value_input_option="RAW")
        except Exception as exc:
            log.exception("Failed to append invoice line: %s", exc)

    log.info("Invoice created: %s from quote %s", invoice_id, quote.get("quote_id"))
    return invoice_id


def invoice_get(invoice_id: str) -> Optional[dict]:
    tab = _invoices_tab()
    if tab is None:
        return None
    try:
        rows = tab.get_all_records()
        for row in rows:
            if str(row.get("invoice_id", "")).strip() == invoice_id:
                for k in ("subtotal", "tax_total", "total", "amount_paid", "amount_due"):
                    try:
                        row[k] = float(row.get(k, 0) or 0)
                    except (TypeError, ValueError):
                        row[k] = 0.0
                return row
    except Exception as exc:
        log.exception("invoice_get failed: %s", exc)
    return None


def invoice_lines(invoice_id: str) -> list[dict]:
    tab = _invoice_lines_tab()
    if tab is None:
        return []
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("invoice_lines read failed: %s", exc)
        return []
    matched = []
    for row in rows:
        if str(row.get("invoice_id", "")).strip() != invoice_id:
            continue
        for k in ("quantity", "unit_price", "line_total"):
            try:
                row[k] = float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                row[k] = 0.0
        try:
            row["line_order"] = int(row.get("line_order", 0) or 0)
        except (TypeError, ValueError):
            row["line_order"] = 0
        matched.append(row)
    matched.sort(key=lambda r: r.get("line_order", 0))
    return matched


def invoice_list_for_tradie(tradie_phone: str) -> list[dict]:
    tab = _invoices_tab()
    if tab is None:
        return []
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("invoice_list_for_tradie failed: %s", exc)
        return []
    target = _normalise_phone(tradie_phone)
    out = []
    for row in rows:
        if _normalise_phone(row.get("tradie_phone", "")) != target:
            continue
        for k in ("subtotal", "tax_total", "total", "amount_paid", "amount_due"):
            try:
                row[k] = float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                row[k] = 0.0
        out.append(row)
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def invoice_update_fields(invoice_id: str, updates: dict) -> bool:
    """Update one or more fields on an invoice row by header name."""
    tab = _invoices_tab()
    if tab is None:
        return False
    row_idx = _find_row_index(tab, "invoice_id", invoice_id)
    if row_idx is None:
        return False
    try:
        for field, value in updates.items():
            if field in INVOICES_HEADERS:
                col = INVOICES_HEADERS.index(field) + 1
                tab.update_cell(row_idx, col, value)
        # Touch updated_at
        if "updated_at" not in updates:
            col = INVOICES_HEADERS.index("updated_at") + 1
            tab.update_cell(row_idx, col,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return True
    except Exception as exc:
        log.exception("invoice_update_fields failed: %s", exc)
        return False


# ----------------------------------------------------------------------------
# InvoiceAdapter — base class. Every accounting platform implements this.
# ----------------------------------------------------------------------------

class InvoiceAdapter:
    @classmethod
    def name(cls) -> str:
        raise NotImplementedError

    def connect_start_url(self, tradie: dict, state: str) -> str:
        raise NotImplementedError

    def connect_callback(self, tradie: dict, code: str) -> bool:
        """Exchange OAuth code for tokens, save to Client tab."""
        raise NotImplementedError

    def ensure_contact(self, invoice: dict, tradie: dict) -> Optional[str]:
        """Find-or-create the customer in the platform. Returns external_id."""
        raise NotImplementedError

    def create_invoice(self, invoice: dict, lines: list[dict], tradie: dict) -> Optional[dict]:
        """Push invoice to platform. Returns {external_id, external_url, invoice_number}."""
        raise NotImplementedError

    def fetch_status(self, invoice: dict, tradie: dict) -> Optional[dict]:
        """Pull current status from platform."""
        raise NotImplementedError

    def verify_webhook(self, signature: str, body: bytes) -> bool:
        raise NotImplementedError


# ----------------------------------------------------------------------------
# XeroAdapter — implements InvoiceAdapter for Xero
# ----------------------------------------------------------------------------

class XeroAdapter(InvoiceAdapter):
    @classmethod
    def name(cls) -> str:
        return "xero"

    # ---- OAuth ----

    def connect_start_url(self, tradie: dict, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": XERO_CLIENT_ID,
            "redirect_uri": f"{PUBLIC_BASE_URL}/q/connect/xero/callback",
            "scope": XERO_SCOPES,
            "state": state,
        }
        return f"{XERO_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def connect_callback(self, tradie: dict, code: str) -> bool:
        # Exchange code → tokens
        try:
            r = requests.post(
                XERO_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{PUBLIC_BASE_URL}/q/connect/xero/callback",
                },
                auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
                timeout=15,
            )
            r.raise_for_status()
            tokens = r.json()
        except Exception as exc:
            log.exception("Xero token exchange failed: %s", exc)
            return False

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = int(tokens.get("expires_in", 1800))
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=expires_in - 60)).isoformat(timespec="seconds")

        # Get tenant ID (Xero "organisation" the user authorised)
        try:
            r = requests.get(
                XERO_CONNECTIONS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            r.raise_for_status()
            connections = r.json()
        except Exception as exc:
            log.exception("Xero connections fetch failed: %s", exc)
            return False

        if not connections:
            log.error("Xero connections returned empty list")
            return False
        tenant_id = connections[0].get("tenantId", "")

        # Store on tradie row
        phone = tradie.get("phone_number", "")
        client_update_field(phone, "accounting_platform", "xero")
        client_update_field(phone, "accounting_external_id", tenant_id)
        client_set_secret(phone, "accounting_refresh_token", refresh_token)
        client_set_secret(phone, "accounting_access_token", access_token)
        client_update_field(phone, "accounting_token_expires", expires_at)
        client_update_field(phone, "service_invoices", "TRUE")
        log.info("Xero connected for tradie %s, tenant %s", phone, tenant_id)
        return True

    # ---- Token refresh ----

    def _access_token(self, tradie: dict) -> Optional[str]:
        """Get a valid Xero access token, refreshing if needed."""
        phone = tradie.get("phone_number", "")
        access = client_get_secret(phone, "accounting_access_token")
        expires_str = client_get_field(phone, "accounting_token_expires")
        refresh = client_get_secret(phone, "accounting_refresh_token")

        # If access is non-empty and not expired, use it
        if access and expires_str:
            try:
                expires = datetime.fromisoformat(expires_str)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires > datetime.now(timezone.utc):
                    return access
            except (ValueError, TypeError):
                pass

        # Otherwise refresh
        if not refresh:
            log.error("No refresh token for tradie %s", phone)
            return None
        try:
            r = requests.post(
                XERO_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh},
                auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
                timeout=15,
            )
            r.raise_for_status()
            tokens = r.json()
        except Exception as exc:
            log.exception("Xero token refresh failed: %s", exc)
            return None

        new_access = tokens.get("access_token")
        new_refresh = tokens.get("refresh_token", refresh)
        expires_in = int(tokens.get("expires_in", 1800))
        new_expires = (datetime.now(timezone.utc)
                       + timedelta(seconds=expires_in - 60)).isoformat(timespec="seconds")
        client_set_secret(phone, "accounting_access_token", new_access)
        client_set_secret(phone, "accounting_refresh_token", new_refresh)
        client_update_field(phone, "accounting_token_expires", new_expires)
        return new_access

    def _headers(self, tradie: dict) -> Optional[dict]:
        tok = self._access_token(tradie)
        if not tok:
            return None
        tenant = client_get_field(tradie.get("phone_number", ""), "accounting_external_id")
        return {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Xero-tenant-id": tenant,
        }

    # ---- Contact sync ----

    def ensure_contact(self, invoice: dict, tradie: dict) -> Optional[str]:
        headers = self._headers(tradie)
        if not headers:
            return None
        name = invoice.get("customer_name", "")
        email = invoice.get("customer_email", "")
        phone = invoice.get("customer_phone", "")

        # Search by name first (Xero's where filter)
        try:
            params = {"where": f'Name=="{name}"'} if name else {}
            r = requests.get(f"{XERO_API_BASE}/Contacts",
                             headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            contacts = data.get("Contacts", [])
            if contacts:
                return contacts[0].get("ContactID")
        except Exception as exc:
            log.exception("Xero contact search failed: %s", exc)

        # Create
        payload = {"Contacts": [{
            "Name": name or f"Customer-{uuid.uuid4().hex[:6]}",
            "EmailAddress": email,
            "Phones": [{"PhoneType": "MOBILE", "PhoneNumber": phone}] if phone else [],
        }]}
        try:
            r = requests.post(f"{XERO_API_BASE}/Contacts",
                              headers=headers, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            contacts = data.get("Contacts", [])
            if contacts:
                return contacts[0].get("ContactID")
        except Exception as exc:
            log.exception("Xero contact create failed: %s", exc)
        return None

    # ---- Invoice creation ----

    def create_invoice(self, invoice: dict, lines: list[dict], tradie: dict) -> Optional[dict]:
        headers = self._headers(tradie)
        if not headers:
            return None

        contact_id = self.ensure_contact(invoice, tradie)
        if not contact_id:
            log.error("Could not get Xero contact for invoice %s", invoice.get("invoice_id"))
            return None

        tax_treatment = invoice.get("tax_treatment") or "Exclusive"
        line_items = []
        for ln in lines:
            line_items.append({
                "Description": ln.get("description", ""),
                "Quantity": float(ln.get("quantity", 0)),
                "UnitAmount": float(ln.get("unit_price", 0)),
                "AccountCode": ln.get("account_code") or invoice.get("account_code", "200"),
                "TaxType": ln.get("tax_rate_code") or "OUTPUT",
            })

        payload = {"Invoices": [{
            "Type": "ACCREC",   # Accounts Receivable (sales invoice)
            "Contact": {"ContactID": contact_id},
            "Date": invoice.get("issue_date"),
            "DueDate": invoice.get("due_date"),
            "Reference": invoice.get("reference", ""),
            "LineAmountTypes": tax_treatment,
            "LineItems": line_items,
            "Status": "DRAFT",
        }]}

        try:
            r = requests.post(f"{XERO_API_BASE}/Invoices",
                              headers=headers, json=payload, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.exception("Xero invoice create failed: %s", exc)
            return None

        inv = (data.get("Invoices") or [{}])[0]
        external_id = inv.get("InvoiceID", "")
        invoice_number = inv.get("InvoiceNumber", "")
        tenant = client_get_field(tradie.get("phone_number", ""), "accounting_external_id")
        external_url = f"https://go.xero.com/AccountsReceivable/Edit.aspx?InvoiceID={external_id}"

        return {
            "external_id": external_id,
            "external_url": external_url,
            "invoice_number": invoice_number,
            "customer_external_id": contact_id,
        }

    # ---- Status sync ----

    def fetch_status(self, invoice: dict, tradie: dict) -> Optional[dict]:
        ext_id = invoice.get("external_invoice_id", "")
        if not ext_id:
            return None
        headers = self._headers(tradie)
        if not headers:
            return None
        try:
            r = requests.get(f"{XERO_API_BASE}/Invoices/{ext_id}",
                             headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.exception("Xero invoice fetch failed: %s", exc)
            return None
        inv = (data.get("Invoices") or [{}])[0]
        status_map = {
            "DRAFT": "draft", "SUBMITTED": "draft", "AUTHORISED": "sent",
            "PAID": "paid", "VOIDED": "void", "DELETED": "void",
        }
        return {
            "status": status_map.get(inv.get("Status", ""), "draft"),
            "amount_paid": float(inv.get("AmountPaid", 0) or 0),
            "amount_due": float(inv.get("AmountDue", 0) or 0),
            "total": float(inv.get("Total", 0) or 0),
        }

    # ---- Webhook signature ----

    def verify_webhook(self, signature: str, body: bytes) -> bool:
        if not XERO_WEBHOOK_KEY:
            log.warning("XERO_WEBHOOK_KEY not configured; rejecting webhook")
            return False
        digest = hmac.new(XERO_WEBHOOK_KEY.encode(),
                          body, hashlib.sha256).digest()
        import base64
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(signature, expected)


# Adapter registry — future MyobAdapter, QbAdapter slot in here
ADAPTERS = {
    "xero": XeroAdapter(),
}


def get_adapter(platform: str) -> Optional[InvoiceAdapter]:
    return ADAPTERS.get(platform.lower())


def get_adapter_for_tradie(tradie: dict) -> Optional[InvoiceAdapter]:
    platform = client_get_field(tradie.get("phone_number", ""), "accounting_platform") or "xero"
    return get_adapter(platform)


# ----------------------------------------------------------------------------
# Outbound SMS — invoice notifications
# ----------------------------------------------------------------------------

def notify_tradie_invoice_created(tradie: dict, invoice: dict) -> bool:
    """SMS the tradie when invoice draft is created in Xero."""
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    if not twilio_from or not owner:
        return False
    inv_num = invoice.get("invoice_number") or invoice.get("invoice_id", "")[:8].upper()
    body = (
        f"Invoice {inv_num} drafted in Xero — ${invoice.get('total', 0):.2f} "
        f"for {invoice.get('customer_name', 'customer')}. "
        f"Review and send: {invoice.get('external_url', '(see Xero)')}"
    )
    return _send_sms(twilio_from, owner, body)


def notify_tradie_invoice_paid(tradie: dict, invoice: dict) -> bool:
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    if not twilio_from or not owner:
        return False
    inv_num = invoice.get("invoice_number") or invoice.get("invoice_id", "")[:8].upper()
    body = (
        f"PAID: Invoice {inv_num} — ${invoice.get('amount_paid', 0):.2f} "
        f"from {invoice.get('customer_name', 'customer')}."
    )
    return _send_sms(twilio_from, owner, body)


# ----------------------------------------------------------------------------
# Routes — Xero OAuth
# ----------------------------------------------------------------------------

@app.route("/q/connect/xero", methods=["GET"])
@require_login
def q_xero_connect():
    if not XERO_CLIENT_ID or not XERO_CLIENT_SECRET:
        flash("Xero is not configured on this server yet. Contact support.", "error")
        return redirect(url_for("q_dashboard"))

    tradie = find_tradie(session["tradie_phone"])
    if not tradie:
        return redirect(url_for("q_login"))

    # CSRF state — short-lived, in-memory
    state = secrets.token_urlsafe(24)
    with _xero_oauth_state_lock:
        _xero_oauth_state[state] = {
            "tradie_phone": session["tradie_phone"],
            "expires": time.time() + 600,  # 10 min
        }

    adapter = get_adapter("xero")
    url = adapter.connect_start_url(tradie, state)
    return redirect(url)


@app.route("/q/connect/xero/callback", methods=["GET"])
def q_xero_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    err = request.args.get("error", "")

    if err:
        log.warning("Xero callback error: %s", err)
        flash(f"Xero connection failed: {err}", "error")
        return redirect(url_for("q_dashboard"))

    if not code or not state:
        flash("Xero connection failed: missing code or state.", "error")
        return redirect(url_for("q_dashboard"))

    # Validate state
    with _xero_oauth_state_lock:
        entry = _xero_oauth_state.pop(state, None)
    if not entry:
        flash("Xero connection failed: invalid or expired state. Try again.", "error")
        return redirect(url_for("q_dashboard"))
    if entry["expires"] < time.time():
        flash("Xero connection timed out. Try again.", "error")
        return redirect(url_for("q_dashboard"))

    tradie_phone = entry["tradie_phone"]
    tradie = find_tradie(tradie_phone)
    if not tradie:
        flash("Account not found.", "error")
        return redirect(url_for("q_login"))

    adapter = get_adapter("xero")
    if adapter.connect_callback(tradie, code):
        flash("Connected to Xero. You can now create invoices.", "success")
    else:
        flash("Xero connection failed. Check the server logs and try again.", "error")
    return redirect(url_for("q_dashboard"))


@app.route("/q/connect/xero/disconnect", methods=["POST"])
@require_login
def q_xero_disconnect():
    phone = session["tradie_phone"]
    for field in ("accounting_platform", "accounting_external_id",
                  "accounting_access_token", "accounting_refresh_token",
                  "accounting_token_expires", "service_invoices"):
        client_update_field(phone, field, "")
    flash("Disconnected from Xero.", "info")
    return redirect(url_for("q_dashboard"))


# ----------------------------------------------------------------------------
# Routes — Invoice management
# ----------------------------------------------------------------------------

@app.route("/q/<quote_id>/invoice", methods=["POST"])
@require_login
def q_create_invoice(quote_id):
    """Create a draft invoice from an accepted quote."""
    quote = quote_get(quote_id)
    if not quote or _normalise_phone(quote.get("tradie_phone", "")) != session["tradie_phone"]:
        abort(404)
    if quote.get("status") != "accepted":
        flash("Only accepted quotes can be turned into invoices.", "error")
        return redirect(url_for("q_review", quote_id=quote_id))

    tradie = find_tradie(session["tradie_phone"])
    if not tradie:
        return redirect(url_for("q_login"))

    # Check that Xero is connected
    if not client_get_field(session["tradie_phone"], "accounting_refresh_token"):
        flash("Connect your Xero account first.", "info")
        return redirect(url_for("q_dashboard"))

    items = quote_items(quote_id)
    invoice_id = invoice_create_from_quote(quote, items, tradie)
    if not invoice_id:
        flash("Could not create invoice locally. Try again.", "error")
        return redirect(url_for("q_review", quote_id=quote_id))

    # Push to Xero
    inv = invoice_get(invoice_id)
    inv_lines = invoice_lines(invoice_id)
    adapter = get_adapter_for_tradie(tradie)
    result = adapter.create_invoice(inv, inv_lines, tradie)

    if not result:
        invoice_update_fields(invoice_id, {
            "status": "failed",
            "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_sync_status": "failed",
        })
        flash("Invoice created locally but Xero push failed. Check connection and retry.", "error")
        return redirect(url_for("q_invoice_view", invoice_id=invoice_id))

    invoice_update_fields(invoice_id, {
        "external_invoice_id": result["external_id"],
        "external_url": result["external_url"],
        "invoice_number": result["invoice_number"],
        "customer_external_id": result.get("customer_external_id", ""),
        "status": "draft",   # Xero draft — tradie still has to send it from Xero
        "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_sync_status": "ok",
    })

    # Notify tradie
    updated = invoice_get(invoice_id)
    if updated:
        notify_tradie_invoice_created(tradie, updated)

    flash("Invoice drafted in Xero. Review and send from your Xero account.", "success")
    return redirect(url_for("q_invoice_view", invoice_id=invoice_id))


@app.route("/q/invoices", methods=["GET"])
@require_login
def q_invoices_list():
    invoices = invoice_list_for_tradie(session["tradie_phone"])
    connected = bool(client_get_field(session["tradie_phone"], "accounting_refresh_token"))
    return render_template("q_invoices.html", invoices=invoices, xero_connected=connected)


@app.route("/q/invoices/<invoice_id>", methods=["GET"])
@require_login
def q_invoice_view(invoice_id):
    invoice = invoice_get(invoice_id)
    if not invoice or _normalise_phone(invoice.get("tradie_phone", "")) != session["tradie_phone"]:
        abort(404)
    lines = invoice_lines(invoice_id)
    return render_template("q_invoice.html", invoice=invoice, lines=lines)


@app.route("/q/invoices/<invoice_id>/resync", methods=["POST"])
@require_login
def q_invoice_resync(invoice_id):
    """Manually pull current status from Xero. For debugging / status check."""
    invoice = invoice_get(invoice_id)
    if not invoice or _normalise_phone(invoice.get("tradie_phone", "")) != session["tradie_phone"]:
        abort(404)
    tradie = find_tradie(session["tradie_phone"])
    if not tradie:
        return redirect(url_for("q_login"))
    adapter = get_adapter_for_tradie(tradie)
    status = adapter.fetch_status(invoice, tradie)
    if status:
        updates = {
            "status": status["status"],
            "amount_paid": status["amount_paid"],
            "amount_due": status["amount_due"],
            "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_sync_status": "ok",
        }
        if status["status"] == "paid":
            updates["paid_date"] = datetime.now(timezone.utc).date().isoformat()
        invoice_update_fields(invoice_id, updates)
        flash(f"Status: {status['status']}, paid ${status['amount_paid']:.2f}, due ${status['amount_due']:.2f}", "info")
    else:
        flash("Could not fetch status from Xero.", "error")
    return redirect(url_for("q_invoice_view", invoice_id=invoice_id))


# ----------------------------------------------------------------------------
# Routes — Webhooks
# ----------------------------------------------------------------------------

@app.route("/invoices/webhook/xero", methods=["POST"])
def xero_webhook():
    """Xero POSTs here when invoices change (paid, voided, etc.).
    Signed with HMAC-SHA256. We verify, then queue status refreshes."""
    sig = request.headers.get("x-xero-signature", "")
    body = request.get_data()

    adapter = get_adapter("xero")
    if not adapter.verify_webhook(sig, body):
        log.warning("Xero webhook signature verification failed")
        return Response(status=401)

    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return Response(status=400)

    events = data.get("events", [])
    log.info("Xero webhook: %d events", len(events))

    # Each event references a tenant + resource ID. We look up the invoice
    # by external_invoice_id and pull current status.
    inv_tab = _invoices_tab()
    if inv_tab is None:
        return Response(status=200)  # acknowledge so Xero stops retrying
    try:
        all_invoices = inv_tab.get_all_records()
    except Exception:
        return Response(status=200)

    for evt in events:
        resource_id = evt.get("resourceId", "")
        if not resource_id:
            continue
        # Find matching local invoice
        matched = None
        for inv in all_invoices:
            if str(inv.get("external_invoice_id", "")).strip() == resource_id:
                matched = inv
                break
        if not matched:
            log.info("Webhook for unknown invoice %s; ignoring", resource_id)
            continue

        tradie_phone = _normalise_phone(matched.get("tradie_phone", ""))
        tradie = find_tradie(tradie_phone)
        if not tradie:
            continue
        adapter = get_adapter_for_tradie(tradie)
        status = adapter.fetch_status(matched, tradie)
        if not status:
            continue
        was_paid = matched.get("status") == "paid"
        invoice_update_fields(matched.get("invoice_id", ""), {
            "status": status["status"],
            "amount_paid": status["amount_paid"],
            "amount_due": status["amount_due"],
            "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_sync_status": "ok",
            "paid_date": (datetime.now(timezone.utc).date().isoformat()
                          if status["status"] == "paid" and not matched.get("paid_date")
                          else matched.get("paid_date", "")),
        })
        # SMS tradie on first transition to paid
        if status["status"] == "paid" and not was_paid:
            updated = invoice_get(matched.get("invoice_id", ""))
            if updated:
                notify_tradie_invoice_paid(tradie, updated)

    return Response(status=200)


# ===========================================================================
# LEADS — public form endpoint for workbenchhq.org/start
# ===========================================================================

# Simple in-memory rate limiter for the public form. Survives only between
# Render restarts but that's fine — purpose is to slow down a script not to
# build a fortress. Keyed by IP. Allows 3 submissions per hour per IP.
_lead_rate: dict[str, list[float]] = {}
_lead_rate_lock = Lock()

def _lead_rate_limited(ip: str) -> bool:
    if not ip:
        return False
    now = time.time()
    window = 3600  # 1 hour
    limit = 3
    with _lead_rate_lock:
        history = [t for t in _lead_rate.get(ip, []) if now - t < window]
        if len(history) >= limit:
            _lead_rate[ip] = history
            return True
        history.append(now)
        _lead_rate[ip] = history
        return False


def _cors_origin(request_obj) -> Optional[str]:
    """Return the request Origin if it's allowed, otherwise None."""
    origin = request_obj.headers.get("Origin", "")
    if origin in ALLOWED_LEAD_ORIGINS:
        return origin
    return None


def _cors_headers(origin: Optional[str]) -> dict:
    if not origin:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def _format_lead_notify_sms(name: str, mobile: str, email: str,
                            business: str, trade: str, location: str,
                            notes: str, lead_id: str) -> str:
    lines = [
        f"NEW LEAD — {trade}",
        f"{name} at {business}",
        f"Mobile: {mobile}",
    ]
    if email:
        lines.append(f"Email: {email}")
    if location:
        lines.append(f"Where: {location}")
    if notes:
        snippet = notes if len(notes) <= 120 else notes[:117] + "..."
        lines.append(f"Notes: {snippet}")
    lines.append(f"ID: {lead_id[:8]}")
    return "\n".join(lines)


def _format_lead_auto_reply(name: str) -> str:
    first = name.split()[0] if name else "there"
    return (
        f"Hi {first} — thanks for your interest in Workbench. "
        f"Sacha (the founder) will text you back within 24 hours. "
        f"While you wait, try Joe yourself: text or call +61485067607 "
        f"and pretend you're a customer of a Sunshine Coast plumber. "
        f"That's the same engine that would run for your business."
    )


@app.route("/api/lead", methods=["POST", "OPTIONS"])
def api_lead():
    """Public endpoint for the workbenchhq.org/start signup form."""
    origin = _cors_origin(request)
    cors = _cors_headers(origin)

    if request.method == "OPTIONS":
        return Response(status=204, headers=cors)

    # If the origin isn't whitelisted, decline without leaking detail.
    if not origin:
        log.warning("api_lead: blocked origin %r", request.headers.get("Origin"))
        return Response(status=403)

    # Honeypot field — bots fill every input
    if (request.form.get("hp_company") or "").strip():
        log.info("api_lead: honeypot triggered, silently accepted")
        return jsonify({"ok": True, "lead_id": "honeypot"}), 200, cors

    # Rate limit per IP
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _lead_rate_limited(ip):
        log.warning("api_lead: rate limited %s", ip)
        return jsonify({"error": "Too many requests. Please try again later."}), 429, cors

    # Extract + sanity-check fields
    name = (request.form.get("name") or "").strip()[:120]
    mobile_raw = (request.form.get("mobile") or "").strip()[:32]
    email = (request.form.get("email") or "").strip()[:160]
    business = (request.form.get("business") or "").strip()[:160]
    trade = (request.form.get("trade") or "").strip()[:120]
    location = (request.form.get("location") or "").strip()[:160]
    notes = (request.form.get("notes") or "").strip()[:1000]
    source = (request.form.get("source") or "website-form").strip()[:60]

    if not name or not mobile_raw or not business or not trade:
        return jsonify({"error": "Missing required fields"}), 400, cors

    mobile = _normalise_phone(mobile_raw)
    if not mobile.startswith("+61") or len(mobile) < 11:
        return jsonify({"error": "Please enter a valid Australian mobile."}), 400, cors

    # Light email shape check (only if provided)
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        return jsonify({"error": "Please enter a valid email address."}), 400, cors

    # Write to Sheet
    lead_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ua = (request.headers.get("User-Agent") or "")[:240]
    row = [
        lead_id, created_at, source,
        name, mobile, email, business, trade, location,
        notes, "new", "", "",
        ua, ip,
    ]
    leads_tab = _ensure_tab(LEADS_TAB, LEADS_HEADERS)
    if leads_tab is None:
        log.error("api_lead: Leads tab unavailable")
        return jsonify({"error": "Server temporarily unavailable"}), 503, cors
    try:
        leads_tab.append_row(row, value_input_option="USER_ENTERED")
        log.info("api_lead: lead saved %s (%s, %s)", lead_id[:8], name, trade)
    except Exception as exc:
        log.exception("api_lead: failed to append lead row: %s", exc)
        return jsonify({"error": "Server error"}), 500, cors

    # Notify Sacha via SMS
    try:
        notify_body = _format_lead_notify_sms(name, mobile, email, business, trade,
                                              location, notes, lead_id)
        _send_sms(LEAD_FROM_NUMBER, LEAD_NOTIFY_NUMBER, notify_body)
    except Exception as exc:
        log.exception("api_lead: failed to notify owner: %s", exc)
        # Don't fail the request — the lead is in the Sheet

    # Auto-reply to prospect
    try:
        _send_sms(LEAD_FROM_NUMBER, mobile, _format_lead_auto_reply(name))
    except Exception as exc:
        log.exception("api_lead: failed to send auto-reply: %s", exc)
        # Don't fail the request — Sacha will follow up manually

    return jsonify({"ok": True, "lead_id": lead_id}), 200, cors
