"""RSS/Atom podcast feed parsing + episode upsert.

Pure helpers with no worker/route coupling: fetch a feed, normalise its metadata and episodes,
and idempotently upsert them into the Podcast/Episode tables. The worker drives scanning; routes
drive subscribe/list. Episode identity is (podcast_id, guid) so re-scans never duplicate.

⚠️ **Nothing here downloads episode audio.** The server owns the subscription; every client streams
or downloads from `Episode.enclosure_url` itself, exactly as a traditional podcast app does. Only
the feed and the cover image are ever fetched by this process.
"""

from __future__ import annotations

import calendar
import re
import subprocess
from urllib.parse import urlsplit
from datetime import datetime, timezone

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Episode, Podcast, utcnow
from .app_log import write_app_log

_USER_AGENT = "Nudibranch/1.0 (+https://nudibranch.pophosting.xyz)"
# Some hosts (Patreon behind Cloudflare, most notably) serve a feed to a browser and refuse an
# unrecognised agent from a datacenter IP. Retried with this only after a block-shaped status, so
# the honest agent above stays the one nearly every feed sees.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"


def browser_headers(url: str, accept: str = "*/*") -> dict[str, str]:
    """A complete, self-consistent browser header set for the block-status retry.

    ⚠️ A `User-Agent` on its own is not convincing: a filter that bothers to check the agent also
    checks that the rest of the request agrees with it, and a "Chrome" request carrying no
    Accept-Language, no Sec-Fetch-* and no Referer is exactly the shape it is looking for.

    Note this cannot defeat filtering that keys on TLS fingerprint or IP reputation, which is what
    a datacenter address is usually refused for — headers are the part we can honestly influence.
    `Accept-Encoding` deliberately omits `br`: httpx only decodes Brotli when the optional package
    is installed, so advertising it risks being handed a body we cannot read.
    """
    try:
        origin = urlsplit(url)
        referer = f"{origin.scheme}://{origin.netloc}/" if origin.scheme and origin.netloc else None
    except ValueError:
        referer = None
    headers = {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    return headers
# Statuses that mean "we don't like you" rather than "this isn't here". Public because the worker's
# episode download needs the same definition — one list, so the feed and the audio can't disagree
# about what counts as being filtered.
BLOCK_STATUSES = frozenset({401, 403, 406, 409, 429, 503})
_BLOCK_STATUSES = BLOCK_STATUSES
_FEED_TIMEOUT = 20.0


class FeedFetchError(Exception):
    """A feed could not be retrieved, with a reason fit to show a user.

    Exists so callers can tell "the host refused us" from "that isn't a feed" — the subscribe route
    reported both as one opaque 400, which made a server-side block indistinguishable from a typo
    in the URL.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_MAX_FEED_BYTES = 32 * 1024 * 1024


def _proxy_url() -> str | None:
    """The podcast-only outbound proxy, or None for direct. See `Settings.podcast_proxy_url`."""
    from ..core.config import get_settings

    return get_settings().podcast_proxy_url.strip() or None


def _curl_proxy_args() -> list[str]:
    proxy = _proxy_url()
    return ["--proxy", proxy] if proxy else []


def _fetch_via_curl(url: str) -> bytes:
    """Last-resort fetch through curl, for a host that refuses httpx but serves curl.

    Measured, not guessed: Patreon behind Cloudflare returned 403 to httpx from a server — with a
    browser User-Agent and a full matching header set — while plain `curl -A '<browser UA>'` from
    that same machine got 200. Headers were therefore not the discriminator; the transport is
    (curl negotiates HTTP/2 and presents an OpenSSL TLS fingerprint, httpx neither), and that is
    not something request-level code can imitate.

    ⚠️ Security notes, because this hands a user-supplied URL to a tool that speaks far more than
    HTTP: the scheme is checked here AND pinned with `--proto`/`--proto-redir` so a `file://` feed
    URL cannot read local files and a redirect cannot walk to another protocol; the URL goes after
    `--` so one starting with `-` is not read as a flag; and there is no shell anywhere (argv list,
    `shell=False`), so the query string's `&` needs no quoting. Size and time are both capped.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise FeedFetchError("only http and https feed URLs are supported")
    command = [
        "curl", "--silent", "--show-error", "--fail", "--location",
        "--proto", "=http,https", "--proto-redir", "=http,https",
        *_curl_proxy_args(),
        "--max-time", str(int(_FEED_TIMEOUT)),
        "--max-filesize", str(_MAX_FEED_BYTES),
        "--user-agent", _BROWSER_USER_AGENT,
        "--", url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=_FEED_TIMEOUT + 10, check=False)
    except FileNotFoundError as error:      # curl absent from the image
        raise FeedFetchError("the feed host refused the request and no fallback fetcher is available") from error
    except subprocess.TimeoutExpired as error:
        raise FeedFetchError("the feed host took too long to respond") from error
    except OSError as error:
        raise FeedFetchError(f"the fallback fetcher could not run ({type(error).__name__})") from error
    if result.returncode != 0:
        raise _curl_error(result.stderr, result.returncode)
    return result.stdout


def _curl_error(stderr: bytes | None, returncode: int) -> "FeedFetchError":
    """A FeedFetchError carrying the HTTP status curl saw, so callers can branch on it rather than
    parsing the human-readable message."""
    reason = _curl_reason(stderr, returncode)
    match = re.search(r"\((\d{3})\)", reason)
    return FeedFetchError(reason, status=int(match.group(1)) if match else None)


def _curl_reason(stderr: bytes | None, returncode: int) -> str:
    """Turn curl's stderr into the same vocabulary the httpx path uses.

    Without this the fallback reported a plain 404 as "refused the request (curl: (56) ...)" —
    both the wrong cause and the kind of tooling jargon that must not reach a user (CLAUDE.md §8).
    """
    text = (stderr or b"").decode("utf-8", "replace").strip()
    match = re.search(r"returned error:?\s*(\d{3})", text)
    if match:
        status = int(match.group(1))
        if status == 404:
            return "the feed was not found (404)"
        if status in _BLOCK_STATUSES:
            return f"the feed host refused the request ({status})"
        return f"the feed host returned an error ({status})"
    if returncode == 28:
        return "the feed host took too long to respond"
    if returncode == 63:
        return "the feed was too large to download"
    if returncode in (5, 6, 7):
        return "the feed host could not be reached"
    return "the feed host refused the request"


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch + parse a podcast feed.

    Raises `FeedFetchError` with a human-readable reason on any hard network/HTTP failure; a
    malformed-but-served feed still parses (feedparser is lenient) and is validated by callers.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": _FEED_ACCEPT}
    try:
        with httpx.Client(follow_redirects=True, timeout=_FEED_TIMEOUT, headers=headers, proxy=_proxy_url()) as client:
            response = client.get(url)
            if response.status_code in _BLOCK_STATUSES:
                # Escalate only on a status meaning the host is filtering clients — never on a 404
                # or a 5xx. First a browser header set in-process (fixes hosts that check only the
                # agent), then curl, which survives the ones that fingerprint the transport itself.
                write_app_log(
                    f"Feed host returned {response.status_code}; retrying as a browser: {url}",
                    "warning",
                )
                response = client.get(url, headers=browser_headers(url, _FEED_ACCEPT))
                if response.status_code in _BLOCK_STATUSES:
                    write_app_log(
                        f"Feed host still returned {response.status_code}; falling back to curl: {url}",
                        "warning",
                    )
                    return _parse_feed_bytes(_fetch_via_curl(url))
            response.raise_for_status()
            content = response.content
    except httpx.TimeoutException as error:
        raise FeedFetchError("the feed host took too long to respond") from error
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in _BLOCK_STATUSES:
            reason = f"the feed host refused the request ({status})"
        elif status == 404:
            reason = "the feed was not found (404)"
        else:
            reason = f"the feed host returned an error ({status})"
        raise FeedFetchError(reason, status=status) from error
    except httpx.HTTPError as error:
        # DNS failures, TLS errors, connection refused — the shape of an egress problem.
        raise FeedFetchError(f"the feed host could not be reached ({type(error).__name__})") from error

    return _parse_feed_bytes(content)


def fetch_media(url: str, *, timeout: float = 20.0) -> tuple[bytes, str]:
    """Fetch a podcast image, escalating to curl exactly like `fetch_feed`.

    Podcast art and audio sit on the same edge as the feed (Patreon serves both through
    Cloudflare), so a host that fingerprints the transport refuses all three. Without this a
    subscribe that only succeeded via the fallback would still show no artwork.
    Returns (content, content_type).
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": _USER_AGENT}, proxy=_proxy_url()) as client:
            response = client.get(url)
            if response.status_code not in _BLOCK_STATUSES:
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "")
    except httpx.HTTPStatusError as error:
        raise FeedFetchError(f"the host returned an error ({error.response.status_code})") from error
    except httpx.HTTPError as error:
        raise FeedFetchError(f"the host could not be reached ({type(error).__name__})") from error
    return _fetch_media_via_curl(url, timeout=timeout)


def _fetch_media_via_curl(url: str, *, timeout: float) -> tuple[bytes, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise FeedFetchError("only http and https URLs are supported")
    command = [
        "curl", "--silent", "--show-error", "--fail", "--location",
        "--proto", "=http,https", "--proto-redir", "=http,https",
        *_curl_proxy_args(),
        "--max-time", str(int(timeout)),
        "--max-filesize", str(_MAX_FEED_BYTES),
        "--user-agent", _BROWSER_USER_AGENT,
        # ⚠️ `%{stderr}` sends the report to stderr. Written to stdout it would be appended to the
        # image bytes with no separator, and splitting binary data on a newline to recover it is
        # guesswork — the last bytes of a PNG are as likely to contain one as not.
        "--write-out", "%{stderr}%{content_type}",
        "--", url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout + 10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FeedFetchError(f"the fallback fetcher could not run ({type(error).__name__})") from error
    if result.returncode != 0:
        raise _curl_error(result.stderr, result.returncode)
    content_type = (result.stderr or b"").decode("ascii", "replace").strip()
    return result.stdout, content_type


def _parse_feed_bytes(content: bytes) -> feedparser.FeedParserDict:
    """Shared tail for both fetch paths, so the curl fallback can't skip the empty-body check."""
    if not content:
        raise FeedFetchError("the feed host returned an empty response")
    return feedparser.parse(content)


def _struct_to_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_duration_ms(raw) -> int | None:
    """iTunes <itunes:duration> is either seconds ("3600") or "HH:MM:SS" / "MM:SS"."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [int(float(p)) for p in parts]
        except ValueError:
            return None
        seconds = 0
        for part in nums:
            seconds = seconds * 60 + part
        return seconds * 1000
    try:
        return int(float(text)) * 1000
    except ValueError:
        return None


def _parse_int(raw) -> int | None:
    if raw is None:
        return None
    match = re.search(r"\d+", str(raw))
    return int(match.group()) if match else None


def _entry_enclosure(entry) -> dict | None:
    """The entry's media enclosure as {url, format, file_size}.

    `format` and `file_size` come from the feed's own `type`/`length` attributes. They used to be
    measured from the downloaded file; with nothing downloaded here, the feed's declaration is the
    only source — and it is what a client needs to name the file it saves.
    """
    for enclosure in getattr(entry, "enclosures", []) or []:
        href = enclosure.get("href") or enclosure.get("url")
        if href:
            return {
                "url": href,
                "format": _media_format(enclosure.get("type"), href),
                "file_size": _parse_int(enclosure.get("length")),
            }
    for link in getattr(entry, "links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return {
                "url": link["href"],
                "format": _media_format(link.get("type"), link["href"]),
                "file_size": _parse_int(link.get("length")),
            }
    return None


def _media_format(content_type: str | None, url: str) -> str | None:
    """"audio/mpeg" -> "mp3"; falls back to the URL's own extension."""
    subtype = (content_type or "").split(";")[0].strip().lower().split("/")[-1]
    mapped = {"mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "mp4a-latm": "m4a"}.get(subtype, subtype)
    cleaned = "".join(character for character in mapped if character.isalnum())
    if cleaned and len(cleaned) <= 8:
        return cleaned
    try:
        suffix = urlsplit(url).path.rsplit(".", 1)
    except ValueError:
        return None
    if len(suffix) == 2 and suffix[1].isalnum() and len(suffix[1]) <= 8:
        return suffix[1].lower()
    return None


def _entry_image_url(entry) -> str | None:
    image = getattr(entry, "image", None)
    if image and image.get("href"):
        return image["href"]
    href = entry.get("itunes_image") if hasattr(entry, "get") else None
    if isinstance(href, dict):
        return href.get("href")
    return href or None


def parse_podcast_meta(feed: feedparser.FeedParserDict) -> dict:
    channel = feed.feed if feed else {}
    image = channel.get("image") if hasattr(channel, "get") else None
    image_url = image.get("href") if isinstance(image, dict) else None
    return {
        "title": (channel.get("title") or "").strip() or "Untitled podcast",
        "author": (channel.get("author") or channel.get("itunes_author") or "").strip() or None,
        "description": (channel.get("summary") or channel.get("subtitle") or "").strip() or None,
        "image_url": image_url,
    }


def parse_episodes(feed: feedparser.FeedParserDict) -> list[dict]:
    """Normalise feed entries → episode dicts. Entries with no playable enclosure are skipped."""
    episodes: list[dict] = []
    for entry in getattr(feed, "entries", []) or []:
        enclosure = _entry_enclosure(entry)
        if not enclosure:
            continue
        enclosure_url = enclosure["url"]
        guid = (entry.get("id") or entry.get("guid") or enclosure_url).strip()
        published = _struct_to_datetime(entry.get("published_parsed")) or _struct_to_datetime(entry.get("updated_parsed"))
        episodes.append(
            {
                "guid": guid,
                "title": (entry.get("title") or "Untitled episode").strip(),
                "description": (entry.get("summary") or "").strip() or None,
                "published_at": published,
                "enclosure_url": enclosure_url,
                "format": enclosure["format"],
                "file_size": enclosure["file_size"],
                "duration_ms": _parse_duration_ms(entry.get("itunes_duration")),
                "image_url": _entry_image_url(entry),
                "season": _parse_int(entry.get("itunes_season")),
                "episode_number": _parse_int(entry.get("itunes_episode")),
            }
        )
    return episodes


def upsert_podcast(
    session: Session,
    feed_url: str,
    feed: feedparser.FeedParserDict,
) -> Podcast:
    """Create or update the Podcast row for feed_url from the parsed feed metadata."""
    meta = parse_podcast_meta(feed)
    podcast = session.scalar(select(Podcast).where(Podcast.feed_url == feed_url))
    if podcast is None:
        podcast = Podcast(feed_url=feed_url, title=meta["title"])
        session.add(podcast)
    podcast.title = meta["title"]
    podcast.author = meta["author"]
    podcast.description = meta["description"]
    if meta["image_url"] and not podcast.image_url:
        podcast.image_url = meta["image_url"]
    podcast.last_scanned_at = utcnow()
    podcast.last_error = None
    session.flush()
    return podcast


def upsert_episodes(session: Session, podcast: Podcast, parsed: list[dict]) -> list[Episode]:
    """Insert new episodes / refresh mutable metadata on existing ones. Returns newly-created rows."""
    existing = {
        episode.guid: episode
        for episode in session.scalars(select(Episode).where(Episode.podcast_id == podcast.id))
    }
    created: list[Episode] = []
    for item in parsed:
        episode = existing.get(item["guid"])
        if episode is None:
            episode = Episode(
                podcast_id=podcast.id,
                guid=item["guid"],
                title=item["title"],
                description=item["description"],
                published_at=item["published_at"],
                enclosure_url=item["enclosure_url"],
                format=item["format"],
                file_size=item["file_size"],
                duration_ms=item["duration_ms"],
                image_url=item["image_url"],
                season=item["season"],
                episode_number=item["episode_number"],
            )
            session.add(episode)
            created.append(episode)
        else:
            # Refresh metadata that can legitimately change. A publisher does re-host audio, and
            # since the enclosure URL is now what every client plays from, a stale one is a dead
            # episode rather than a cosmetic difference.
            episode.title = item["title"]
            episode.description = item["description"]
            episode.published_at = item["published_at"]
            episode.enclosure_url = item["enclosure_url"]
            if item["format"]:
                episode.format = item["format"]
            if item["file_size"]:
                episode.file_size = item["file_size"]
            if item["duration_ms"]:
                episode.duration_ms = item["duration_ms"]
            if item["image_url"]:
                episode.image_url = item["image_url"]
            episode.season = item["season"]
            episode.episode_number = item["episode_number"]
    session.flush()
    return created
