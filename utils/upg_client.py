"""HTTP client for the Unified Payment Gateway (UPG).

All payment operations in mpola_api route through UPG instead of
calling Interswitch directly. UPG is hosted at UPG_BASE_URL.

Required env vars:
    UPG_BASE_URL    - e.g. https://api.unified.qriscorp.com
    UPG_API_KEY     - project API key issued by UPG admin
    UPG_PROJECT_ID  - e.g. "mpola"
"""
import os
import uuid
import requests
from typing import Any, Dict, Optional

import logging
logger = logging.getLogger(__name__)

UPG_BASE_URL = os.getenv("UPG_BASE_URL", "https://api.unified.qriscorp.com")
UPG_API_KEY = os.getenv("UPG_API_KEY")
UPG_PROJECT_ID = os.getenv("UPG_PROJECT_ID", "mpola")


def _detect_carrier(phone: str) -> str:
    """Auto-detect MTN or AIRTEL from Ugandan phone number prefix."""
    p = phone.strip()
    if p.startswith(("0077", "0078", "0076", "0079", "077", "078", "076", "079")):
        return "MTN"
    if p.startswith(("0075", "0074", "0070", "075", "074", "070")):
        return "AIRTEL"
    return "MTN"  # fallback


class UPGClient:
    """Synchronous HTTP client for the Unified Payment Gateway."""

    def __init__(self) -> None:
        self.base_url = UPG_BASE_URL.rstrip("/")
        self.api_key = UPG_API_KEY
        self.project_id = UPG_PROJECT_ID

        if not self.api_key:
            raise ValueError("UPG_API_KEY environment variable must be set")
        if not self.project_id:
            raise ValueError("UPG_PROJECT_ID environment variable must be set")

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        h: Dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Project-ID": self.project_id,
            "Content-Type": "application/json",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        if response.status_code >= 400:
            raise Exception(
                f"UPG request failed [{response.status_code}]: {response.text[:500]}"
            )
        return response.json()

    @staticmethod
    def _new_idempotency_key() -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def collect(
        self,
        amount: float,
        phone: str,
        carrier: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Collect (payin) from a customer's mobile money wallet."""
        if not idempotency_key:
            idempotency_key = self._new_idempotency_key()
        url = f"{self.base_url}/v1/collect"
        payload: Dict[str, Any] = {
            "amount": str(int(amount)),
            "currency": "UGX",
            "sender": {
                "phone": phone,
                "carrier": carrier.lower(),
            },
        }
        logger.info(f"UPG collect: amount={amount}, carrier={carrier}, phone={phone}")
        response = requests.post(url, json=payload, headers=self._headers(idempotency_key), timeout=30)
        return self._handle_response(response)

    def disburse(
        self,
        amount: float,
        phone: str,
        carrier: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Disburse (payout) to a customer's mobile money wallet."""
        if not idempotency_key:
            idempotency_key = self._new_idempotency_key()
        url = f"{self.base_url}/v1/disburse"
        payload: Dict[str, Any] = {
            "amount": str(int(amount)),
            "currency": "UGX",
            "recipient": {
                "phone": phone,
                "carrier": carrier.lower(),
            },
        }
        logger.info(f"UPG disburse: amount={amount}, carrier={carrier}, phone={phone}")
        response = requests.post(url, json=payload, headers=self._headers(idempotency_key), timeout=30)
        return self._handle_response(response)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_success(result: Dict[str, Any]) -> bool:
        return result.get("status") == "success"

    @staticmethod
    def transaction_id(result: Dict[str, Any]) -> str:
        return result.get("transaction_id", "")
