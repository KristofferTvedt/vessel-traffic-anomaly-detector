"""Runtime configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class BBox:
    """Area of interest in WGS84 degrees."""
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def contains(self, lon: float, lat: float) -> bool:
        return (self.min_lon <= lon <= self.max_lon
                and self.min_lat <= lat <= self.max_lat)

    def inset(self, margin: float) -> "BBox":
        """Shrink the box by ``margin`` degrees on every side. Behaviour at the
        very edge of the AOI is dominated by vessels crossing the boundary, so
        detection runs on the inner box only."""
        return BBox(self.min_lon + margin, self.min_lat + margin,
                    self.max_lon - margin, self.max_lat - margin)


@dataclass(frozen=True)
class Config:
    bw_client_id: str
    bw_client_secret: str
    aoi_name: str
    aoi: BBox
    raw_dir: Path
    db_path: Path

    @classmethod
    def load(cls) -> "Config":
        def _path(env: str, default: str) -> Path:
            p = Path(os.getenv(env, default).strip())
            return p if p.is_absolute() else ROOT / p

        return cls(
            bw_client_id=os.getenv("BW_CLIENT_ID", "").strip(),
            bw_client_secret=os.getenv("BW_CLIENT_SECRET", "").strip(),
            aoi_name=os.getenv("AOI_NAME", "aoi").strip(),
            aoi=BBox(
                min_lon=float(os.getenv("AOI_MIN_LON", "4.90")),
                min_lat=float(os.getenv("AOI_MIN_LAT", "60.10")),
                max_lon=float(os.getenv("AOI_MAX_LON", "5.55")),
                max_lat=float(os.getenv("AOI_MAX_LAT", "60.55")),
            ),
            raw_dir=_path("RAW_DIR", "raw"),
            db_path=_path("DB_PATH", "data/vessels.db"),
        )

    def require_barentswatch(self) -> None:
        if not self.bw_client_id or not self.bw_client_secret:
            raise RuntimeError(
                "BW_CLIENT_ID / BW_CLIENT_SECRET are required for live "
                "collection. Register a client at https://developer.barentswatch.no/ "
                "then copy .env.example to .env and fill them in."
            )
