import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional

# Load .env robustly (try project root and CWD)
_ROOT = Path(__file__).resolve().parent  # /var/www/storage-api -> parent is project root
_candidates = [
    _ROOT / ".env",
    Path.cwd() / ".env",
]
for p in _candidates:
    try:
        if p.exists():
            load_dotenv(p, override=False)
    except Exception:
        pass

_DATA_DIR_ENV = os.getenv("STORAGE_DATA_DIR")
if _DATA_DIR_ENV:
    _DATA_DIR = Path(_DATA_DIR_ENV).expanduser()
else:
    _DATA_DIR = Path("/var/lib/storage-api")

_DATABASE_URL_ENV = os.getenv("STORAGE_DATABASE_URL") or os.getenv("DATABASE_URL")
if not _DATABASE_URL_ENV:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    default_sqlite_path = _DATA_DIR / "storage.db"
    _DATABASE_URL_ENV = f"sqlite:///{default_sqlite_path}"

_CHROMA_DB_PATH_ENV = os.getenv("CHROMA_DB_PATH")
if not _CHROMA_DB_PATH_ENV:
    default_chroma_dir = _DATA_DIR / "chroma_db"
    try:
        default_chroma_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _CHROMA_DB_PATH_ENV = str(default_chroma_dir)

_AI_ANALYSIS_QUEUE_PATH_ENV = os.getenv("AI_ANALYSIS_QUEUE_PATH")
if not _AI_ANALYSIS_QUEUE_PATH_ENV:
    default_queue_dir = _DATA_DIR / "logs"
    try:
        default_queue_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _AI_ANALYSIS_QUEUE_PATH_ENV = str(default_queue_dir / "ai_analysis_queue.txt")

class Settings:
    # Database
    DATABASE_URL: str = _DATABASE_URL_ENV

    # File Storage
    STORAGE_UPLOAD_DIR: str = os.getenv("STORAGE_UPLOAD_DIR", "./uploads/storage")
    BASE_URL: str = os.getenv("STORAGE_BASE_URL", os.getenv("BASE_URL", "https://api-storage.arkturian.com"))

    # VOD Base URL - automatically derived from BASE_URL
    # Replaces "api-storage" with "vod" in the BASE_URL
    @property
    def VOD_BASE_URL(self) -> str:
        vod_url = os.getenv("VOD_BASE_URL")
        if vod_url:
            return vod_url
        # Auto-derive from BASE_URL: api-storage.X.com -> vod.X.com
        return self.BASE_URL.replace("api-storage", "vod")

    # ChromaDB / Knowledge Graph
    CHROMA_DB_PATH: str = _CHROMA_DB_PATH_ENV

    DATA_DIR: str = str(_DATA_DIR)

    # API Keys
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    API_KEY: str = os.getenv("API_KEY", "Inetpass1")

    # External APIs
    ONEAL_API_BASE: str = os.getenv("ONEAL_API_BASE", "https://oneal-api.arkturian.com")
    ONEAL_API_KEY: Optional[str] = os.getenv("ONEAL_API_KEY", "")
    
    # User Settings
    NEW_USER_TRUST_LEVEL: str = os.getenv("NEW_USER_TRUST_LEVEL", "user")

    # Media Settings
    MAX_FILE_SIZE: int = int(os.getenv("STORAGE_MAX_FILE_SIZE", os.getenv("MAX_FILE_SIZE", "524288000")))  # 500MB default
    ALLOWED_IMAGE_TYPES: list = ["image/jpeg", "image/png", "image/webp"]
    ALLOWED_VIDEO_TYPES: list = ["video/mp4", "video/quicktime", "video/x-msvideo", "video/webm"]
    ALLOWED_AUDIO_TYPES: list = ["audio/mpeg", "audio/wav", "audio/aac", "audio/mp4", "audio/webm"]

    # Analysis Settings
    ANALYSIS_TIMEOUT: int = int(os.getenv("STORAGE_ANALYSIS_TIMEOUT", "60"))  # seconds
    AI_ANALYSIS_QUEUE_PATH: str = _AI_ANALYSIS_QUEUE_PATH_ENV

    # Transcoding Settings
    TRANSCODING_MODE: str = os.getenv("TRANSCODING_MODE", "local")  # local, remote, disabled
    TRANSCODING_API_URL: Optional[str] = os.getenv("TRANSCODING_API_URL")
    TRANSCODING_API_KEY: Optional[str] = os.getenv("TRANSCODING_API_KEY")
    TRANSCODING_CALLBACK_URL: Optional[str] = os.getenv("TRANSCODING_CALLBACK_URL")  # Callback URL for transcoding status updates
    TRANSCODING_RESPONSE_MODE: str = os.getenv("TRANSCODING_RESPONSE_MODE", "zip")  # "path" (local) or "zip" (remote)

    # CORS Settings
    CORS_ORIGINS: list = os.getenv(
        "CORS_ORIGINS",
        "https://admin.arkturian.com,https://admin.arkserver.arkturian.com,http://localhost:3000,http://localhost:8080"
    ).split(",")

settings = Settings()
