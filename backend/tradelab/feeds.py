"""Registry of data feeds and their health. Populated by ingestion modules (from M1)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

FeedState = Literal["not_started", "connecting", "live", "stalled", "error"]


@dataclass
class FeedStatus:
    id: str
    source: str
    state: FeedState = "not_started"
    last_received_at_utc: str | None = None
    detail: str = ""


class FeedRegistry:
    def __init__(self) -> None:
        self._feeds: dict[str, FeedStatus] = {}

    def register(self, feed: FeedStatus) -> FeedStatus:
        self._feeds[feed.id] = feed
        return feed

    def get(self, feed_id: str) -> FeedStatus:
        return self._feeds[feed_id]

    def snapshot(self) -> list[dict]:
        return [asdict(f) for f in self._feeds.values()]
