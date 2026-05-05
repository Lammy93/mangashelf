# MangaShelf 📚

A self-hosted manga reading app with a dark web UI, running entirely in Docker.

## Features
- 📖 Read CBZ, CBR, PDF, ZIP, RAR, EPUB
- 🔁 Three reading modes: Single page, Double page spread, Long strip
- 🔍 Search & download from MangaDex (built-in)
- ➕ Add any custom source URL
- 💾 Auto-saves reading progress per chapter
- 🎨 Dark, clean UI

---

## Unraid Setup

### 1. Copy files to your server
Transfer the `mangaapp` folder to your Unraid server, e.g.:
```
/mnt/user/appdata/mangashelf/
```

### 2. Adjust paths in docker-compose.yml
The default paths are:
```yaml
- /mnt/user/data/media/manga:/manga        # your manga library
- /mnt/user/appdata/mangashelf/data:/data  # app database + cache
```
Change the left side of each line to match your actual share paths.

### 3. Create the appdata folder
```bash
mkdir -p /mnt/user/appdata/mangashelf/data
```

### 4. Build and run
Open the Unraid terminal and run:
```bash
cd /mnt/user/appdata/mangashelf
docker-compose up --build -d
```

First build takes ~2-3 minutes. After that:
```bash
docker-compose up -d    # start
docker-compose down     # stop
docker-compose logs -f  # view logs
```

### 5. Open the app
Visit: **http://YOUR-UNRAID-IP:8080**

---

## Folder structure for your manga
```
/mnt/user/data/media/manga/
  My Manga Title/
    Chapter_01.cbz
    Chapter_02.cbz
  Another Manga.pdf
```

---

## Reader controls
| Action | Input |
|---|---|
| Next page | Click right side / Arrow Right / Arrow Down |
| Previous page | Click left side / Arrow Left / Arrow Up |
| Hide toolbar | H key or ⊙ button |
| Change mode | Single / Double / Strip buttons in toolbar |

---

## Updating
```bash
cd /mnt/user/appdata/mangashelf
docker-compose down
docker-compose up --build -d
```
