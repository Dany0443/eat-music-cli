"""
eat.py — download + tag + CLI.

One file for everything except Spotify metadata.
"""

from __future__ import annotations

import json, re, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Optional

import click, requests, yt_dlp
from mutagen.mp4 import MP4, MP4Cover
from rapidfuzz import fuzz
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .meta import Track, get_tracks, fetch_art, parse_url

C = Console()

# ── Config / Cache ────────────────────────────────────────────────────────────

_CFG  = Path.home() / ".config" / "eatmusic.json"
_CACHE = Path.home() / ".cache"  / "eatmusic.json"
_YT_COOKIES = Path.home() / ".config" / "eatmusic" / "ytcookies"
_UPDATE_URL = "https://webjuniors.org/eatcli/version.txt"
_REINSTALL_URL = "https://webjuniors.org/eatcli/install.sh"
_UPDATE_INTERVAL_SEC = 24 * 60 * 60

def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def cfg_get(key: str, default):
    return _read(_CFG).get(key, default)

def cfg_set(**kwargs) -> None:
    d = _read(_CFG); d.update(kwargs); _write(_CFG, d)

def _store_cookie_file(src: str) -> str:
    src = _normalize_path_str(src)
    p = Path(src).expanduser()
    if not p.exists() or not p.is_file():
        raise ValueError(f"Cookie file not found: {p}")
    _YT_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    _YT_COOKIES.write_bytes(p.read_bytes())
    try:
        _YT_COOKIES.chmod(0o600)
    except Exception:
        pass
    return str(_YT_COOKIES)

def _normalize_path_str(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and ((s[0] == s[-1]) and s[0] in {"'", '"'}):
        s = s[1:-1].strip()
    return s

def cache_get(key: str) -> Optional[str]:
    return _read(_CACHE).get(key)

def cache_set(key: str, val: str) -> None:
    d = _read(_CACHE); d[key] = val; _write(_CACHE, d)

def _current_version() -> str:
    try:
        return pkg_version("eatmusic")
    except Exception:
        return "0.0.0"

def _version_parts(v: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", v)]
    return tuple(nums[:4]) if nums else (0,)

def _is_newer_version(latest: str, current: str) -> bool:
    a = _version_parts(latest)
    b = _version_parts(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))

def _maybe_print_update_notice(force: bool = False, show_when_latest: bool = False) -> None:
    current = _current_version()
    c = _read(_CACHE)
    now = time.time()
    latest = ""
    checked = float(c.get("update_checked_at") or 0.0)
    if not force and checked > 0 and now - checked < _UPDATE_INTERVAL_SEC:
        latest = str(c.get("update_latest") or "").strip()
    else:
        try:
            r = requests.get(
                _UPDATE_URL,
                timeout=3.0,
                headers={"User-Agent": f"eatmusic/{current}"},
            )
            if r.status_code == 200:
                raw = (r.text or "").strip().splitlines()[0]
                latest = raw.split("#")[0].strip()
            c["update_checked_at"] = now
            c["update_latest"] = latest
            _write(_CACHE, c)
        except Exception:
            return
    if latest and _is_newer_version(latest, current):
        C.print(f"[yellow]Update available: {current} → {latest}[/yellow]")
        C.print(f"[dim]curl -fsSL {_REINSTALL_URL} | bash[/dim]")
    elif show_when_latest:
        C.print(f"[green]You are up to date ({current})[/green]")

# ── Filesystem helpers ────────────────────────────────────────────────────────

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def clean(s: str) -> str:
    return _BAD.sub("_", s).strip(". ")[:180] or "_"

def out_path(music_dir: str, t: Track, album_root_artist: str | None = None) -> Path:
    w  = max(len(str(t.total)), 2)
    fn = f"{str(t.track_num).zfill(w)} - {clean(t.title)}.m4a"
    root_artist = album_root_artist or t.artist
    p  = Path(music_dir) / clean(root_artist) / clean(t.album)
    p.mkdir(parents=True, exist_ok=True)
    return p / fn

def choose_album_root_artist(
    tracks: list[Track],
    various_threshold: int = 3,
) -> str:
    """
    Choose a single root artist folder for an album.
    - If album looks like a compilation, use "Various Artists".
    - Otherwise use dominant artist.
    """
    names = [t.artist.strip() for t in tracks if t.artist and t.artist.strip()]
    if not names:
        return "Unknown Artist"
    c = Counter(names)
    dominant, dominant_n = c.most_common(1)[0]
    distinct = len(c)
    total = len(names)
    dominant_share = dominant_n / total if total else 1.0
    is_compilation = (
        distinct >= max(various_threshold, 4)
        or (distinct >= various_threshold and dominant_share < 0.80)
    )
    return "Various Artists" if is_compilation else dominant

# ── YouTube search + matching ─────────────────────────────────────────────────

_NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|hd|4k|remaster(?:ed)?|"
    r"explicit|clean|feat\.?|ft\.?|prod\.?|remix|live|acoustic|"
    r"cover|karaoke|nightcore|visuali[sz]er)\b",
    re.I,
)
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_VEVO  = re.compile(r"\b(vevo|official|records?|music)\b", re.I)

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", _PAREN.sub(" ", s))).strip().lower()

def _score(t: Track, title: str, channel: str, dur: float) -> float:
    if t.duration_sec > 0:
        ratio = dur / t.duration_sec if dur > 0 else 0
        if not (0.4 <= ratio <= 2.0):
            return 0.0
    ts = fuzz.token_set_ratio(_norm(t.title), _norm(title))
    ar = max(fuzz.partial_ratio(t.artist.lower(), f"{title} {channel}".lower()),
             fuzz.token_set_ratio(t.artist.lower(), f"{title} {channel}".lower()))
    ds = max(0.0, 100 - abs(t.duration_sec - dur) * 2) if t.duration_sec > 0 else 50.0
    ob = 8.0 if _VEVO.search(f"{title} {channel}") else 0.0
    return min(ts * 0.40 + ar * 0.30 + ds * 0.30 + ob, 100.0)

def find_youtube_candidates(t: Track, limit: int = 3) -> list[str]:
    """Search YouTube and return best matching candidate URLs."""
    cached = cache_get(f"yt:{t.id}")
    candidates: list[tuple[float, str]] = []
    seen: set[str] = set()
    if cached:
        seen.add(cached)
        candidates.append((100.0, cached))

    queries = [
        f"{t.artist} - {t.title} official audio",
        f"{t.artist} {t.title}",
    ]
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    for query in queries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch8:{query}", download=False)
                for e in (info.get("entries") or []):
                    if not e:
                        continue
                    vid   = e.get("id") or ""
                    url   = f"https://www.youtube.com/watch?v={vid}" if vid else ""
                    s     = _score(t, e.get("title",""), e.get("channel") or e.get("uploader",""), float(e.get("duration") or 0))
                    if url and s >= 35 and url not in seen:
                        seen.add(url)
                        candidates.append((s, url))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [u for _, u in candidates[:max(1, limit)]]

def find_youtube(t: Track) -> Optional[str]:
    c = find_youtube_candidates(t, limit=1)
    return c[0] if c else None

# ── Download ──────────────────────────────────────────────────────────────────

def _cookies_from_browser_arg(val: str):
    parts = [p.strip() for p in val.split(":", 1)]
    if not parts[0]:
        return None
    if len(parts) == 1:
        return (parts[0], None, None, None)
    return (parts[0], parts[1] or None, None, None)

def download(
    yt_url: str,
    dest: Path,
    yt_cookies: str = "",
    yt_cookies_from_browser: str = "",
    yt_sleep: float = 0.0,
) -> bool:
    """Download best audio and convert to .m4a."""
    import tempfile, shutil
    with tempfile.TemporaryDirectory(prefix="eatmusic_") as tmp:
        tmpl = str(Path(tmp) / "dl.%(ext)s")
        opts = {
            "format":    "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl":   tmpl,
            "quiet":     True,
            "no_warnings": True,
            "retries":   5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "postprocessors": [{
                "key":            "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }],
        }
        if yt_cookies:
            opts["cookiefile"] = yt_cookies
        if yt_cookies_from_browser:
            cb = _cookies_from_browser_arg(yt_cookies_from_browser)
            if cb:
                opts["cookiesfrombrowser"] = cb
        if yt_sleep > 0:
            opts["sleep_interval_requests"] = yt_sleep
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([yt_url])
        except Exception:
            return False

        produced = next(
            (f for ext in ("m4a","aac","opus","webm","mp3","ogg")
             for f in Path(tmp).glob(f"*.{ext}")), None
        )
        if not produced:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dest))
    return dest.exists() and dest.stat().st_size > 10_000

# ── Tagging ───────────────────────────────────────────────────────────────────

def tag(path: Path, t: Track, art: bytes, lyrics: str) -> None:
    try:
        audio = MP4(str(path))
        tags: dict = {
            "\xa9nam": [t.title],
            "\xa9ART": [t.artist],
            "\xa9alb": [t.album],
            "aART":    [t.artist],
            "trkn":    [(t.track_num, t.total)],
        }
        if t.year:
            tags["\xa9day"] = [t.year]
        if lyrics:
            tags["\xa9lyr"] = [lyrics]
        if art:
            fmt = MP4Cover.FORMAT_PNG if art[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(art, imageformat=fmt)]
        audio.update(tags)
        audio.save()
    except Exception:
        pass

# ── Lyrics ────────────────────────────────────────────────────────────────────

_LRC_TS = re.compile(r"\[\d+:\d+\.\d+\]")

def get_lyrics(artist: str, title: str, dur: float) -> str:
    try:
        r = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title, "duration": int(dur)},
            headers={"User-Agent": "eatmusic/2.0"},
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            text = d.get("plainLyrics") or _LRC_TS.sub("", d.get("syncedLyrics") or "")
            return text.strip()
    except Exception:
        pass
    return ""

def _failed_json_path(music_dir: str) -> Path:
    return Path(music_dir) / "failed.json"

def _track_key(t: Track) -> str:
    return f"{t.id}|{t.album}|{t.title}"

def _track_to_dict(t: Track, msg: str, source_url: str) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "artist": t.artist,
        "album": t.album,
        "track_num": t.track_num,
        "total": t.total,
        "duration_sec": t.duration_sec,
        "year": t.year,
        "art_url": t.art_url,
        "last_error": msg,
        "source_url": source_url,
        "updated_at": int(time.time()),
    }

def _track_from_dict(d: dict) -> Track:
    return Track(
        id=str(d.get("id") or ""),
        title=str(d.get("title") or "Unknown"),
        artist=str(d.get("artist") or "Unknown"),
        album=str(d.get("album") or "Unknown"),
        track_num=int(d.get("track_num") or 1),
        total=int(d.get("total") or 1),
        duration_sec=float(d.get("duration_sec") or 0),
        year=str(d.get("year") or ""),
        art_url=str(d.get("art_url") or ""),
    )

def _load_failed_queue(music_dir: str) -> list[dict]:
    p = _failed_json_path(music_dir)
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_failed_queue(music_dir: str, items: list[dict]) -> None:
    p = _failed_json_path(music_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2))

# ── Process one track ─────────────────────────────────────────────────────────

def process(
    t: Track,
    music_dir: str,
    album_root_artist: str | None,
    force: bool,
    no_lyrics: bool,
    dry_run: bool,
    yt_cookies: str,
    yt_cookies_from_browser: str,
    yt_sleep: float,
) -> tuple[bool, str]:
    """
    Returns (success, message).
    """
    dest = out_path(music_dir, t, album_root_artist=album_root_artist)

    # Already done?
    if not force and dest.exists() and dest.stat().st_size > 10_000:
        return True, "skipped"

    # Find YouTube match candidates
    yt_candidates = find_youtube_candidates(t, limit=3)
    if not yt_candidates:
        return False, "no YouTube match found"

    if dry_run:
        return True, f"would download → {yt_candidates[0]}"

    # Download with alternate candidates
    chosen = ""
    for yt_url in yt_candidates:
        if download(yt_url, dest, yt_cookies, yt_cookies_from_browser, yt_sleep):
            chosen = yt_url
            break
    if not chosen:
        cache_set(f"yt:{t.id}", "")   # invalidate bad cache entry
        return False, "download failed after candidate retries"
    cache_set(f"yt:{t.id}", chosen)

    # Art + lyrics + tag
    art    = fetch_art(t.art_url)
    lyrics = "" if no_lyrics else get_lyrics(t.artist, t.title, t.duration_sec)
    tag(dest, t, art, lyrics)

    cache_set(f"dl:{t.id}", str(dest))
    return True, str(dest)

# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("url", required=False)
@click.option("--dry-run",    is_flag=True, help="Show matches, don't download.")
@click.option("--force",      is_flag=True, help="Re-download existing files.")
@click.option("--no-lyrics",  is_flag=True, help="Skip lyrics.")
@click.option("--workers",    default=None, type=int, help="Parallel workers (default 3).")
@click.option("--serial",     is_flag=True, help="Process tracks one by one (more reliable).")
@click.option("--music-dir",  default=None, help="Override music folder.")
@click.option("--setup",      is_flag=True, help="Set music folder.")
@click.option("--setup-youtube", is_flag=True, help="Set YouTube cookies / throttling options.")
@click.option("--yt-cookies", default=None, help="Path to YouTube cookies.txt (Netscape format).")
@click.option("--yt-cookies-from-browser", default=None, help="Browser cookie source, e.g. firefox or chrome:Default.")
@click.option("--yt-sleep",   default=None, type=float, help="Sleep seconds between yt-dlp requests.")
@click.option("--retry-failed", is_flag=True, help="Retry tracks from failed.json queue.")
@click.option("--no-update-check", is_flag=True, help="Skip automatic update check for this run.")
@click.option("--update-check", is_flag=True, help="Check updates now and print result.")
@click.option("--clear-cache",is_flag=True, help="Wipe the search cache.")
def main(
    url,
    dry_run,
    force,
    no_lyrics,
    workers,
    serial,
    music_dir,
    setup,
    setup_youtube,
    yt_cookies,
    yt_cookies_from_browser,
    yt_sleep,
    retry_failed,
    no_update_check,
    update_check,
    clear_cache,
):
    """eat <spotify_url>"""
    if update_check:
        _maybe_print_update_notice(force=True, show_when_latest=True)
        if not url and not retry_failed:
            return
    elif not no_update_check:
        _maybe_print_update_notice()

    if setup:
        default = cfg_get("music_dir", str(Path.home() / "Music"))
        current_cookie = cfg_get("yt_cookies", "")
        current_browser = cfg_get("yt_cookies_from_browser", "")
        current_sleep = cfg_get("yt_sleep", 0.0)

        C.print(Panel(
            "[bold]Setup[/bold]\n"
            "Configure download folder and optional YouTube auth for age-restricted tracks.\n"
            "You can skip cookies and still download normal tracks.",
            border_style="cyan",
            box=box.ROUNDED,
        ))
        d = C.input(f"[bold]Music folder[/bold] [dim](current: {default})[/dim]: ").strip()
        cookie_src = C.input(
            f"[bold]Cookie file (optional)[/bold] [dim](current: {current_cookie or 'none'})[/dim]: "
        ).strip()
        browser = C.input(
            f"[bold]Cookies from browser (optional)[/bold] [dim](current: {current_browser or 'none'})[/dim]: "
        ).strip()
        sleep_s = C.input(
            f"[bold]yt sleep seconds[/bold] [dim](current: {current_sleep})[/dim]: "
        ).strip()

        try:
            sleep_v = float(sleep_s) if sleep_s else float(current_sleep or 0.0)
        except Exception:
            sleep_v = float(current_sleep or 0.0)

        cookie_path = current_cookie
        if cookie_src:
            try:
                cookie_path = _store_cookie_file(cookie_src)
                C.print(f"[green]✓[/green] Cookie file stored at {cookie_path}")
            except Exception as e:
                C.print(f"[yellow]⚠[/yellow] {e}")
                C.print("[yellow]Continuing setup without changing cookie file.[/yellow]")

        cfg_set(
            music_dir=d or default,
            yt_cookies=cookie_path,
            yt_cookies_from_browser=(browser or current_browser),
            yt_sleep=max(sleep_v, 0.0),
        )
        C.print(f"[green]✓[/green] Saved: {d or default}")
        return

    if setup_youtube:
        current_cookie = cfg_get("yt_cookies", "")
        current_browser = cfg_get("yt_cookies_from_browser", "")
        current_sleep = cfg_get("yt_sleep", 0.0)
        C.print(Panel(
            "[bold]Cookie export quick steps[/bold]\n"
            "1) Open a private/incognito window and sign into YouTube.\n"
            "2) In the same tab, open https://www.youtube.com/robots.txt.\n"
            "3) Export [bold]youtube.com[/bold] cookies to a Netscape cookie file using a browser extension.\n"
            "4) Close that private window.\n"
            "Then set either cookie file or browser source below.",
            border_style="cyan",
            box=box.ROUNDED,
        ))
        cookie = C.input(f"[bold]Cookie file[/bold] [dim](current: {current_cookie or 'none'})[/dim]: ").strip()
        browser = C.input(f"[bold]Cookies from browser[/bold] [dim](current: {current_browser or 'none'})[/dim]: ").strip()
        sleep_s = C.input(f"[bold]yt sleep seconds[/bold] [dim](current: {current_sleep})[/dim]: ").strip()
        try:
            sleep_v = float(sleep_s) if sleep_s else float(current_sleep or 0.0)
        except Exception:
            sleep_v = float(current_sleep or 0.0)
        cookie_path = current_cookie
        if cookie:
            try:
                cookie_path = _store_cookie_file(cookie)
                C.print(f"[green]✓[/green] Cookie file stored at {cookie_path}")
            except Exception as e:
                C.print(f"[yellow]⚠[/yellow] {e}")
                C.print("[yellow]Continuing setup without changing cookie file.[/yellow]")
        cfg_set(
            yt_cookies=cookie_path,
            yt_cookies_from_browser=(browser or current_browser),
            yt_sleep=max(sleep_v, 0.0),
        )
        C.print("[green]✓[/green] YouTube settings saved.")
        return

    if clear_cache:
        _CACHE.unlink(missing_ok=True)
        C.print("[green]✓[/green] Cache cleared.")
        return

    if not url and not retry_failed:
        C.print("Usage: [bold]eat <spotify_url>[/bold]")
        sys.exit(1)

    mdir = music_dir or cfg_get("music_dir", str(Path.home() / "Music"))
    workers = workers if workers is not None else cfg_get("workers", 3)
    workers = max(1, int(workers))
    if serial:
        workers = 1
    yt_cookies = yt_cookies if yt_cookies is not None else cfg_get("yt_cookies", "")
    yt_cookies_from_browser = (
        yt_cookies_from_browser if yt_cookies_from_browser is not None
        else cfg_get("yt_cookies_from_browser", "")
    )
    yt_sleep = yt_sleep if yt_sleep is not None else float(cfg_get("yt_sleep", 0.0) or 0.0)
    yt_sleep = max(float(yt_sleep), 0.0)
    yt_cookies = _normalize_path_str(yt_cookies)
    yt_cookies = str(Path(yt_cookies).expanduser()) if yt_cookies else ""

    # ── Fetch metadata or retry queue ────────────────────────────────────────
    kind = "retry"
    if retry_failed:
        q = _load_failed_queue(mdir)
        tracks = [_track_from_dict(x) for x in q if isinstance(x, dict) and x.get("id")]
        if not tracks:
            C.print("[yellow]No entries in failed.json queue.[/yellow]")
            return
        C.print(f"[dim]Retrying {len(tracks)} track(s) from failed queue…[/dim]")
    else:
        C.print("[dim]Fetching Spotify metadata…[/dim]")
        try:
            tracks = get_tracks(url)
        except Exception as e:
            C.print(f"[red]✗[/red] {e}")
            sys.exit(1)
        kind, _ = parse_url(url)

    album_root_artist: str | None = None
    if kind == "album":
        album_root_artist = choose_album_root_artist(tracks, various_threshold=3)
    if kind in {"album", "playlist"} and workers > 1 and not dry_run:
        C.print("[yellow]Tip:[/yellow] multi-track URLs are more stable with [bold]--serial[/bold] or [bold]--workers 1[/bold].")
    label   = f"{tracks[0].artist} — " + (
        tracks[0].album if kind == "album" else
        tracks[0].title if kind == "track" else "Playlist"
    )
    C.print(Panel(
        f"[bold]{label}[/bold]  [dim]({len(tracks)} track{'s' if len(tracks)>1 else ''})[/dim]\n"
        f"[dim]→ {mdir}[/dim]" + ("  [yellow]dry-run[/yellow]" if dry_run else ""),
        box=box.ROUNDED, border_style="cyan",
    ))

    # ── Process ───────────────────────────────────────────────────────────────
    results: list[tuple[Track, bool, str]] = [None] * len(tracks)  # type: ignore
    t0 = time.monotonic()

    prog = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        console=C,
    )
    with prog:
        task = prog.add_task("[cyan]Working…[/cyan]", total=len(tracks))

        def _do(args):
            i, t = args
            ok, msg = process(
                t, mdir, album_root_artist, force, no_lyrics, dry_run,
                yt_cookies, yt_cookies_from_browser, yt_sleep,
            )
            return i, t, ok, msg

        if workers > 1 and not dry_run:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_do, (i, t)): i for i, t in enumerate(tracks)}
                for fut in as_completed(futs):
                    i, t, ok, msg = fut.result()
                    results[i] = (t, ok, msg)
                    icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
                    prog.update(task, advance=1, description=f"{icon} {t.artist} — {t.title}")
        else:
            for i, t in enumerate(tracks):
                i, t, ok, msg = _do((i, t))
                results[i] = (t, ok, msg)
                icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
                prog.update(task, advance=1, description=f"{icon} {t.artist} — {t.title}")
                if yt_sleep > 0 and not dry_run and i < len(tracks) - 1:
                    time.sleep(yt_sleep)

    # ── Summary ───────────────────────────────────────────────────────────────
    C.print()
    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0,1))
    tbl.add_column(width=3); tbl.add_column(); tbl.add_column(style="dim")
    for t, ok, msg in results:
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        note = "[dim]already had it[/dim]" if msg == "skipped" else (
               f"[dim]{msg}[/dim]" if ok else f"[red]{msg}[/red]"
        )
        tbl.add_row(icon, f"{t.artist} — {t.title}", note)
    C.print(tbl)

    ok_n   = sum(1 for _, ok, msg in results if ok and msg != "skipped")
    skip_n = sum(1 for _, ok, msg in results if msg == "skipped")
    fail_n = sum(1 for _, ok, _ in results if not ok)
    C.print(
        f"[green]{ok_n} downloaded[/green]  "
        + (f"[dim]{skip_n} skipped[/dim]  " if skip_n else "")
        + (f"[red]{fail_n} failed[/red]  " if fail_n else "")
        + f"[dim]{time.monotonic()-t0:.1f}s[/dim]"
    )

    # Log failures
    queue = _load_failed_queue(mdir)
    qmap = {f"{x.get('id','')}|{x.get('album','')}|{x.get('title','')}": x for x in queue if isinstance(x, dict)}
    for t, ok, msg in results:
        k = _track_key(t)
        if ok:
            qmap.pop(k, None)
        else:
            qmap[k] = _track_to_dict(t, msg, url or "")
    _save_failed_queue(mdir, list(qmap.values()))

    if fail_n:
        log = Path(mdir) / "failed.txt"
        with open(log, "a") as f:
            f.write(f"\n# {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            for t, ok, msg in results:
                if not ok:
                    f.write(f"{t.artist} — {t.title}  [{t.album}]  {msg}\n")
        C.print(f"[yellow]Failed tracks logged:[/yellow] {log}")
        C.print(f"[yellow]Failed queue updated:[/yellow] {_failed_json_path(mdir)}")

    sys.exit(0 if fail_n == 0 else 1)