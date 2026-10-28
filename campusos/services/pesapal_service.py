"""
Pesapal API v3 client for CampusOS.

Handles:
  - OAuth token generation (cached; Pesapal tokens live 5 minutes)
  - IPN URL registration (one-time setup per tunnel / deployment)
  - Order submission (returns the redirect URL for the student)
  - Transaction status queries (used to verify IPNs server-side)
"""
import logging
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PesapalError(Exception):
    """Raised when Pesapal returns an error or the network fails."""


# ─── Token cache ─────────────────────────────────────────────────
_token_lock = threading.Lock()
_cached_token = None
_cached_token_expiry = 0


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
            f"Pesapal returned an invalid response (HTTP {resp.status_code}): {snippet}"
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
            raise PesapalError(f"Pesapal auth returned no token: {data}")

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
        raise PesapalError(f"No ipn_id in Pesapal response: {data}")
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
    """
    url = f"{_base_url()}/Transactions/SubmitOrderRequest"
    payload = {
        "id": merchant_reference,
        "currency": currency,
        "amount": float(amount),
        "description": description,
        "callback_url": callback_url,
        "notification_id": notification_id,
        "billing_address": billing,
    }
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
        err = data['error']
        err_msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
        raise PesapalError(f"Pesapal rejected order: {err_msg}")
        
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
