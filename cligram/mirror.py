import logging
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .instagram import MediaUnavailable, RateLimited, media_kind, video_url


LOG = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


class DownloadError(Exception):
    pass


class StopRequested(Exception):
    pass


def check_cdn_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (
            parts.scheme == "https" and parts.username is None and parts.port in (None, 443)
            and host.endswith((".cdninstagram.com", ".fbcdn.net"))
        ):
            return
    except ValueError:
        pass
    raise DownloadError("Instagram did not provide a video URL on its CDN.")


class CDNRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_cdn_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_video(url, output, stop):
    check_cdn_url(url)
    request = Request(url, headers={"Referer": "https://www.instagram.com/"})
    try:
        with build_opener(CDNRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise DownloadError("Instagram returned a partial video.")
            first = response.read(12)
            if len(first) != 12 or first[4:8] != b"ftyp":
                raise DownloadError("Instagram did not return an MP4 video.")
            size = output.write(first)
            while chunk := response.read(CHUNK_SIZE):
                if stop.is_set():
                    raise StopRequested
                size += output.write(chunk)
            if response.length not in (None, 0) or size <= len(first):
                raise DownloadError("Instagram returned an incomplete video.")
    except HTTPError as error:
        error.close()
        if error.code == 429:
            raise RateLimited("Instagram's video CDN is rate limiting requests.") from None
        raise DownloadError(f"Instagram's video CDN returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, HTTPException):
        raise DownloadError("The video transfer was interrupted.") from None


def file_identity(info):
    return str(info.st_dev), str(info.st_ino), info.st_size, info.st_mtime_ns


def is_owned(path, identity):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and file_identity(info) == identity


def publish_video(db, code, path, url, stop):
    # Staging on the same filesystem allows atomic publication without replacing an existing file.
    with NamedTemporaryFile(dir=path.parent, prefix=".cligram-", suffix=".part") as temporary:
        download_video(url, temporary, stop)
        temporary.flush()
        os.fsync(temporary.fileno())
        if stop.is_set():
            raise StopRequested
        identity = file_identity(os.fstat(temporary.fileno()))
        with db:
            db.execute("INSERT OR REPLACE INTO managed VALUES (?, ?, ?, ?, ?, 0)", (code, *identity))
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            with db:
                db.execute("DELETE FROM managed WHERE code = ?", (code,))
            raise DownloadError("A file with that name already exists in the download folder.") from None


@dataclass
class SyncResult:
    downloaded: int = 0
    deleted: int = 0
    pending_removal: int = 0
    skipped: int = 0
    complete: bool = True


def sync_collection(instagram, collection_id, folder: Path, database: Path, stop):
    snapshot = instagram.snapshot(collection_id)
    if stop.is_set():
        raise StopRequested
    if not folder.is_dir():
        raise OSError(f"Download folder {folder} is unavailable.")
    result = SyncResult(complete=snapshot.complete)
    if not snapshot.complete:
        LOG.warning("Instagram returned an incomplete collection. Removals are paused for this scan.")
    with closing(sqlite3.connect(database)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS managed (
            code TEXT PRIMARY KEY, device TEXT, inode TEXT, size INTEGER, modified INTEGER, misses INTEGER
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS non_reels (code TEXT PRIMARY KEY)")
        managed = {row[0]: row[1:] for row in db.execute("SELECT * FROM managed")}
        non_reels = {row[0] for row in db.execute("SELECT code FROM non_reels")}

        with db:
            for code in managed.keys() & snapshot.posts.keys():
                db.execute("UPDATE managed SET misses = 0 WHERE code = ?", (code,))

        for code, node in snapshot.posts.items():
            if stop.is_set():
                raise StopRequested
            path = folder / f"{code}.mp4"
            record = managed.get(code)
            if code in non_reels or (record and is_owned(path, record[:4])):
                continue
            try:
                kind = media_kind(node)
                url = video_url(node) if kind == "reel" else None
                if kind is None or (kind == "reel" and not url):
                    if stop.wait(0.5):
                        raise StopRequested
                    detail = instagram.media(code)
                    kind = kind or media_kind(detail)
                    url = video_url(detail) if kind == "reel" else None
                if kind == "other":
                    with db:
                        db.execute("INSERT OR IGNORE INTO non_reels VALUES (?)", (code,))
                    continue
                if not url:
                    raise DownloadError("Instagram did not provide a playable Reel.")
                if path.exists() or path.is_symlink():
                    raise DownloadError("A file with that name already exists in the download folder.")
                publish_video(db, code, path, url, stop)
                result.downloaded += 1
                LOG.info("Downloaded %s.mp4", code)
            except (DownloadError, MediaUnavailable) as error:
                LOG.warning("Skipped %s: %s", code, error)
                result.skipped += 1

        if snapshot.complete:
            for code in managed.keys() - snapshot.posts.keys():
                if stop.is_set():
                    raise StopRequested
                identity, misses = managed[code][:4], managed[code][4]
                path = folder / f"{code}.mp4"
                if not is_owned(path, identity):
                    with db:
                        db.execute("DELETE FROM managed WHERE code = ?", (code,))
                elif not misses:
                    with db:
                        db.execute("UPDATE managed SET misses = 1 WHERE code = ?", (code,))
                    result.pending_removal += 1
                else:
                    path.unlink(missing_ok=True)
                    with db:
                        db.execute("DELETE FROM managed WHERE code = ?", (code,))
                    result.deleted += 1
                    LOG.info("Removed %s.mp4", code)
    return result
