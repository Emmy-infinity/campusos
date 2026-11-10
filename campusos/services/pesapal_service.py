"""
Pesapal API v3 client for CampusOS.

Handles:
  - OAuth token generation (cached; Pesapal tokens live 5 minutes)
  - IPN URL registration (one-time setup per tunnel / deployment)
  - Order submission (returns the redirect URL for the student)
  - Transaction status queries (used to verify IPNs server-side)

CHANGELOG
---------
- Added _normalize_billing(): guarantees every field Pesapal's
  billing_address schema expects is present AND is a non-empty string.
- Name fields (first/middle/last) are sanitized to Pesapal's allowed
  charset: A-Z a-z 0-9 - _ . :
  Anything else (spaces, apostrophes, slashes, commas) is stripped.
  This fixes:
    "Middle Name cannot be null or empty"
    "Middle Name can only contain alphanumeric characters, dashes,
     underscores, dots, and colons"
- middle_name fallback is "NA" (no slash — "/" is not in the allowed
  charset, so "N/A" would fail).
- submit_order() logs the outgoing payload so failures show the exact
  billing_address that went out.
- PesapalError stores `status_code` and `payload` for richer error logs.
"""
import json
import logging
import re
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PesapalError(Exception):
    """Raised when Pesapal returns an error or the network fails."""

    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


# ─── Token cache ─────────────────────────────────────────────────
_token_lock = threading.Lock()
_cached_token = None
_cached_token_expiry = 0


# ─── Billing normalizer & sanitizer ─────────────────────────────
# Pesapal's billing_address schema:
#   - every field must be a **non-empty string**
#   - name fields accept only:  A-Z a-z 0-9 - _ . :
#   - middle_name rejects `null` AND `""` on some merchant tiers
#
# We always send every field with a value and strip disallowed chars.
_PESAPAL_BILLING_DEFAULTS = {
    "email_address": "",
    "phone_number": "",
    "country_code": "UG",
    "first_name": "",
    "middle_name": "NA",    # no slash — "/" is not in the charset
    "last_name": "",
    "line_1": "",           # note: `line_1`, not `line1`
    "line_2": "",
    "city": "",
    "state": "",
    "postal_code": "",
    "zip_code": "",
}

# Pesapal rejects chars outside this set for name fields.
_NAME_CHARSET = re.compile(r"[^A-Za-z0-9\-_.:]")

# Name fields that must be sanitized with the strict charset.
_NAME_FIELDS = ("first_name", "middle_name", "last_name")

# Address fields — currently we sanitize with the same charset to be safe.
# If Pesapal is more permissive for addresses, we can relax this later.
_ADDRESS_FIELDS = ("line_1", "line_2", "city", "state")

# Postal/zip — alphanumeric only.
_POSTAL_FIELDS = ("postal_code", "zip_code")


def _sanitize(value: str, pattern: re.Pattern) -> str:
    """Strip characters not in `pattern` and trim whitespace."""
    if not value:
        return ""
    return pattern.sub("", str(value)).strip()


def _normalize_billing(billing):
    """
    Fill every field Pesapal's billing_address schema expects, coerce
    None → "", sanitize string fields to Pesapal's charset rules, and
    apply non-empty fallbacks.

    Guarantees:
      - every key in _PESAPAL_BILLING_DEFAULTS exists in the output
      - every value is a string (never None)
      - name fields contain only [A-Za-z0-9-_.:]
      - middle_name is non-empty
      - line_1, city are non-empty
    """
    merged = {**_PESAPAL_BILLING_DEFAULTS, **(billing or {})}
    out = {k: ("" if v is None else str(v)) for k, v in merged.items()}

    # Sanitize name fields to Pesapal's strict charset.
    # "Mary Ann" → "MaryAnn", "O'Brien" → "OBrien", "N/A" → "NA".
    for field in _NAME_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)

    # Address fields — same charset rule, to avoid the next round of
    # "line_1 can only contain ..." errors.
    for field in _ADDRESS_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)

    # Postal / zip — alphanumeric only.
    for field in _POSTAL_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)

    # Fallbacks for mandatory non-empty fields.
    if not out["middle_name"]:
        out["middle_name"] = "NA"           # "/" not allowed
    if not out["first_name"]:
        out["first_name"] = "Student"
    if not out["last_name"]:
        out["last_name"] = "User"
    if not out["line_1"]:
        out["line_1"] = "Kampala"
    if not out["city"]:
        out["city"] = "Kampala"

    return out


def _base_url() -> str:
    return settings.PESAPAL_BASE_URL.rstrip("/")


def _safe_json(resp: requests.Response) -> dict:
    """
    Safely parse JSON. If Pesapal returns an HTML error page (e.g., 500 or 404),
    this catches the JSONDecodeError and raises a clean PesapalError.
    """
    try:
        return resp.json()
    except ValueError:
        snippet = resp.text[:300].replace("\n", " ")
        raise PesapalError(
            f"Pesapal returned an invalid response (HTTP {resp.status_code}): {snippet}",
            status_code=resp.status_code,
        )


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid Pesapal bearer token, cached until just before expiry."""
    global _cached_token, _cached_token_expiry

    with _token_lock:
        now = time.time()
        if (not force_refresh and _cached_token
                and now < _cached_token_expiry):
            return _cached_token

        url = f"{_base_url()}/Auth/RequestToken"
        payload = {
            "consumer_key": settings.PESAPAL_CONSUMER_KEY,
            "consumer_secret": settings.PESAPAL_CONSUMER_SECRET,
        }
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json"}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = _safe_json(resp)
        except requests.RequestException as exc:
            logger.exception("Pesapal auth failed: %s", exc)
            raise PesapalError(f"Could not authenticate with Pesapal: {exc}")

        token = data.get("token")
        if not token:
            raise PesapalError(
                f"Pesapal auth returned no token: {data}",
                payload=data,
            )

        _cached_token = token
        _cached_token_expiry = now + 240  # safe margin under 5 min
        return token


def _auth_headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_access_token()}",
    }


# ─── IPN registration ────────────────────────────────────────────

def register_ipn_url(ipn_url: str) -> str:
    """Register (or re-register) our IPN URL. Returns the ipn_id."""
    url = f"{_base_url()}/URLSetup/RegisterIPN"
    payload = {"url": ipn_url, "ipn_notification_type": "POST"}
    try:
        resp = requests.post(url, json=payload, headers=_auth_headers(),
                             timeout=15)
        resp.raise_for_status()
        data = _safe_json(resp)
    except requests.RequestException as exc:
        logger.exception("Pesapal IPN registration failed: %s", exc)
        raise PesapalError(f"Could not register IPN: {exc}")

    ipn_id = data.get("ipn_id")
    if not ipn_id:
        raise PesapalError(
            f"No ipn_id in Pesapal response: {data}",
            payload=data,
        )
    logger.info("Pesapal IPN registered: %s (id=%s)", ipn_url, ipn_id)
    return ipn_id


def list_registered_ipns() -> list:
    url = f"{_base_url()}/URLSetup/GetIpnList"
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
        return _safe_json(resp)
    except requests.RequestException as exc:
        raise PesapalError(f"Could not list IPNs: {exc}")


# ─── Orders ──────────────────────────────────────────────────────

def submit_order(*, merchant_reference, amount, currency, description,
                 callback_url, notification_id, billing) -> dict:
    """
    Submit an order to Pesapal. Returns:
      { order_tracking_id, merchant_reference, redirect_url, error, status }

    The billing dict is normalized + sanitized through _normalize_billing()
    before the request goes out, so partial dicts / None values /
    disallowed characters are all made safe before they reach Pesapal.
    """
    url = f"{_base_url()}/Transactions/SubmitOrderRequest"
    payload = {
        "id": merchant_reference,
        "currency": currency,
        "amount": float(amount),
        "description": description,
        "callback_url": callback_url,
        "notification_id": notification_id,
        "billing_address": _normalize_billing(billing),
    }

    logger.info(
        "Pesapal SubmitOrder payload for %s: %s",
        merchant_reference, json.dumps(payload, default=str),
    )

    try:
        resp = requests.post(url, json=payload, headers=_auth_headers(),
                             timeout=20)
        resp.raise_for_status()
        data = _safe_json(resp)
    except requests.RequestException as exc:
        logger.exception("Pesapal submit_order failed: %s", exc)
        raise PesapalError(f"Could not submit order: {exc}")

    if data.get("error"):
        err = data["error"]
        err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise PesapalError(
            f"Pesapal rejected order: {err_msg}",
            status_code=resp.status_code,
            payload=data,
        )

    return data


def get_transaction_status(order_tracking_id: str) -> dict:
    """Query Pesapal for a transaction's current status."""
    url = f"{_base_url()}/Transactions/GetTransactionStatus"
    params = {"orderTrackingId": order_tracking_id}
    try:
        resp = requests.get(url, params=params, headers=_auth_headers(),
                            timeout=15)
        resp.raise_for_status()
        return _safe_json(resp)
    except requests.RequestException as exc:
        raise PesapalError(f"Could not query status: {exc}")
