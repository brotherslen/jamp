"""A cache that makes the network optional.

Phases 0-2 are offline and reproducible, and that is worth protecting: a scraper that silently disagreed with itself between runs would undermine
the confidence scoring everything else rests on.  Phase 3 has to reach the
network, so the compromise is this cache.

Every response is stored, keyed by URL, with the time it was fetched.  A second
run answers from the cache and touches nothing.  `offline=True` refuses to fetch
at all and raises for anything not already stored, which is what makes a phase 3
run reproducible after the first: the cache becomes a durable local copy of the
song databases, backed up with the library and independent of any site staying
up.

Nothing here knows what it is fetching.  The archive.org shapes live in
`archiveorg.py`, so a second source can be added without touching this.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Identifies the tool to the server.  archive.org asks that automated clients
# say who they are, and a request that does not is the kind that gets blocked
# for everyone later.
def _user_agent() -> str:
    from . import __version__

    return ("jamp/%s (personal live-music library tool; "
            "+https://github.com/brotherslen/jamp)" % __version__)


USER_AGENT = _user_agent()

# A server that says "too many requests" or "unavailable" is asked again after
# the wait it names (Retry-After), or after these, and then left alone.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_WAITS = (5.0, 20.0)
MAX_RETRY_AFTER = 120.0
# After this many failed URLs in a row from one site, the rest of the run stops
# asking it: a site that is down is not helped by a thousand more requests.
HOST_FAILURE_LIMIT = 5

# A courtesy gap between live requests.  The cache means this is paid once per
# URL for the life of the cache, not on every run.
MIN_INTERVAL_SECONDS = 0.34


# How long an answer about a show stays good, by how old the show is.
#
# phish.net asks for a 24-hour refresh because "setlists and related content can
# be edited after a show".  That is a live concern for a show played this year -
# someone is still correcting the encore - and an academic one for 1994, where
# refetching two thousand unchanged shows would be rude rather than careful.
THIS_YEAR_DAYS = 7.0
EARLIER_DAYS = 30.0
# How long a 404 is believed.
NOT_FOUND_DAYS = 7.0


def age_for(show_date: str | None) -> float:
    """Days a cached answer about `show_date` may be kept before refetching."""
    if not show_date:
        return EARLIER_DAYS
    try:
        year = int(str(show_date)[:4])
    except (TypeError, ValueError):
        return EARLIER_DAYS
    return THIS_YEAR_DAYS if year >= time.localtime().tm_year else EARLIER_DAYS


def _retry_after(error, fallback: float) -> float:
    """The wait a server asks for in Retry-After, within reason, or `fallback`."""
    try:
        asked = float((error.headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(asked, MAX_RETRY_AFTER))


def _older_than(fetched_at: str, days: float) -> bool:
    """Was this stored longer ago than `days`?  An unreadable stamp counts as old."""
    try:
        when = time.strptime(fetched_at, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return True
    return (time.time() - time.mktime(when)) > days * 86400.0


class OfflineMiss(Exception):
    """Asked for something not in the cache, with fetching disabled."""


class FetchError(Exception):
    """The request was made and failed.  Recorded, so it is not retried blindly."""


@dataclass
class CachedResponse:
    url: str
    status: int
    body: bytes
    fetched_at: str
    from_cache: bool

    def json(self):
        return json.loads(self.body.decode("utf-8", "replace"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS http (
    url        TEXT PRIMARY KEY,
    status     INTEGER NOT NULL,
    body       BLOB    NOT NULL,
    fetched_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    url        TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    failed_at  TEXT NOT NULL
);
"""


class HttpCache:
    """SQLite-backed GET cache.  One file, safe to copy, safe to delete."""

    def __init__(self, path: Path | str, offline: bool = False,
                 opener=None, sleep=time.sleep, max_age_days: float | None = None):
        self.path = Path(path)
        self.offline = offline
        # None means a stored answer never goes stale, which is right for a show
        # from 1994.  phish.net asks that a cache be refreshed every 24 hours
        # because "setlists and related content can be edited after a show" -
        # true of a show from last week, academic for one from thirty years ago.
        # So this is a knob rather than a rule: tighten it for recent shows, and
        # leave it open for the historical library where refetching two thousand
        # unchanged shows would be rude rather than careful.
        self.max_age_days = max_age_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(_SCHEMA)
        self.db.commit()
        # Injected so the tests never touch the network.
        self._opener = opener or self._urlopen
        self._sleep = sleep
        self._last_request = 0.0
        self._host_failures: dict[str, int] = {}
        self.hosts_given_up: set[str] = set()
        self.hits = 0
        self.fetches = 0
        self.stale = 0

    # -- storage ---------------------------------------------------------
    def stored(self, url: str, max_age_days: float | None = None) -> CachedResponse | None:
        row = self.db.execute(
            "SELECT status, body, fetched_at FROM http WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        age = max_age_days if max_age_days is not None else self.max_age_days
        if row[0] == 404:
            # "Not there" is an answer worth caching, but not for good: a
            # source that 404s on a bad day, or has not uploaded a show yet,
            # would otherwise say so for as long as the cache is kept.
            age = NOT_FOUND_DAYS if age is None else min(age, NOT_FOUND_DAYS)
        if age is not None and _older_than(row[2], age):
            return None
        return CachedResponse(url, row[0], row[1], row[2], from_cache=True)

    def _store(self, url: str, status: int, body: bytes) -> CachedResponse:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.db.execute(
            "INSERT OR REPLACE INTO http (url, status, body, fetched_at) VALUES (?, ?, ?, ?)",
            (url, status, body, now))
        self.db.execute("DELETE FROM failures WHERE url = ?", (url,))
        self.db.commit()
        return CachedResponse(url, status, body, now, from_cache=False)

    def note_failure(self, url: str, reason: str) -> None:
        """A failure is recorded rather than forgotten.

        Without this a dead URL is retried on every run for ever, which is slow
        and rude to the server.  It is not treated as an answer: `get` still
        tries again, but the report can say what has been failing and why.
        """
        self.db.execute(
            "INSERT OR REPLACE INTO failures (url, reason, failed_at) VALUES (?, ?, ?)",
            (url, reason[:400], time.strftime("%Y-%m-%dT%H:%M:%S")))
        self.db.commit()

    # -- fetching --------------------------------------------------------
    def _urlopen(self, url: str, timeout: int) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()

    def get(self, url: str, timeout: int = 30, fetch_url: str | None = None,
            max_age_days: float | None = None) -> CachedResponse:
        """`url` is the key; `fetch_url` is what is actually requested.

        They differ when a source needs a secret in the query string.
        phish.net takes the API key as ?apikey=..., and storing that as the
        cache key would write it into a file that gets backed up, and print it
        in any report that names a URL.  The key belongs in the request and
        nowhere else.
        """
        hit = self.stored(url, max_age_days)
        if hit is not None:
            self.hits += 1
            return hit
        if self.offline:
            # A stale answer beats no answer when the network is off the table:
            # refusing here would make --offline fail on a library that is
            # merely a month out of date.
            # Infinity, not None: None means "use the instance default", which
            # is the very limit being set aside here.
            stale = self.stored(url, max_age_days=float("inf"))
            if stale is not None:
                self.hits += 1
                self.stale += 1
                return stale
            raise OfflineMiss(url)
        host = urllib.parse.urlsplit(fetch_url or url).netloc.lower()
        if host in self.hosts_given_up:
            raise FetchError("%s: not asked - %s failed %d times in a row this run"
                             % (url, host, HOST_FAILURE_LIMIT))
        waits = list(RETRY_WAITS)
        while True:
            gap = MIN_INTERVAL_SECONDS - (time.time() - self._last_request)
            if gap > 0:
                self._sleep(gap)
            try:
                status, body = self._opener(fetch_url or url, timeout)
                break
            except urllib.error.HTTPError as e:
                self._last_request = time.time()
                # A 404 is an answer - the show is not there - and caching it
                # stops the same miss being asked again on every future run.
                if e.code == 404:
                    self.fetches += 1
                    self._host_failures[host] = 0
                    return self._store(url, 404, b"")
                if e.code in RETRY_STATUSES and waits:
                    self._sleep(_retry_after(e, waits.pop(0)))
                    continue
                self._failed(host, url, "HTTP %s" % e.code)
                raise FetchError("%s: HTTP %s" % (url, e.code)) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                self._last_request = time.time()
                if waits:
                    self._sleep(waits.pop(0))
                    continue
                self._failed(host, url, "%s: %s" % (type(e).__name__, e))
                raise FetchError("%s: %s" % (url, e)) from e
            except Exception as e:                   # noqa: BLE001 - reported, not swallowed
                self._failed(host, url, "%s: %s" % (type(e).__name__, e))
                raise FetchError("%s: %s" % (url, e)) from e
        self._last_request = time.time()
        self.fetches += 1
        self._host_failures[host] = 0
        return self._store(url, status, body)

    def _failed(self, host: str, url: str, reason: str) -> None:
        self.note_failure(url, reason)
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        if self._host_failures[host] >= HOST_FAILURE_LIMIT:
            self.hosts_given_up.add(host)

    # -- housekeeping ----------------------------------------------------
    def stats(self) -> dict:
        n = self.db.execute("SELECT COUNT(*) FROM http").fetchone()[0]
        misses = self.db.execute("SELECT COUNT(*) FROM http WHERE status = 404").fetchone()[0]
        fails = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"urls": n, "known_404": misses, "failures": fails,
                "sites_given_up": sorted(self.hosts_given_up),
                "bytes": size, "hits": self.hits, "fetches": self.fetches,
                "stale_served": self.stale}

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
