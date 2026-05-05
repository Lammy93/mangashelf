<p align="center">
  <img src="assets/logo.png" alt="MangaShelf Logo" width="140"/>
</p>

<h1 align="center">MangaShelf</h1>

<p align="center">
  A self-hosted manga reading application with a modern dark interface, fully containerised with Docker.
</p>

---

## Overview

MangaShelf is a lightweight, self-hosted solution for reading and managing manga collections. It supports multiple file formats, flexible reading modes, and integrates with external sources for easy content access.

---

## Features

- Supports CBZ, CBR, PDF, ZIP, RAR, and EPUB formats  
- Multiple reading modes: single page, double page spread, and long strip  
- Built-in search and download integration 
- Ability to add custom source URLs  
- Automatic saving of reading progress per chapter  
- Clean, dark user interface  

---

## Installation (Docker)

### Prerequisites

- Docker installed  
- Docker Compose installed  

---
## Quick Start (Docker)

Run MangaShelf instantly:

``` bash
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/manga:/manga \
  -v $(pwd)/data:/data \
  --name mangashelf \
  your-dockerhub-username/mangashelf
```

Then open:

http://localhost:8080

2. Edit docker-compose.yml:

```services:
  mangashelf:
    build: .
    container_name: mangashelf
    restart: unless-stopped
    user: "99:100"
    ports:
      - "8035:8080"
    volumes:
      # Your manga files — adjust to your Unraid share path
      - /mnt/user/appdata/Manga:/manga
      # Persistent database + cache — stored in appdata
      - /mnt/user/appdata/mangashelf/data:/data
    environment:
      - TZ=Australia/Sydney
```

3. Create directories
mkdir -p manga data

4. Run the application
```docker-compose up --build -d```

## Manga Folder Structure
manga/
  My Manga Title/
    Chapter_01.cbz
    Chapter_02.cbz
  Another Manga.pdf

## Updating
```docker-compose down```
```docker-compose up --build -d```

## Notes
Ensure proper permissions are set for mounted volumes
Keep your manga library organised for best performance