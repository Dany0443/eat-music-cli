

from __future__ import annotations
import json, random, re, time
from dataclasses import dataclass
from typing import Optional
import requests

_S = requests.Session()
_S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4

class MetadataError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

def _retry_delay(attempt: int, retry_after: str | None) -> float:
    try:
        if retry_after is not None:
            ra = float(retry_after)
            if ra >= 0:
                return min(ra, 30.0)
    except Exception:
        pass
    # Exponential backoff + a little jitter to avoid burst retries.
    return min((1.25 ** attempt) + random.uniform(0.2, 0.8), 15.0)

def _get(url: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = _S.get(url, **kwargs)
            if r.status_code in _RETRYABLE and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt, r.headers.get("Retry-After")))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt >= _MAX_ATTEMPTS - 1:
                break
            time.sleep(_retry_delay(attempt, None))
    raise RuntimeError(f"HTTP request failed for {url}") from last_exc

# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    id:           str
    title:        str
    artist:       str
    album:        str
    track_num:    int
    total:        int
    duration_sec: float
    year:         str
    art_url:      str = ""

# ── URL parsing ───────────────────────────────────────────────────────────────

_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)

def parse_url(url: str) -> tuple[str, str]:
    m = _URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a Spotify URL: {url!r}")
    return m.group(1), m.group(2)

# ── Embed scraper (method 1) ──────────────────────────────────────────────────

def _embed_json(kind: str, sid: str) -> Optional[dict]:
    """Fetch Spotify embed page and extract __NEXT_DATA__ JSON."""
    try:
        r = _get(f"https://open.spotify.com/embed/{kind}/{sid}", timeout=12)
    except Exception:
        return None
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
    )
    return json.loads(m.group(1)) if m else None

def _nav(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d or default

def _img(images: list) -> str:
    if not images:
        return ""
    return max(images, key=lambda i: i.get("width") or i.get("maxWidth") or 0).get("url", "")

def _id_from_uri(uri: str) -> str:
    # spotify:track:<id> / spotify:album:<id> / spotify:playlist:<id>
    if not uri:
        return ""
    return uri.rsplit(":", 1)[-1]

def _artists_from_entity(e: dict) -> list[str]:
    artists = [a.get("name", "") for a in (e.get("artists") or []) if isinstance(a, dict)]
    if artists:
        return [a for a in artists if a]
    subtitle = (e.get("subtitle") or "").replace("\xa0", " ")
    if subtitle:
        return [a.strip() for a in subtitle.split(",") if a.strip()]
    return ["Unknown"]

def _year_from_release(value) -> str:
    # Spotify may return release dates as plain strings or dicts (e.g. {"isoString": "..."}).
    if isinstance(value, dict):
        value = value.get("isoString") or value.get("date") or value.get("value") or ""
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) >= 4 and s[:4].isdigit():
        return s[:4]
    m = re.search(r"(19|20)\d{2}", s)
    return m.group(0) if m else ""

def _track_from_entity(e: dict, album_override: dict | None = None) -> Track:
    album  = album_override or e.get("album") or {}
    images = (
        album.get("images")
        or e.get("images")
        or _nav(album, "visualIdentity", "image")
        or _nav(e, "visualIdentity", "image")
        or []
    )
    artists = _artists_from_entity(e)
    rd = album.get("release_date") or album.get("releaseDate") or e.get("release_date") or e.get("releaseDate") or ""
    return Track(
        id           = e.get("id") or _id_from_uri(e.get("uri", "")),
        title        = e.get("name") or e.get("title") or "Unknown",
        artist       = artists[0],
        album        = album.get("name") or album.get("title") or e.get("album") or e.get("name") or e.get("title") or "Unknown",
        track_num    = e.get("track_number") or 1,
        total        = album.get("total_tracks") or e.get("total_tracks") or 1,
        duration_sec = (e.get("duration") or e.get("duration_ms", 0)) / 1000,
        year         = _year_from_release(rd),
        art_url      = _img(images),
    )

def _tracks_from_embed(kind: str, sid: str) -> Optional[list[Track]]:
    data = _embed_json(kind, sid)
    if not data:
        return None

    # Navigate to the entity - Spotify's __NEXT_DATA__ structure
    entity = (
        _nav(data, "props", "pageProps", "state", "data", "entity") or
        _nav(data, "props", "pageProps", "state", "data", default={})
    )
    if not entity or "id" not in entity:
        raise MetadataError("schema_change", "Spotify embed schema did not include an expected entity object")

    if kind == "track":
        return [_track_from_entity(entity)]

    if kind == "album":
        album_stub = {
            "id": entity.get("id"),
            "name": entity.get("name") or entity.get("title"),
            "release_date": entity.get("release_date") or entity.get("releaseDate", ""),
            "images": entity.get("images") or _nav(entity, "visualIdentity", "image") or [],
            "total_tracks": entity.get("total_tracks") or len(entity.get("trackList") or []) or 0,
        }
        items = _nav(entity, "tracks", "items") or entity.get("trackList") or []
        tracks: list[Track] = []
        for idx, t in enumerate(items, start=1):
            if not isinstance(t, dict):
                continue
            if "id" not in t and "uri" not in t:
                continue
            if "track_number" not in t:
                t = {**t, "track_number": idx}
            tr = _track_from_entity(t, album_stub)
            if tr.id:
                tracks.append(tr)
        return tracks or None

    if kind == "playlist":
        tracks = []
        for item in (_nav(entity, "tracks", "items") or []):
            t = item.get("track") or item
            if t and t.get("id"):
                try:
                    tracks.append(_track_from_entity(t))
                except Exception:
                    pass
        return tracks or None

    return None

# ── Anonymous token fallback (method 2) ──────────────────────────────────────

_token: str = ""
_token_exp: float = 0.0

def _anon_token() -> str:
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    r = _get(
        "https://open.spotify.com/get_access_token",
        params={"reason": "transport", "productType": "web_player"},
        headers={"app-platform": "WebPlayer", "spotify-app-version": "1.2.46.372"},
        timeout=10,
    )
    d = r.json()
    _token = d["accessToken"]
    _token_exp = d["accessTokenExpirationTimestampMs"] / 1000
    return _token

def _api(path: str) -> dict:
    tok = _anon_token()
    r = _get(
        f"https://api.spotify.com/v1/{path}",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=12,
    )
    return r.json()

def _tracks_from_api(kind: str, sid: str) -> Optional[list[Track]]:
    try:
        if kind == "track":
            return [_track_from_entity(_api(f"tracks/{sid}"))]
        if kind == "album":
            d = _api(f"albums/{sid}")
            stub = {
                "id": d["id"], "name": d["name"],
                "release_date": d.get("release_date", ""),
                "images": d.get("images", []),
                "total_tracks": d.get("total_tracks", 0),
            }
            tracks = []
            results = d.get("tracks", {})
            while results:
                for t in results.get("items", []):
                    tracks.append(_track_from_entity(t, stub))
                next_url = results.get("next")
                results = (_get(next_url, headers={"Authorization": f"Bearer {_anon_token()}"}, timeout=12).json()
                           if next_url else None)
            return tracks
        if kind == "playlist":
            d = _api(f"playlists/{sid}")
            tracks = []
            results = d.get("tracks", {})
            while results:
                for item in results.get("items", []):
                    t = item.get("track")
                    if t and t.get("id"):
                        try: tracks.append(_track_from_entity(t))
                        except Exception: pass
                next_url = results.get("next")
                results = (_get(next_url, headers={"Authorization": f"Bearer {_anon_token()}"}, timeout=12).json()
                           if next_url else None)
            return tracks or None
    except requests.RequestException:
        raise
    except KeyError as e:
        raise MetadataError("schema_change", f"Spotify API response missing expected field: {e}") from e
    except Exception:
        return None

def _classify_error(exc: Exception) -> str:
    if isinstance(exc, MetadataError):
        return exc.code
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {401, 402, 403, 429}:
            return "rate_limit"
        return "network"
    if isinstance(exc, requests.RequestException):
        return "network"
    return "network"

def _human_error(code: str) -> str:
    if code == "rate_limit":
        return (
            "Could not fetch Spotify metadata.\n"
            "  Error class: rate_limit\n"
            "  Spotify is blocking or throttling this IP/session.\n"
            "  Try again later or use a different network."
        )
    if code == "schema_change":
        return (
            "Could not fetch Spotify metadata.\n"
            "  Error class: schema_change\n"
            "  Spotify changed response format and parser fallback did not match.\n"
            "  Please update eatmusic."
        )
    return (
        "Could not fetch Spotify metadata.\n"
        "  Error class: network\n"
        "  Requests to Spotify failed after retries.\n"
        "  Check connection and try again."
    )

# ── Public entry point ────────────────────────────────────────────────────────

def get_tracks(url: str) -> list[Track]:
    """
    Resolve a Spotify URL to a list of Track objects.
    Tries embed scraping first, then anonymous token API.
    """
    kind, sid = parse_url(url)

    errors: list[str] = []
    for attempt in range(3):
        for source in (_tracks_from_embed, _tracks_from_api):
            try:
                tracks = source(kind, sid)
                if tracks:
                    return tracks
            except Exception as e:
                errors.append(_classify_error(e))
        if attempt < 2:
            time.sleep(1.5 + attempt * 1.5)
    code = "network"
    if "rate_limit" in errors:
        code = "rate_limit"
    elif "schema_change" in errors:
        code = "schema_change"
    raise MetadataError(code, _human_error(code))

def fetch_art(url: str) -> bytes:
    if not url:
        return b""
    try:
        return _get(url, timeout=10).content
    except Exception:
        return b""
