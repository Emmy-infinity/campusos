"""
Pesapal API v3 client for CampusOS.

CHANGELOG
---------
- Added _normalize_billing(): guarantees every field Pesapal's
  billing_address schema expects is present, non-empty, and (for name
  and address fields) restricted to Pesapal's allowed charset
  [A-Za-z0-9-_.:].

  Pesapal's validator on this merchant tier rejects `null` AND `""`
  for EVERY field — including ones documented as optional. It checks
  them in order and returns the first failure. So we now fill every
  field with a non-empty placeholder:

    middle_name  → "NA"
    line_2       → "NA"
    state        → "NA"
    postal_code  → "00000"
    zip_code     → "00000"

- First/middle/last name and address fields are sanitized to strip
  any char outside [A-Za-z0-9-_.:] — e.g. "O'Brien" → "OBrien",
  "Mary Ann" → "MaryAnn", "N/A" → "NA".
- submit_order() logs the outgoing payload so failures show the exact
  billing_address.
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
# Pesapal's validator on this merchant tier requires EVERY billing
# field to be a **non-empty string**, and name/address fields to
# contain only [A-Za-z0-9-_.:]. We always send every field with a
# non-empty value, sanitizing as needed.
_PESAPAL_BILLING_DEFAULTS = {
    "email_address": "",
    "phone_number": "",
    "country_code": "UG",
    "first_name": "Student",
    "middle_name": "NA",       # "/" not in allowed charset
    "last_name": "User",
    "line_1": "Kampala",
    "line_2": "NA",            # was "" — Pesapal rejects empty
    "city": "Kampala",
    "state": "NA",             # was "" — Pesapal rejects empty
    "postal_code": "00000",    # was "" — Pesapal rejects empty
    "zip_code": "00000",       # was "" — Pesapal rejects empty
}

# Pesapal's allowed charset for name + address fields.
_NAME_CHARSET = re.compile(r"[^A-Za-z0-9\-_.:]")

# Fields that get sanitized with the strict charset.
_NAME_FIELDS = ("first_name", "middle_name", "last_name")
_ADDRESS_FIELDS = ("line_1", "line_2", "city", "state")
_POSTAL_FIELDS = ("postal_code", "zip_code")


def _sanitize(value, pattern):
    """Strip characters not in `pattern` and trim whitespace."""
    if not value:
        return ""
    return pattern.sub("", str(value)).strip()


def _normalize_billing(billing):
    """
    Fill every field Pesapal's billing_address schema expects, sanitize
    to Pesapal's allowed charset, and apply non-empty fallbacks for
    every field the validator checks.

    Guarantees:
      - every key in _PESAPAL_BILLING_DEFAULTS exists in the output
      - every value is a string (never None)
      - every value is non-empty
      - name + address fields contain only [A-Za-z0-9-_.:]
    """
    merged = {**_PESAPAL_BILLING_DEFAULTS, **(billing or {})}
    out = {k: ("" if v is None else str(v)) for k, v in merged.items()}

    # Sanitize name + address fields to Pesapal's charset.
    # "O'Brien" → "OBrien", "Mary Ann" → "MaryAnn", "N/A" → "NA"
    for field in _NAME_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)
    for field in _ADDRESS_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)
    for field in _POSTAL_FIELDS:
        out[field] = _sanitize(out[field], _NAME_CHARSET)

    # Fallbacks — Pesapal's validator rejects "" on this tier, so
    # every empty field gets a placeholder. Keep this in sync with
    # _PESAPAL_BILLING_DEFAULTS so a partial caller dict is still safe.
    if not out["email_address"]:
        out["email_address"] = "noemail@campusos.local"
    if not out["phone_number"]:
        out["phone_number"] = "0700000000"
    if not out["country_code"]:
        out["country_code"] = "UG"
    if not out["first_name"]:
        out["first_name"] = "Student"
    if not out["middle_name"]:
        out["middle_name"] = "NA"
    if not out["last_name"]:
        out["last_name"] = "User"
    if not out["line_1"]:
        out["line_1"] = "Kampala"
    if not out["line_2"]:
        out["line_2"] = "NA"
    if not out["city"]:
        out["city"] = "Kampala"
    if not out["state"]:
        out["state"] = "NA"
    if not out["postal_code"]:
        out["postal_code"] = "00000"
    if not out["zip_code"]:
        out["zip_code"] = "00000"

    return out


def _base_url() -> str:
    return settings.PESAPAL_BASE_URL.rstrip("/")


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        snippet = resp.text[:300].replace("\n", " ")
        raise PesapalError(
            f"Pesapal returned an invalid response (HTTP {resp.status_code}): {snippet}",
            status_code=resp.status_code,
        )


def get_access_token(force_refresh=False):
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
        _cached_token_expiry = now + 240
        return token


def _auth_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_access_token()}",
    }


def register_ipn_url(ipn_url):
    url = f"{_base_url()}/URLSetup/RegisterIPN"
    payload = {"url": ipn_url, "ipn_notification_type": "POST"}
    try:
        resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
        data = _safe_json(resp)
    except requests.RequestException as exc:
        logger.exception("Pesapal IPN registration failed: %s", exc)
        raise PesapalError(f"Could not register IPN: {exc}")

    ipn_id = data.get("ipn_id")
    if not ipn_id:
        raise PesapalError(f"No ipn_id in Pesapal response: {data}", payload=data)
    logger.info("Pesapal IPN registered: %s (id=%s)", ipn_url, ipn_id)
    return ipn_id


def list_registered_ipns():
    url = f"{_base_url()}/URLSetup/GetIpnList"
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
        return _safe_json(resp)
    except requests.RequestException as exc:
        raise PesapalError(f"Could not list IPNs: {exc}")


def submit_order(*, merchant_reference, amount, currency, description,
                 callback_url, notification_id, billing):
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
        resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=20)
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


def get_transaction_status(order_tracking_id):
    url = f"{_base_url()}/Transactions/GetTransactionStatus"
    params = {"orderTrackingId": order_tracking_id}
    try:
        resp = requests.get(url, params=params, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
        return _safe_json(resp)
    except requests.RequestException as exc:
        raise PesapalError(f"Could not query status: {exc}")
