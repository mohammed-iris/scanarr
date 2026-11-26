# Running Scanarr in Docker

There are two ways to run it:

- **A. On your Windows laptop now** (Docker Desktop): only Scanarr runs in Docker. It uses the qBittorrent and Prowlarr apps you already have.
- **B. On the UGREEN NAS later:** Scanarr, qBittorrent and Prowlarr all run as containers.

Scanarr is built from this folder (`docker compose ... --build`), so there's no image to download.

---

## A. Windows laptop with Docker Desktop

**1. Install Docker Desktop**
Download it from docker.com, install it, and restart if asked. Start Docker Desktop and wait until it says *Engine running*.

**2. Make the two folders and point qBittorrent at them**
- Create `C:\Scanarr\temp` and `C:\Scanarr\completed`.
- In qBittorrent → Options → **Downloads**:
  - Default Save Path: `C:\Scanarr\completed`
  - Tick **Use another path for incomplete torrents**: `C:\Scanarr\temp`
- In qBittorrent → Options → **Web UI**: enable it (your port is **9090**) and **generate an API key**.

**3. Start Scanarr**
Unzip Scanarr. In the `scanarr` folder, right-click → *Open in Terminal*, then run:

```powershell
docker compose -f docker-compose.windows.yml up -d --build
```

The first build takes a couple of minutes.

**4. Log in**
Run this to see the login:

```powershell
docker logs scanarr
```

Then open **http://localhost:8787**.

**5. Settings**

| Setting | Value |
|---|---|
| Prowlarr URL | `http://host.docker.internal:9696` + Prowlarr API key |
| qBittorrent URL | `http://host.docker.internal:9090` + qBittorrent API key |
| Path map (Folders & Renaming) | from `C:\Scanarr` to `/data` |

Click **Test & sync indexers** and **Test connection**. Your two folders should appear under *Folders & Renaming*.

`host.docker.internal` is how a container reaches apps on your laptop. The path map tells Scanarr that qBittorrent's `C:\Scanarr\...` is `/data/...` inside the container.

**Phone on the same Wi-Fi:** open `http://<laptop-IP>:8787`. Get the IP from `ipconfig` (IPv4 Address).

---

## B. UGREEN DXP2800 (full stack)

**1. Prepare the NAS**
- In UGOS: install the **Docker** app from the App Center, and enable **SSH** (Control Panel → Terminal).
- Create the folders. Put Temp and the app configs on the SSD, and media on the HDD. Volume numbers depend on how you set up your storage pools; check them in Storage Manager.

  ```
  /volume2/scanarr/temp          (SSD)  unfinished downloads
  /volume2/docker/scanarr        (SSD)  Scanarr settings + history
  /volume2/docker/qbittorrent    (SSD)
  /volume2/docker/prowlarr       (SSD)
  /volume1/media                 (HDD)  Completed: Movies/, TV/, Audio/ ... appear here
  ```

**2. Copy Scanarr to the NAS**
Copy the `scanarr` folder to e.g. `/volume2/docker/scanarr-src`, over SMB or File Manager.

**3. Edit `docker-compose.yml`**
- Change `/volume1` and `/volume2` if your volumes are numbered differently.
- Set **PUID/PGID** to your NAS user. Get them over SSH:

  ```bash
  ssh youruser@NAS-IP
  id          # e.g. uid=1000(iris) gid=10(admin)  ->  PUID=1000, PGID=10
  ```

  Use the same values in all three services and in Scanarr's `args`.

**4. Start everything**
Over SSH:

```bash
cd /volume2/docker/scanarr-src
sudo docker compose up -d --build
```

<details><summary>Prefer the UGOS Docker app instead of SSH?</summary>

Docker app → **Project** → **Create** → choose `/volume2/docker/scanarr-src` as the project path. It will pick up `docker-compose.yml`. Then deploy. If the app can't build from a folder, use the SSH command once; after that you can start, stop and see logs from the app.
</details>

**5. Set up qBittorrent** at `http://NAS-IP:9090`
- First password: `sudo docker logs qbittorrent`, then change it.
- Options → Downloads: **Default Save Path** `/data/completed`, **incomplete** `/data/temp`.
- Default Torrent Management Mode: **Manual**.
- Options → Web UI → API Key → **Generate**.

**6. Set up Prowlarr** at `http://NAS-IP:9696`: add your indexers (YTS, The Pirate Bay, Knaben…).

**7. Set up Scanarr** at `http://NAS-IP:8787`

Get the login with `sudo docker logs scanarr`. Then in Settings:

| Setting | Value |
|---|---|
| Prowlarr URL | `http://prowlarr:9696` + API key |
| qBittorrent URL | `http://qbittorrent:9090` + API key |
| Path map | leave empty: all containers see the same `/data` paths |

**8. Share the media folder to the TV:** SMB share or DLNA on `/volume1/media`.

---

## Everyday commands

| What | Command (add `-f docker-compose.windows.yml` on the laptop) |
|---|---|
| See the login / logs | `docker logs scanarr` (live: `docker logs -f scanarr`) |
| Stop | `docker compose stop` |
| Start | `docker compose start` |
| Restart | `docker compose restart scanarr` |
| **Update Scanarr** (new zip from me) | replace the files (keep `config/`), then `docker compose up -d --build` |
| Is it healthy? | `docker ps` shows `(healthy)` next to scanarr |
| Remove (keeps settings) | `docker compose down` |

## Options you can set in the compose file

| Setting | Where | Effect |
|---|---|---|
| Web UI port | `ports: ["8787:8787"]` | change the **left** number, e.g. `"8899:8787"` |
| Fixed login | `SCANARR_USERNAME`, `SCANARR_PASSWORD` | instead of the generated password |
| Time zone | `TZ=Asia/Dubai` | schedule times and log timestamps |
| File owner | `PUID` / `PGID` build args | renamed files belong to your NAS user |
| Log detail | `LOG_LEVEL=DEBUG` | more detail in `docker logs` |

## Troubleshooting

| Problem | Fix |
|---|---|
| Forgot the login | `docker logs scanarr`: it's printed at every start |
| "Too many wrong attempts" | wait 5 minutes, or `docker restart scanarr` |
| qBittorrent / Prowlarr "unreachable" | NAS: use the service names `http://qbittorrent:9090` / `http://prowlarr:9696`. Laptop: `http://host.docker.internal:...` |
| Folders show "not available" | qBittorrent needs a Default Save Path. On the laptop, check the **path map** (`C:\Scanarr` → `/data`) |
| Files aren't renamed, Log says "files not found on disk" | Scanarr and qBit must see the same folders: same `/data` mounts on the NAS, path map on the laptop |
| "Permission denied" in the Log | PUID/PGID don't match the owner of your media folder. Fix them and run `docker compose up -d --build` |
| Port already in use | change the left port number in the compose file |
