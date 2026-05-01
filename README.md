# Eat CLI

Download Spotify tracks, albums, and playlists as tagged `.m4a` files.  
Finds the best YouTube match, pulls lyrics, and writes full tags — one command.

```
eat <spotify_url>
```

---

## Install

```bash
curl -fsSL https://webjuniors.org/eatcli/install.sh | bash
```

Requires Python 3.10+, pip/pipx, and ffmpeg. Handles everything on Debian/Ubuntu/Zorin.  
After install, restart your terminal or run `source ~/.bashrc`.

---

## Usage

```bash
eat https://open.spotify.com/track/...
eat https://open.spotify.com/album/...
eat https://open.spotify.com/playlist/...
```

For all options:

```bash
eat --help
```

---

## First run

```bash
eat --setup
```

Sets your music folder (default `~/Music`) and optionally configures YouTube cookies for age-restricted content.

---

## Uninstall

```bash
curl -fsSL https://webjuniors.org/eatcli/install.sh | bash -s -- --uninstall
```

---

## Notes

- Works on Spotify **public** content. No Spotify account needed.
- YouTube matching uses fuzzy scoring on title, artist, and duration.
- Lyrics fetched from [lrclib.net](https://lrclib.net), embedded in the file.
- Age-restricted YouTube tracks require cookies (`--setup-youtube`).
