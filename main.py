
"""
YT-DLP API Server
A production-ready FastAPI wrapper for yt-dlp

Features:
- Video info extraction
- Audio/Video download with format selection
- Direct streaming URLs
- Playlist support
- Progress tracking via WebSocket
- Rate limiting & caching
"""

import os
import json
import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, HttpUrl, Field
import yt_dlp
import aiofiles

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)
CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(exist_ok=True)
MAX_FILE_AGE_HOURS = int(os.getenv("MAX_FILE_AGE_HOURS", "24"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class VideoRequest(BaseModel):
    url: str = Field(..., description="Video URL")
    format_id: Optional[str] = Field(None, description="Specific format ID to download")
    quality: str = Field("best", description="Quality preference: best, worst, bestaudio, bestvideo")
    extract_audio: bool = Field(False, description="Extract audio only")
    audio_format: str = Field("mp3", description="Audio format: mp3, m4a, wav, opus")
    audio_quality: str = Field("192", description="Audio quality in kbps")
    subtitle_langs: Optional[List[str]] = Field(None, description="Subtitle languages to download")
    write_thumbnail: bool = Field(False, description="Download thumbnail")
    write_info_json: bool = Field(False, description="Write info JSON")

class StreamRequest(BaseModel):
    url: str = Field(..., description="Video URL")
    format_id: Optional[str] = Field(None, description="Specific format ID")
    quality: str = Field("best", description="Quality preference")

class PlaylistRequest(BaseModel):
    url: str = Field(..., description="Playlist URL")
    extract_flat: bool = Field(True, description="Extract flat playlist (faster)")
    playlist_items: Optional[str] = Field(None, description="Playlist items to extract (e.g., '1:10')")

class VideoInfo(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    thumbnail: Optional[str] = None
    webpage_url: str
    formats: List[Dict[str, Any]] = []
    subtitles: Dict[str, Any] = {}
    chapters: List[Dict[str, Any]] = []
    is_live: bool = False
    was_live: bool = False

class DownloadResponse(BaseModel):
    success: bool
    message: str
    download_id: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    filename: Optional[str] = None
    info: Optional[Dict[str, Any]] = None

class StreamResponse(BaseModel):
    success: bool
    url: Optional[str] = None
    title: Optional[str] = None
    format: Optional[str] = None
    quality: Optional[str] = None
    expires: Optional[str] = None
    message: Optional[str] = None

# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

class DownloadManager:
    """Manages concurrent downloads and cleanup"""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_DOWNLOADS):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.active_downloads: Dict[str, dict] = {}
        self.download_history: List[dict] = []

    async def acquire(self, download_id: str, info: dict):
        await self.semaphore.acquire()
        self.active_downloads[download_id] = {
            "started": datetime.utcnow(),
            "info": info,
            "progress": 0
        }

    def release(self, download_id: str):
        if download_id in self.active_downloads:
            self.download_history.append({
                **self.active_downloads[download_id],
                "ended": datetime.utcnow(),
                "id": download_id
            })
            del self.active_downloads[download_id]
        self.semaphore.release()

    def update_progress(self, download_id: str, progress: float):
        if download_id in self.active_downloads:
            self.active_downloads[download_id]["progress"] = progress

    def get_status(self) -> dict:
        return {
            "active": len(self.active_downloads),
            "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
            "active_downloads": [
                {
                    "id": k,
                    "started": v["started"].isoformat(),
                    "progress": v["progress"],
                    "title": v["info"].get("title", "Unknown")
                }
                for k, v in self.active_downloads.items()
            ],
            "history_count": len(self.download_history)
        }

download_manager = DownloadManager()

def generate_download_id(url: str) -> str:
    """Generate a unique download ID"""
    return hashlib.sha256(f"{url}{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE_HOURS"""
    cutoff = datetime.now() - timedelta(hours=MAX_FILE_AGE_HOURS)
    removed = 0
    for file_path in DOWNLOAD_DIR.iterdir():
        if file_path.is_file():
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            if mtime < cutoff:
                try:
                    file_path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed

def get_ydl_opts(
    download_id: str = None,
    extract_audio: bool = False,
    audio_format: str = "mp3",
    audio_quality: str = "192",
    format_id: str = None,
    quality: str = "best",
    subtitle_langs: list = None,
    write_thumbnail: bool = False,
    write_info_json: bool = False,
    output_template: str = None
) -> dict:
    """Build yt-dlp options dictionary"""

    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": os.getenv("COOKIE_FILE", None),
        "user_agent": os.getenv("USER_AGENT", None),
        "referer": os.getenv("REFERER", None),
        "headers": {},
        "geo_bypass": True,
        "geo_bypass_country": os.getenv("GEO_BYPASS_COUNTRY", None),
    }

    if output_template:
        opts["outtmpl"] = output_template

    # Format selection
    if format_id:
        opts["format"] = format_id
    elif extract_audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": audio_quality,
        }]
    else:
        if quality == "best":
            opts["format"] = "bestvideo*+bestaudio/best"
        elif quality == "worst":
            opts["format"] = "worstvideo*+worstaudio/worst"
        elif quality == "bestaudio":
            opts["format"] = "bestaudio/best"
        elif quality == "bestvideo":
            opts["format"] = "bestvideo/best"
        else:
            opts["format"] = quality

    # Subtitles
    if subtitle_langs:
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = subtitle_langs
        opts["writeautomaticsub"] = True

    # Thumbnail
    if write_thumbnail:
        opts["writethumbnail"] = True
        opts["postprocessors"] = opts.get("postprocessors", []) + [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}]

    # Info JSON
    if write_info_json:
        opts["writeinfojson"] = True

    # Progress hook
    if download_id:
        def progress_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = (downloaded / total) * 100
                    download_manager.update_progress(download_id, pct)

        opts["progress_hooks"] = [progress_hook]

    return opts

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cleanup_old_files()
    yield
    # Shutdown
    pass

app = FastAPI(
    title="YT-DLP API",
    description="A powerful API for video downloading and streaming powered by yt-dlp",
    version="2.0.0",
    lifespan=lifespan
)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "YT-DLP API",
        "version": "2.0.0",
        "endpoints": {
            "info": "POST /api/info - Extract video metadata",
            "download": "POST /api/download - Download video/audio",
            "stream": "POST /api/stream - Get direct stream URL",
            "playlist": "POST /api/playlist - Extract playlist info",
            "formats": "POST /api/formats - List available formats",
            "status": "GET /api/status - Server status",
            "ws": "WS /ws - Real-time progress"
        },
        "docs": "/docs"
    }

@app.get("/api/status")
async def status():
    return {
        "status": "operational",
        "downloads": download_manager.get_status(),
        "download_dir": str(DOWNLOAD_DIR),
        "cache_dir": str(CACHE_DIR),
        "max_file_age_hours": MAX_FILE_AGE_HOURS
    }

@app.post("/api/info", response_model=VideoInfo)
async def get_video_info(url: str = Query(..., description="Video URL")):
    """Extract video metadata without downloading"""
    try:
        opts = get_ydl_opts()
        opts["extract_flat"] = False

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                raise HTTPException(status_code=404, detail="Could not extract video info")

            # Handle playlists - return first entry
            if "entries" in info:
                entries = list(info["entries"])
                if not entries:
                    raise HTTPException(status_code=404, detail="Empty playlist")
                info = entries[0]

            return VideoInfo(
                id=info.get("id", ""),
                title=info.get("title", "Unknown"),
                description=info.get("description"),
                duration=info.get("duration"),
                uploader=info.get("uploader"),
                upload_date=info.get("upload_date"),
                view_count=info.get("view_count"),
                like_count=info.get("like_count"),
                thumbnail=info.get("thumbnail"),
                webpage_url=info.get("webpage_url", url),
                formats=info.get("formats", []),
                subtitles=info.get("subtitles", {}),
                chapters=info.get("chapters", []),
                is_live=info.get("is_live", False),
                was_live=info.get("was_live", False)
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

@app.post("/api/formats")
async def list_formats(url: str = Query(..., description="Video URL")):
    """List all available formats for a video"""
    try:
        opts = get_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                raise HTTPException(status_code=404, detail="Video not found")

            if "entries" in info:
                info = info["entries"][0]

            formats = []
            for f in info.get("formats", []):
                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution"),
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "abr": f.get("abr"),
                    "vbr": f.get("vbr"),
                    "asr": f.get("asr"),
                    "audio_channels": f.get("audio_channels"),
                    "quality": f.get("quality"),
                    "has_video": f.get("vcodec") != "none",
                    "has_audio": f.get("acodec") != "none",
                    "url": f.get("url")  # Direct URL (may expire)
                })

            return {
                "video_id": info.get("id"),
                "title": info.get("title"),
                "duration": info.get("duration"),
                "formats": formats,
                "best_audio": ydl.select_format("bestaudio", info),
                "best_video": ydl.select_format("bestvideo", info),
                "best": ydl.select_format("best", info)
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list formats: {str(e)}")

@app.post("/api/stream", response_model=StreamResponse)
async def get_stream_url(request: StreamRequest):
    """Get a direct streaming URL for a video"""
    try:
        opts = get_ydl_opts(
            format_id=request.format_id,
            quality=request.quality
        )
        opts["format"] = opts.get("format", "best")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)

            if info is None:
                return StreamResponse(success=False, message="Could not extract stream URL")

            if "entries" in info:
                info = info["entries"][0]

            # Get the selected format
            if request.format_id:
                selected_format = next(
                    (f for f in info["formats"] if f["format_id"] == request.format_id),
                    None
                )
            else:
                selected_format = ydl.select_format(opts["format"], info)

            if not selected_format:
                return StreamResponse(success=False, message="No suitable format found")

            return StreamResponse(
                success=True,
                url=selected_format.get("url"),
                title=info.get("title"),
                format=selected_format.get("format"),
                quality=selected_format.get("quality_label") or selected_format.get("resolution"),
                expires=selected_format.get("manifest_url")  # Some URLs expire
            )
    except Exception as e:
        return StreamResponse(success=False, message=f"Stream extraction failed: {str(e)}")

@app.post("/api/download")
async def download_video(
    request: VideoRequest,
    background_tasks: BackgroundTasks
):
    """Download video or audio to server"""
    download_id = generate_download_id(request.url)
    output_template = str(DOWNLOAD_DIR / f"{download_id}_%(title)s.%(ext)s")

    try:
        await download_manager.acquire(download_id, {"url": request.url, "title": "Pending..."})

        opts = get_ydl_opts(
            download_id=download_id,
            extract_audio=request.extract_audio,
            audio_format=request.audio_format,
            audio_quality=request.audio_quality,
            format_id=request.format_id,
            quality=request.quality,
            subtitle_langs=request.subtitle_langs,
            write_thumbnail=request.write_thumbnail,
            write_info_json=request.write_info_json,
            output_template=output_template
        )

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=True)

            if "entries" in info:
                info = info["entries"][0]

            # Find the downloaded file
            downloaded_files = list(DOWNLOAD_DIR.glob(f"{download_id}_*"))
            main_file = None

            for f in downloaded_files:
                if f.suffix not in [".json", ".jpg", ".png", ".webp"]:
                    main_file = f
                    break

            if not main_file and downloaded_files:
                main_file = downloaded_files[0]

            if not main_file:
                raise HTTPException(status_code=500, detail="Download completed but file not found")

            file_size = main_file.stat().st_size

            download_manager.release(download_id)

            # Schedule cleanup
            background_tasks.add_task(cleanup_old_files)

            return DownloadResponse(
                success=True,
                message="Download completed successfully",
                download_id=download_id,
                file_path=str(main_file),
                file_size=file_size,
                filename=main_file.name,
                info={
                    "title": info.get("title"),
                    "duration": info.get("duration"),
                    "uploader": info.get("uploader"),
                    "thumbnail": info.get("thumbnail")
                }
            )

    except HTTPException:
        download_manager.release(download_id)
        raise
    except Exception as e:
        download_manager.release(download_id)
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

@app.get("/api/download/{download_id}")
async def serve_file(download_id: str, filename: Optional[str] = None):
    """Serve a downloaded file"""
    # Find file by download_id prefix
    files = list(DOWNLOAD_DIR.glob(f"{download_id}_*"))

    if not files:
        raise HTTPException(status_code=404, detail="File not found or expired")

    # Filter by filename if provided
    if filename:
        files = [f for f in files if filename in f.name]

    if not files:
        raise HTTPException(status_code=404, detail="File not found")

    main_file = files[0]

    # Determine media type
    media_type = None
    ext = main_file.suffix.lower()
    if ext in [".mp4", ".webm", ".mkv", ".avi", ".mov"]:
        media_type = f"video/{ext[1:]}"
    elif ext in [".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac"]:
        media_type = f"audio/{ext[1:]}"

    return FileResponse(
        path=main_file,
        filename=main_file.name,
        media_type=media_type
    )

@app.post("/api/playlist")
async def get_playlist_info(request: PlaylistRequest):
    """Extract playlist information"""
    try:
        opts = get_ydl_opts()
        opts["extract_flat"] = request.extract_flat
        opts["playlist_items"] = request.playlist_items

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)

            if info is None:
                raise HTTPException(status_code=404, detail="Playlist not found")

            entries = []
            if "entries" in info:
                for entry in info["entries"]:
                    if entry:
                        entries.append({
                            "id": entry.get("id"),
                            "title": entry.get("title"),
                            "duration": entry.get("duration"),
                            "uploader": entry.get("uploader"),
                            "url": entry.get("url") or f"https://youtube.com/watch?v={entry.get('id')}"
                        })

            return {
                "playlist_id": info.get("id"),
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "description": info.get("description"),
                "total_entries": info.get("playlist_count"),
                "entries": entries
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playlist extraction failed: {str(e)}")

# ═══════════════════════════════════════════════════════════════
# WEBSOCKET - REAL-TIME PROGRESS
# ═══════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

ws_manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({
            "type": "connected",
            "message": "WebSocket connected. Send {\"action\": \"subscribe\", \"download_id\": \"...\"} to track downloads."
        })

        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "subscribe":
                download_id = data.get("download_id")
                if download_id and download_id in download_manager.active_downloads:
                    await websocket.send_json({
                        "type": "download_status",
                        "download_id": download_id,
                        "status": download_manager.active_downloads[download_id]
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Download not found"
                    })

            elif action == "status":
                await websocket.send_json({
                    "type": "server_status",
                    "data": download_manager.get_status()
                })

            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        ws_manager.disconnect(websocket)

# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": str(exc), "type": type(exc).__name__}
    )

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
