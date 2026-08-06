"""
Push notification delivery via Expo's push service.

Mpola's app is an Expo app, so this skips FCM/APNs credential setup entirely —
Expo's push endpoint accepts a device's Expo push token and relays it to the
right platform. No third-party account needed beyond what Expo already
provides for free. Production standalone builds still need EAS push
credentials configured on the Expo side; this code path is correct either way.
"""

import requests

from logging_module import logger

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_expo_push(push_token: str, title: str, body: str, data: dict | None = None) -> bool:
    """Best-effort — a failed push should never block the caller."""
    if not push_token or not push_token.startswith(("ExponentPushToken", "ExpoPushToken")):
        return False
    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json={
                "to": push_token,
                "title": title,
                "body": body,
                "data": data or {},
                "sound": "default",
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.error(f"Expo push send failed [{resp.status_code}]: {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Expo push send error: {e}")
        return False
