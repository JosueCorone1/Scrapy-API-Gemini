"""Download media files referenced by already-collected Instagram posts."""
import re
import time
from pathlib import Path
from urllib.parse import urlparse


_ALLOWED_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net", ".fbsbx.com")
_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".mp4", ".m4v", ".mov"}
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


class MediaDownloadError(Exception):
    """A media URL was invalid, unavailable, or refused by its CDN."""


def _valid_media_url(url):
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES
    )


def _extension(url, content_type):
    ext = _MIME_EXTENSIONS.get((content_type or "").split(";", 1)[0].strip().lower())
    if ext:
        return ext
    url_ext = Path(urlparse(url).path).suffix.lower()
    return url_ext if url_ext in _EXTENSIONS else ".bin"


class MediaDownloader:
    """One cookie-free CDN session. No retries or access-control bypasses."""

    def __init__(self):
        from curl_cffi import requests as curl_requests

        self._session = curl_requests.Session(impersonate="chrome")

    def close(self):
        self._session.close()

    def download(self, url, path_stem):
        if not _valid_media_url(url):
            raise MediaDownloadError("URL de medios no válida o dominio no permitido")

        try:
            response = self._session.get(
                url,
                headers={"accept": "image/avif,image/webp,image/*,video/mp4,*/*;q=0.8",
                         "referer": "https://www.instagram.com/"},
                timeout=45,
                stream=True,
            )
        except Exception as exc:
            raise MediaDownloadError(f"falló la conexión: {type(exc).__name__}") from None

        temp_path = Path(f"{path_stem}.part")
        try:
            if response.status_code != 200:
                raise MediaDownloadError(f"CDN respondió HTTP {response.status_code}")

            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if not (content_type.startswith("image/") or content_type.startswith("video/")):
                raise MediaDownloadError("el CDN no devolvió un archivo de imagen o video")

            temp_path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with temp_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        output.write(chunk)
                        size += len(chunk)
            if not size:
                raise MediaDownloadError("el archivo recibido está vacío")

            destination = Path(f"{path_stem}{_extension(url, content_type)}")
            temp_path.replace(destination)
            return destination
        except MediaDownloadError:
            temp_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise MediaDownloadError(f"no se pudo guardar el archivo: {exc}") from None
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise MediaDownloadError(f"falló la descarga: {type(exc).__name__}") from None
        finally:
            response.close()


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "media"))[:80]


def _assets(item):
    media = item.get("_media") or item
    carousel = media.get("carousel") or []
    if carousel:
        for index, slide in enumerate(carousel, 1):
            video = slide.get("video") or {}
            if video.get("link"):
                yield index, "video", video["link"]
            elif slide.get("image"):
                yield index, "image", slide["image"]
        return

    video = media.get("video") or {}
    if video.get("link"):
        yield None, "video", video["link"]
    elif media.get("image"):
        yield None, "image", media["image"]


def download_content(username, items, output_dir):
    """Download selected post/reel assets; attach status and relative paths."""
    root = Path(output_dir).resolve()
    results = []
    downloader = None
    stop_reason = None
    try:
        for item_index, item in enumerate(items, 1):
            kind = "reels" if item.get("source") == "reel" else "posts"
            item_id = _safe_name(item.get("id") or f"item_{item_index}")
            files = []
            errors = []
            assets = list(_assets(item))
            if not assets:
                item["media_downloads"] = {"status": "unavailable", "files": [], "error": "sin URL de medios"}
                results.append(item["media_downloads"])
                continue

            for slide_index, media_type, url in assets:
                if stop_reason:
                    errors.append(f"descarga detenida: {stop_reason}")
                    break
                if downloader is None:
                    downloader = MediaDownloader()
                slide_part = f"_slide_{slide_index:02d}" if slide_index else ""
                stem = root / "media" / _safe_name(username) / kind / f"{item_id}{slide_part}_{media_type}"
                try:
                    saved = downloader.download(url, stem)
                    files.append(saved.relative_to(root).as_posix())
                except MediaDownloadError as exc:
                    errors.append(str(exc))
                    if "HTTP 429" in str(exc):
                        stop_reason = "límite temporal del CDN (HTTP 429)"
                if not stop_reason:
                    time.sleep(0.25)

            status = "complete" if not errors else "partial" if files else "failed"
            item["media_downloads"] = {"status": status, "files": files}
            if errors:
                item["media_downloads"]["errors"] = errors
            results.append(item["media_downloads"])
    finally:
        if downloader is not None:
            downloader.close()

    return results
