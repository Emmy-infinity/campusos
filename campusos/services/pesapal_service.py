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
  This fixes "Middle Name cannot be null or empty" — the field is
  documented as optional, but Pesapal's validator rejects both `null`
  and `""` on some merchant tiers. We send "N/A" as a placeholder when
  the caller doesn't provide one.
- submit_order() now runs billing through _normalize_billing() before
  building the request, so callers can pass partial dicts (or dicts
  with None values) without triggering Pesapal's strict validation.
- PesapalError now stores `payload` and `status_code` when available,
  so error handlers can log Pesapal's exact response instead of just
  the extracted message. This makes the `payload=%r` in views.py useful.
- submit_order() logs the outgoing payload at INFO level, so the next
  time Pesapal rejects an order, the exact billing_address is in the
  log — not just the error message.
"""
import json
import logging
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


# ─── Billing normalizer ──────────────────────────────────────────
# Pesapal's billing_address schema requires every field to be a
# **non-empty string**. Documented as "optional" for middle_name,
# state, postal_code, zip_code — but the validator rejects `null`
# AND `""` for them on some merchant tiers. We always send a value.
_PESAPAL_BILLING_DEFAULTS = {
    "email_address": "",
    "phone_number": "",
    "country_code": "UG",
    "first_name": "",
    "middle_name": "N/A",   # ← must be non-empty; "" also fails
    "last_name": "",
    "line_1": "",           # note: `line_1`, not `line1`
    "line_2": "",
    "city": "",
    "state": "",
    "postal_code": "",
    "zip_code": "",
}

# Fields that MUST be non-empty. If the caller passes "" or None,
# we substitute this placeholder. middle_name is the one that
# triggered "cannot be null or empty" in production.
_BILLING_NON_EMPTY_PLACEHOLDER = "N/A"


def _normalize_billing(billing):
    """
    Fill every field Pesapal's billing_address schema expects and coerce
    None/empty values to safe strings.

    Guarantees:
      - Every key in _PESAPAL_BILLING_DEFAULTS exists in the output.
      - Every value is a string (never None).
      - middle_name is non-empty (Pesapal rejects "" too).

    Callers can therefore pass partial dicts or dicts with None values
    without triggering 'X cannot be null or empty' errors.
    """
    merged = {**_PESAPAL_BILLING_DEFAULTS, **(billing or {})}
    out = {k: ("" if v is None else str(v)) for k, v in merged.items()}
    # Pesapal rejects empty strings for middle_name. Force a placeholder
    # if the caller didn't supply one.
    if not out["middle_name"]:
        out["middle_name"] = _BILLING_NON_EMPTY_PLACEHOLDER
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
        # Truncate the HTML response to avoid gigantic error logs
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

    The billing dict is normalized through _normalize_billing() before the
    request goes out, so partial dicts / None values / empty strings are
    safe — Pesapal's validator never sees `null` or `""` for a required
    field.
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

    # Diagnostic — if Pesapal rejects the order, this log line shows the
    # exact payload that went out (including middle_name value), so we
    # can tell "our code sent the wrong thing" apart from "Pesapal's
    # validator is broken".
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
        # Pesapal usually returns error as a dict with 'message' or 'code'
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
