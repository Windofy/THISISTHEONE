"""
Distributed production guards for paid AI routes.

Uses Firestore when ENABLE_DISTRIBUTED_GUARDS=true. Falls back to local process
state when disabled so local development keeps working.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

try:
    from google.cloud import firestore
except Exception:  # pragma: no cover - optional production dependency
    firestore = None


_CLIENT = None


def enabled() -> bool:
    return os.getenv("ENABLE_DISTRIBUTED_GUARDS", "false").lower() == "true"


def _client():
    global _CLIENT
    if _CLIENT is None:
        if firestore is None:
            raise RuntimeError("Firestore dependency is not available.")
        _CLIENT = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
    return _CLIENT


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _collection_name() -> str:
    return os.getenv("GUARD_COLLECTION", "windofy_runtime_guards")


def check_rate_limit(*, route: str, client_ip: str, limit: int, window_seconds: int) -> bool:
    """
    Returns True when the request is allowed, False when it should be blocked.
    Firestore stores fixed-window counters per route/IP/window.
    """
    if not enabled():
        return True

    now = int(datetime.now(timezone.utc).timestamp())
    bucket = now // max(1, window_seconds)
    doc_id = f"rate:{route.strip('/')}:{client_ip}:{bucket}"
    doc_ref = _client().collection(_collection_name()).document(doc_id)

    @firestore.transactional
    def txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        count = int((snap.to_dict() or {}).get("count", 0)) if snap.exists else 0
        if count >= limit:
            return False
        transaction.set(
            doc_ref,
            {
                "kind": "rate",
                "route": route,
                "clientIp": client_ip,
                "bucket": bucket,
                "count": count + 1,
                "expiresAt": now + window_seconds + 3600,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True

    return bool(txn(_client().transaction()))


def check_daily_cap(*, name: str, limit: Optional[int], units: int = 1) -> bool:
    """
    Returns True when today's paid-call budget has room, False when exhausted.
    """
    if not enabled() or not limit or limit <= 0:
        return True

    today = _today()
    doc_ref = _client().collection(_collection_name()).document(f"budget:{today}:{name}")

    @firestore.transactional
    def txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        used = int((snap.to_dict() or {}).get("used", 0)) if snap.exists else 0
        if used + units > limit:
            return False
        transaction.set(
            doc_ref,
            {
                "kind": "budget",
                "name": name,
                "date": today,
                "used": used + units,
                "limit": limit,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True

    return bool(txn(_client().transaction()))
