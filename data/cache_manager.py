"""Daily disk cache for expensive data fetches (bars, ratings, news, catalyst hits).

Caches are stored under data/cache/{date}/ as parquet (bars) or JSON (everything
else). On a cache hit the data is returned immediately; on a miss the caller is
responsible for fetching and calling save_*. Cache dirs older than 5 days are
pruned automatically on each load call.
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class CacheManager:
    """Read/write daily cache files keyed by date string (YYYY-MM-DD)."""

    def __init__(self, cache_dir: str = "data/cache", ttl_days: int = 5):
        self._root = Path(cache_dir)
        self._ttl_days = ttl_days

    # ------------------------------------------------------------------
    # Bars (parquet)
    # ------------------------------------------------------------------

    def load_bars(self, date: str) -> Optional[dict]:
        path = self._path(date, "bars.json")
        if not path.exists():
            return None
        self._prune_old()
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Cache read error bars {date}: {e}")
            return None

    def save_bars(self, date: str, bars: dict) -> None:
        path = self._path(date, "bars.json", mkdir=True)
        try:
            with open(path, "w") as f:
                json.dump(bars, f)
        except Exception as e:
            logger.warning(f"Cache write error bars {date}: {e}")

    # ------------------------------------------------------------------
    # Ratings + company info (JSON)
    # ------------------------------------------------------------------

    def load_ratings(self, date: str) -> Optional[dict]:
        return self._load_json(date, "ratings.json")

    def save_ratings(self, date: str, ratings: dict) -> None:
        self._save_json(date, "ratings.json", ratings)

    def load_company_info(self, date: str) -> Optional[dict]:
        return self._load_json(date, "company_info.json")

    def save_company_info(self, date: str, info: dict) -> None:
        self._save_json(date, "company_info.json", info)

    # ------------------------------------------------------------------
    # News (JSON)
    # ------------------------------------------------------------------

    def load_news(self, date: str) -> Optional[dict]:
        return self._load_json(date, "news.json")

    def save_news(self, date: str, news: dict) -> None:
        self._save_json(date, "news.json", news)

    # ------------------------------------------------------------------
    # Catalyst hits (JSON)
    # ------------------------------------------------------------------

    def load_catalyst(self, date: str) -> Optional[dict]:
        return self._load_json(date, "catalyst.json")

    def save_catalyst(self, date: str, catalyst: dict) -> None:
        self._save_json(date, "catalyst.json", catalyst)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path(self, date: str, filename: str, mkdir: bool = False) -> Path:
        d = self._root / date
        if mkdir:
            d.mkdir(parents=True, exist_ok=True)
        return d / filename

    def _load_json(self, date: str, filename: str) -> Optional[dict]:
        path = self._path(date, filename)
        if not path.exists():
            return None
        self._prune_old()
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Cache read error {filename} {date}: {e}")
            return None

    def _save_json(self, date: str, filename: str, data: dict) -> None:
        path = self._path(date, filename, mkdir=True)
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Cache write error {filename} {date}: {e}")

    def _prune_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._ttl_days)
        if not self._root.exists():
            return
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            try:
                entry_date = datetime.strptime(entry.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if entry_date < cutoff:
                    shutil.rmtree(entry)
                    logger.debug(f"Pruned cache dir {entry.name}")
            except ValueError:
                pass
