# Installing Scanarr

The only thing you install yourself is **Docker**. A setup wizard does the rest: it asks for your folders, login and ports, checks every answer, then installs **Scanarr + qBittorrent + Prowlarr** already connected to each other, with a test at the end.

It works the same everywhere: a Windows laptop, a Linux PC or a NAS.

## 1. Start the setup wizard

Pick the one that matches your machine.

**Windows / Mac (Docker Desktop)** - open PowerShell or Terminal and paste:

```
docker run -d --name scanarr-setup -p 9700:9700 --user 0:0 -v /var/run/docker.sock:/var/run/docker.sock ghcr.io/mohammed-iris/scanarr setup
```

**Linux** - same command in a terminal (add `sudo` if your user isn't in the `docker` group).

**UGREEN NAS (or any NAS with a Docker web UI)**

1. Open **Docker → Project → Create**. Name it `scanarr-setup`.
2. Paste the contents of [`docker-compose.yml`](docker-compose.yml) from this repository and **Deploy**.

Then open **`http://<that-machine>:9700`** in a browser (for example `http://localhost:9700` on the laptop, or `http://192.168.1.50:9700` for the NAS).

> If your NAS's Docker UI won't let a container use `/var/run/docker.sock`, the wizard still works: at the end it hands you one compose file to paste into **Project → Create** yourself.

## 2. Answer the wizard

Every field has a recommendation, and is checked before anything is installed.

| Step | What to enter | Tip |
|---|---|---|
| Folders | **Temp** (unfinished downloads), **Completed** (finished files), **Config** (app settings) | On the NAS: Temp and Config on the **SSD**, Completed on the **HDD**, so the HDD can sleep. On Windows: e.g. `C:\Scanarr\temp`. Folders are created for you. |
| Login | One username and password for all three apps | Press **Generate** for a strong, readable password. |
| Ports | Scanarr's web page (**9797**) and the torrent port (random) | qBittorrent and Prowlarr get **no** ports of their own: you open them through Scanarr. |
| Indexers & subtitles | Where to search (YTS, The Pirate Bay, Knaben preselected), subtitle languages, optional TMDb / OpenSubtitles keys | Keys can be added later in Scanarr → Settings. |
| Extras | Time zone, phone notifications (ntfy), updates from inside Scanarr, Jellyfin, Tailscale | All optional. |

Press **Install**. The wizard downloads the apps, starts them and runs a test search. At the end you get a QR code to open Scanarr on your phone, and a button to remove the wizard.

## 3. Open Scanarr

`http://<that-machine>:9797`. Under **Apps** (the ▦ icon) you'll find buttons that open qBittorrent and Prowlarr without another login, the version, update notices and the setup report.

For best download speed, forward the torrent port (shown under Apps) on your router to this machine. Scanarr tells you whether it's reachable.

## Later

- **Change settings, repair, update or uninstall:** start the wizard again (step 1). It finds your installation and offers those options. Uninstall never deletes your Completed folder.
- **Update from inside Scanarr:** Apps → **Update now** appears when a new version is out (if you allowed updates in the wizard). Your settings are backed up first, and if the new version doesn't start, the old one comes back by itself.
- **Forgot the password:** it's printed at every start in the log of the `scanarr` container (Docker Desktop: Containers → scanarr → Logs; UGREEN: Docker → Container → scanarr → Log). You can also run the wizard → *Change settings* and set a new one.

## Where things are

```
Config/
  stack/compose.yml      the whole setup in one file (contains your login - keep it private)
  scanarr/               Scanarr's settings, history, library index, backups
  qbittorrent/  prowlarr/
Temp/                    unfinished downloads
Completed/
  Movies/  TV/           clean names + subtitles
  Audio/ Books/ Software/ Games/ XXX/ Other/
  Unsorted/              drop files here by hand; Scanarr renames them too
```

## Without the wizard

Scanarr also runs on its own next to a qBittorrent and Prowlarr you already have:
`docker run -d --name scanarr -p 9797:9797 -v /path/to/config:/config -v /path/to/completed:/path/to/completed ghcr.io/mohammed-iris/scanarr`
then fill in Settings → Prowlarr and qBittorrent. On Windows without Docker, double-click `run-windows.bat` (needs Python 3.11+).
