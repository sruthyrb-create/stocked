"""Ring Partner API (Amazon Vision API) integration.

* ``verify_signature`` / ``parse_webhook``: Ring webhook v1.1 payloads, signed with
  HMAC-SHA256 over the raw request body, sent as ``X-Signature: sha256=<hex>``.
* ``RingClient``: server-to-server REST client for https://api.amazonvision.com with the
  required ``User-Agent`` and ``Hardware_Id`` headers, refresh-token rotation, 429 back-off,
  device discovery, Event History and image snapshots (303 -> pre-signed URL, fetched
  *without* the bearer token).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.amazonvision.com"
OAUTH = "https://oauth.ring.com/oauth/token"
USER_AGENT = "Stocked/1.0"

# Motion sub-types Ring uses for parcel activity. Ring may add values; unknown ones pass through.
PACKAGE_SUBTYPES = {"package_delivery", "package", "package_dropoff"}
PICKUP_SUBTYPES = {"package_pickup", "package_removed"}


# ----------------------------------------------------------------------------- webhooks


def sign(body: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, key: str) -> bool:
    if not header or not key:
        return False
    return hmac.compare_digest(sign(body, key), header.strip())


@dataclass
class RingEvent:
    request_id: str
    type: str  # motion_detected, button_press, device_online, ...
    sub_type: str  # human, package_delivery, vehicle, ... ("" if none)
    device_id: str
    timestamp_ms: int
    account_id: str
    raw: dict

    @property
    def is_package_delivery(self) -> bool:
        return self.type == "motion_detected" and self.sub_type in PACKAGE_SUBTYPES

    @property
    def is_package_pickup(self) -> bool:
        return self.type == "motion_detected" and self.sub_type in PICKUP_SUBTYPES


def parse_webhook(body: bytes) -> RingEvent:
    payload = json.loads(body)
    meta, data = payload.get("meta", {}), payload.get("data", {})
    attrs = data.get("attributes", {}) or {}
    # The docs show both `data.subType` and `attributes.sub_type`; accept either.
    sub_type = data.get("subType") or data.get("sub_type") or attrs.get("sub_type") or attrs.get("subType") or ""
    ts = attrs.get("timestamp") or int(time.time() * 1000)
    return RingEvent(
        request_id=str(meta.get("request_id") or uuid.uuid4()),
        type=str(data.get("type", "")),
        sub_type=str(sub_type),
        device_id=str(attrs.get("source") or ""),
        timestamp_ms=int(ts),
        account_id=str(meta.get("account_id") or ""),
        raw=payload,
    )


def build_webhook(event_type: str, device_id: str, sub_type: str = "", account_id: str = "ava1.ring.account.demo",
                  ts_ms: int | None = None) -> dict:
    """A v1.1 payload in Ring's documented shape (used by the simulator and tests)."""
    ts_ms = ts_ms or int(time.time() * 1000)
    data: dict[str, Any] = {
        "id": f"{device_id}_{sub_type or event_type}_{ts_ms}",
        "type": event_type,
        "attributes": {"source": device_id, "source_type": "devices", "timestamp": ts_ms},
        "relationships": {"devices": {"links": {"self": f"/v1/devices/{device_id}"}}},
    }
    if sub_type:
        data["subType"] = sub_type
        data["attributes"]["sub_type"] = sub_type
    return {"meta": {"version": "1.1", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "request_id": str(uuid.uuid4()), "account_id": account_id}, "data": data}


# ----------------------------------------------------------------------------- REST client


class RingAuthError(RuntimeError):
    pass


class RingClient:
    """Use either a short-lived access token (Developer Playground) or a refresh token + client credentials."""

    def __init__(self, access_token: str | None = None, refresh_token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None, hardware_id: str | None = None,
                 base_url: str = API, transport: httpx.AsyncBaseTransport | None = None):
        self.access_token = access_token or os.getenv("RING_ACCESS_TOKEN")
        self.refresh_token = refresh_token or os.getenv("RING_REFRESH_TOKEN")
        self.client_id = client_id or os.getenv("RING_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("RING_CLIENT_SECRET")
        self.hardware_id = hardware_id or os.getenv("RING_HARDWARE_ID") or str(uuid.uuid5(uuid.NAMESPACE_URL, "stocked"))
        self.base_url = base_url
        self._expires_at = 0.0 if not self.access_token else time.time() + 25 * 60
        self._http = httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False)

    @property
    def configured(self) -> bool:
        return bool(self.access_token or (self.refresh_token and self.client_id and self.client_secret))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "User-Agent": USER_AGENT,
                "Hardware_Id": self.hardware_id, "Content-Type": "application/json"}

    async def _ensure_token(self) -> None:
        if self.access_token and time.time() < self._expires_at - 60:
            return
        if not (self.refresh_token and self.client_id and self.client_secret):
            if self.access_token:
                return  # Playground token: use it until Ring says 401
            raise RingAuthError("No Ring credentials: set RING_ACCESS_TOKEN or RING_REFRESH_TOKEN + client id/secret.")
        r = await self._http.post(OAUTH, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                               "client_id": self.client_id, "client_secret": self.client_secret},
                                  headers={"User-Agent": USER_AGENT, "Hardware_Id": self.hardware_id})
        if r.status_code != 200:
            raise RingAuthError(f"token refresh failed: HTTP {r.status_code}")
        tok = r.json()
        self.access_token, self.refresh_token = tok["access_token"], tok.get("refresh_token", self.refresh_token)
        self._expires_at = time.time() + int(tok.get("expires_in", 14400))

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        await self._ensure_token()
        for attempt in range(4):
            r = await self._http.request(method, self.base_url + path, headers=self._headers(), **kw)
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code == 401:
                raise RingAuthError("Ring rejected the token (401). Refresh or re-link the account.")
            return r
        return r

    async def devices(self) -> list[dict]:
        r = await self._request("GET", "/v1/devices", params={"include": "status,capabilities"})
        r.raise_for_status()
        out = []
        for d in r.json().get("data", []):
            attrs = d.get("attributes", {})
            out.append({"id": d.get("id"), "name": attrs.get("name") or attrs.get("description") or d.get("id"),
                        "type": d.get("type"), "attributes": attrs})
        return out

    async def history(self, device_id: str, event_types: list[str] | None = None) -> list[dict]:
        params = {"event_types": ",".join(event_types)} if event_types else {}
        r = await self._request("GET", f"/v1/history/devices/{device_id}/events", params=params)
        r.raise_for_status()
        return r.json().get("data", [])

    async def snapshot(self, device_id: str, start_ms: int | None = None) -> bytes | None:
        """Latest image at/after ``start_ms``. Ring answers 303 with a pre-signed URL; fetch it without auth."""
        body = {"type": "latest_in_range", "start_timestamp": start_ms or int(time.time() * 1000) - 60_000,
                "image_options": {"format": "jpeg"}}
        r = await self._request("POST", f"/v1/devices/{device_id}/media/image/download", json=body)
        if r.status_code in (301, 302, 303, 307) and "location" in r.headers:
            async with httpx.AsyncClient(timeout=20) as anon:  # never forward the bearer token
                img = await anon.get(r.headers["location"])
                return img.content if img.status_code == 200 else None
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content
        return None  # 403 TIME_RANGE_NOT_AUTHORIZED etc.: no authorized media yet

    async def aclose(self) -> None:
        await self._http.aclose()
