"""BarentsWatch live AIS client.

Auth is OAuth2 client-credentials: POST id/secret to the token endpoint (in the
body, not headers), scope ``ais``, and reuse the bearer token until it expires.
``latest_positions`` returns the most recent position per vessel currently seen
in Norwegian waters; we filter to the area of interest ourselves.

Field names below are BarentsWatch's own (camelCase): mmsi, latitude, longitude,
speedOverGround (knots), courseOverGround, trueHeading, shipType, msgtime.
"""
from __future__ import annotations

import time

from .config import BBox, Config
from .geo import valid_cog, valid_heading, valid_sog
from .http import get_json, post_form

TOKEN_URL = "https://id.barentswatch.no/connect/token"
LATEST_URL = "https://live.ais.barentswatch.no/v1/latest/combined"


class Client:
    def __init__(self, client_id: str, client_secret: str):
        self._id = client_id
        self._secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0

    def _access_token(self) -> str:
        # Refresh a minute before expiry to avoid racing the clock.
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        payload = post_form(
            TOKEN_URL,
            data={
                "client_id": self._id,
                "client_secret": self._secret,
                "scope": "ais",
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def latest_positions(self) -> list[dict]:
        """Latest AIS position report per vessel, across all reporting waters."""
        data = get_json(
            LATEST_URL,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Accept": "application/json"},
        )
        return data if isinstance(data, list) else data.get("features", data)


def _clean(raw: dict) -> dict | None:
    """Normalise one BarentsWatch record to our internal shape, or drop it."""
    mmsi = raw.get("mmsi")
    lat = raw.get("latitude")
    lon = raw.get("longitude")
    if mmsi is None or lat is None or lon is None:
        return None
    return {
        "mmsi": int(mmsi),
        "name": (raw.get("name") or "").strip() or None,
        "ship_type": raw.get("shipType"),
        "lat": float(lat),
        "lon": float(lon),
        "sog": valid_sog(raw.get("speedOverGround")),   # knots
        "cog": valid_cog(raw.get("courseOverGround")),  # degrees
        "heading": valid_heading(raw.get("trueHeading")),  # degrees
        "msgtime": raw.get("msgtime"),       # ISO UTC
    }


def positions_in(client: Client, aoi: BBox) -> list[dict]:
    """Cleaned latest positions inside the area of interest."""
    out = []
    for raw in client.latest_positions():
        rec = _clean(raw)
        if rec and aoi.contains(rec["lon"], rec["lat"]):
            out.append(rec)
    return out


def from_config(cfg: Config) -> Client:
    cfg.require_barentswatch()
    return Client(cfg.bw_client_id, cfg.bw_client_secret)
