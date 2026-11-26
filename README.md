# Scanarr

One search box for every indexer in Prowlarr: movies, TV, music, software, books, anything. Press **Download**, and Scanarr sends the torrent to qBittorrent and files the result in the right place.

- **Movies and TV** get subtitles and a clean name, with no artwork or metadata files:
  `Movies/Inception (2010)/Inception (2010).mkv` + `.en.srt`, and `TV/Breaking Bad/Season 01/Breaking Bad S01E01.mkv` + `.en.srt`
- **Everything else** lands in its own folder with the original name: `Audio/`, `Books/`, `Software/`, `Games/`, `XXX/`, `Other/`
- **No seeding.** Finished torrents are removed from qBittorrent and the files are kept.

## Install

Install Docker, start the setup wizard, answer its questions. That's it. Same steps on Windows, Linux and a UGREEN NAS: **[INSTALL.md](INSTALL.md)**.

The wizard sets up Scanarr, qBittorrent and Prowlarr connected to each other, with:
one login for all three, qBittorrent's and Prowlarr's pages opened through Scanarr (no ports of their own), folders set, low upload while downloading and no seeding afterwards, your indexers added.

```
Temp/                         <- unfinished downloads (SSD)
Completed/                    <- finished files (HDD); Scanarr renames them here
  Incoming/                   <- raw movie/TV downloads waiting to be renamed
  Movies/  TV/                <- clean names + subtitles
  Audio/ Books/ Software/ Games/ XXX/ Other/
  Unsorted/                   <- drop files here by hand; Scanarr renames them too
```

Renaming is a move inside Completed, so it's instant, and only one copy exists.

## Keeping the HDD asleep

Scanarr wakes the HDD only when it has to. Here's every case where it could, and what it does instead:

| Action | Touches the HDD? |
|---|---|
| Searching | No: Prowlarr is queried over the internet |
| Sending a download | No: the magnet goes to qBit, which downloads into **Temp (SSD)** |
| Watching qBit (every 30 s) | No: qBittorrent's API only |
| "Already in your library?" check | No: answered from Scanarr's database on the SSD |
| Free-space check | No: Completed's free space comes from qBit's own report; Temp is the SSD |
| Fake-file guard, dead torrents | No: qBit API (dead partial files sit in Temp) |
| Missing-subtitle retries | Search: no (uses the file fingerprint stored at download time). Saving: parked on the SSD until the HDD is awake |
| **A download finishes** | **Yes, once.** qBit moves it to Completed (HDD) anyway; Scanarr renames it and adds subtitles in the same moment |
| Housekeeping (Unsorted scan, saving parked subtitles, library index) | Only while the HDD is already awake from a finished download (default). Or daily at a time, or only on "Check now" |
| Undo, approving a review, "Check now" | Yes, because you asked for it |

**No seeding** matters too: finished torrents are removed from qBit, so qBit never reads from the HDD to upload.

If something finished in qBit but its files aren't on disk, Scanarr checks a few times with growing gaps (1 min → 1 h) and then lists it under *Needs review*, instead of checking the disk forever.

Keep the Config folder on the **SSD** (the wizard recommends it). If you use Jellyfin, turn off its real-time library monitoring and schedule library scans, or it will wake the HDD itself.

**Big libraries (8-16 TB):** Scanarr keeps an index of your Movies/TV on the SSD. On a NAS (Linux) it follows file changes as they happen, so it rarely rescans; elsewhere, rescans only re-read folders that changed. A large `Unsorted/` catch-up runs in batches with progress in the status bar, and new downloads are still handled in between.

## Extras

- **Junk filter:** CAM, TS and screener releases are hidden by default. Results that are far too small for their claimed quality get a ⚠ flag (hide them too if you like).
- **★ Best:** the result with the best mix of your preferred quality, a sensible size and plenty of seeds. Set preferences in Settings → Prowlarr → Search preferences.
- **Peek files:** see what's inside before downloading. qBit fetches only the file list, with warnings for program files, archives and "password" files. Untick what you don't want (e.g. pick episodes from a season pack), then download.
- **Subs:** in Renamed, if a subtitle is wrong or out of sync, pick another. ✓ marks ones made for that exact file.
- **Command palette:** Ctrl+Shift+P or Ctrl+K.
- **Backup:** Settings → Backup downloads one file with settings and history, and restores from it.
- **Indexer health:** response time and success rate per indexer. Failing ones are skipped for 30 min, and you can "Retry now".
- **Apps:** open qBittorrent and Prowlarr (no extra login), see versions, whether your torrent port is reachable, update notices with **Update now**, and a QR code to open Scanarr on your phone.
- **Disk-full guard:** downloads pause before Temp or Completed fills up, and resume by themselves when there's room.
- **Slower downloads during set hours:** e.g. keep the internet free in the evening (Settings → Schedule).
- **Watchlist:** search, set filters, then **☆ Watch this search**. It's re-checked every 12 h (internet only). New releases appear under Watchlist and, if you set an **ntfy** URL, on your phone. Nothing downloads by itself.

## Using it

- **Search:** press `/`, type, hit Enter. Filter by category (XXX hidden by default), quality, seeders or indexer. `↑ ↓` selects, and `Enter` or double-click downloads. The details panel shows the final name, warns if you **already have it**, and lets you change the type.
- **Downloads:** pause, resume, recheck and remove (optionally with files). Ctrl- or Shift-click selects several, Ctrl+A selects all, `Del` removes.
- **Needs review:** anything Scanarr isn't sure about. Fix the name and approve. Nothing is touched until you do.
- **Renamed:** every rename, each with **Undo**.
- **Log:** blocked fakes, dead torrents, subtitle retries, errors.
- **Status bar:** qBittorrent, Prowlarr, free space in Temp and Completed, OpenSubtitles quota, review count.

## What makes it resilient

1. **Safe renaming:** Scanarr waits until qBit has fully moved the files out of Temp and every file is on disk at full size. It never overwrites an existing file, and every rename can be undone.
2. **Needs review:** unclear names, no TMDb match or a name clash go to review instead of being guessed.
3. **Fake-file guard:** movie and TV torrents containing `.exe`, `.scr`, `.lnk`, `.bat` and similar are removed as soon as their file list is known.
4. **Disk space check:** a download is refused if it would leave less than the reserve free (default 2 GB). You can override it per download.
5. **Dead torrents:** no progress for 6 hours means flagged, or removed if you choose.
6. **Subtitles first:** searched on the original downloaded file (exact file hash plus release name) before renaming. Subtitles shipped inside the torrent are used first. Missing ones are retried daily for 7 days.
7. **Catch-up:** after a restart, everything that finished in the meantime is processed. The `Unsorted/` drop folder handles files you add by hand.
8. **Duplicate warning** before you download something you already have.
9. **Login:** the username and password are printed at every start (`docker logs scanarr`, or the Scanarr window on Windows). You can fix them with `SCANARR_USERNAME` / `SCANARR_PASSWORD`.
10. **Health at a glance** in the status bar.

**Torrents added directly in qBittorrent** are handled too (switchable in Settings). Movies and episodes are detected from the file name and renamed. Anything else is left where qBit saved it, and the torrent is removed so it stops seeding.

## Releases

Push a tag `vX.Y.Z` matching `app/version.py`: GitHub Actions runs the tests, builds the image for Intel/AMD and ARM, publishes `ghcr.io/mohammed-iris/scanarr:X.Y.Z` and `:latest`, and creates a release. Each release pins the qBittorrent and Prowlarr versions it was tested with (`app/stack.py`), and updates move to those together.

## Development

```
pip install -r requirements.txt pytest
CONFIG_DIR=./config python -m app serve      # Scanarr on :9797
python -m app setup                           # setup wizard on :9700 (needs Docker)
pytest -q
```
