# 🎬 YT-DLP API Server

A production-ready FastAPI wrapper for **yt-dlp** with download, streaming, metadata extraction, playlist support, and real-time progress tracking via WebSocket.

## 📁 Files Included

| File | Purpose |
|------|---------|
| `main.py` | Main FastAPI application (all endpoints) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Docker container config with FFmpeg |
| `railway.toml` | Railway.app deployment config |
| `render.yaml` | Render.com deployment config |
| `fly.toml` | Fly.io deployment config |

---

## 🚀 Quick Start (Local)

### 1. Install Dependencies

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install requirements
pip install -r requirements.txt

# Install FFmpeg (required!)
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg
# Windows: winget install Gyan.FFmpeg
```

### 2. Run the Server

```bash
python main.py
# OR
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Server runs at: `http://localhost:8000`

---

## 📡 API Endpoints

### `GET /` — API Info
Returns available endpoints and service info.

### `GET /api/status` — Server Status
Returns active downloads, queue status, and config.

### `POST /api/info?url=VIDEO_URL` — Extract Metadata
Get video info without downloading.

```bash
curl "http://localhost:8000/api/info?url=https://youtube.com/watch?v=..."
```

### `POST /api/formats?url=VIDEO_URL` — List Formats
Get all available video/audio formats with direct URLs.

```bash
curl "http://localhost:8000/api/formats?url=https://youtube.com/watch?v=..."
```

### `POST /api/stream` — Get Direct Stream URL
Returns a direct streaming URL (may expire).

```bash
curl -X POST http://localhost:8000/api/stream \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://youtube.com/watch?v=...",
    "quality": "best"
  }'
```

### `POST /api/download` — Download Video/Audio
Download to server storage.

```bash
curl -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://youtube.com/watch?v=...",
    "extract_audio": true,
    "audio_format": "mp3",
    "audio_quality": "192",
    "quality": "bestaudio"
  }'
```

**Response:**
```json
{
  "success": true,
  "download_id": "abc123...",
  "file_path": "/app/downloads/abc123_video_title.mp3",
  "file_size": 5242880,
  "filename": "abc123_video_title.mp3"
}
```

### `GET /api/download/{download_id}` — Serve File
Download the file from server.

```bash
curl -O http://localhost:8000/api/download/abc123
```

### `POST /api/playlist` — Playlist Info
Extract playlist metadata and entries.

```bash
curl -X POST http://localhost:8000/api/playlist \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://youtube.com/playlist?list=...",
    "extract_flat": true
  }'
```

### `WS /ws` — WebSocket Progress
Real-time download progress tracking.

```javascript
const ws = new WebSocket('ws://localhost:8000/ws');
ws.onopen = () => {
  ws.send(JSON.stringify({
    action: "subscribe",
    download_id: "abc123"
  }));
};
ws.onmessage = (event) => {
  console.log(JSON.parse(event.data));
};
```

---

## 🔧 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Server port |
| `HOST` | `0.0.0.0` | Server host |
| `DOWNLOAD_DIR` | `./downloads` | Download storage path |
| `CACHE_DIR` | `./cache` | Cache directory |
| `MAX_FILE_AGE_HOURS` | `24` | Auto-delete files after N hours |
| `MAX_CONCURRENT_DOWNLOADS` | `5` | Max simultaneous downloads |
| `COOKIE_FILE` | — | Path to cookies file |
| `USER_AGENT` | — | Custom User-Agent |
| `REFERER` | — | Custom Referer header |
| `GEO_BYPASS_COUNTRY` | — | Country code for geo bypass |

---

## 🐳 Docker Deployment

### Build & Run Locally

```bash
docker build -t ytdlp-api .
docker run -p 8000:8000 -v $(pwd)/downloads:/app/downloads ytdlp-api
```

### Docker Compose

```yaml
version: '3.8'
services:
  ytdlp-api:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./downloads:/app/downloads
      - ./cache:/app/cache
    environment:
      - MAX_FILE_AGE_HOURS=24
      - MAX_CONCURRENT_DOWNLOADS=5
    restart: unless-stopped
```

---

## ☁️ Free Cloud Deployment

### Railway.app (Recommended — Free Tier)

1. Push code to GitHub
2. Connect repo to Railway
3. Deploy automatically using `railway.toml`

```bash
# Or deploy via CLI
npm i -g @railway/cli
railway login
railway init
railway up
```

### Render.com (Free Tier)

1. Push code to GitHub
2. Create new Web Service on Render
3. Use `render.yaml` or configure manually:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 2`

### Fly.io (Free Tier)

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Launch
fly launch --dockerfile Dockerfile
fly deploy
```

### VPS / Self-Hosted (Linux)

```bash
# Clone repo
git clone <your-repo>
cd ytdlp-api

# Install dependencies
pip install -r requirements.txt

# Install FFmpeg
sudo apt update && sudo apt install ffmpeg

# Run with systemd (create service file)
sudo nano /etc/systemd/system/ytdlp-api.service
```

Service file:
```ini
[Unit]
Description=YT-DLP API Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/ytdlp-api
Environment="PATH=/opt/ytdlp-api/venv/bin"
ExecStart=/opt/ytdlp-api/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable ytdlp-api
sudo systemctl start ytdlp-api
```

---

## 🛡️ Production Tips

1. **Add Authentication** — Use API keys or OAuth for production
2. **Rate Limiting** — Add `slowapi` or nginx rate limiting
3. **Reverse Proxy** — Use nginx/traefik with SSL
4. **Persistent Storage** — Mount external volume for downloads
5. **Monitoring** — Add Prometheus metrics endpoint
6. **Cookies** — Export browser cookies to bypass bot checks:
   ```bash
   yt-dlp --cookies-from-browser chrome --cookies cookies.txt
   ```

---

## 📜 License

MIT — Use responsibly and respect copyright laws.
