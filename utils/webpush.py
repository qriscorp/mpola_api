"""
Browser push notification delivery via the standard Web Push protocol
(VAPID-signed), for mpola_website. This is the desktop/browser counterpart
to utils/push.py's Expo push for mobile — a subscribed browser gets a real
OS-level notification (Windows Action Center / macOS Notification Center)
even when no tab is open, the same way Android/iOS push works for the app.
"""

import json

from pywebpush import webpush, WebPushException

from config import VAPID_PRIVATE_KEY, VAPID_SUBJECT
from logging_module import logger


def send_web_push(
    endpoint: str,
    p256dh: str,
    auth: str,
    title: str,
    body: str,
    data: dict | None = None,
    urgent: bool = False,
) -> bool:
    """Best-effort — a failed push should never block the caller. Returns
    False (instead of raising) on a 404/410 from the push service, which
    means the subscription has expired or been revoked by the browser —
    the caller should delete that WebPushSubscription row when this
    happens, same as a dead Expo token."""
    if not VAPID_PRIVATE_KEY:
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps({
                "title": title,
                "body": body,
                "data": data or {},
                "urgent": urgent,
            }),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=60 * 60 * 24 if urgent else 60 * 30,
        )
        return True
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        if status in (404, 410):
            raise  # signals the caller to delete this dead subscription
        logger.error(f"Web push send failed [{status}]: {e}")
        return False
    except Exception as e:
        logger.error(f"Web push send error: {e}")
        return False
