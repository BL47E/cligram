import sqlite3
import unittest
from contextlib import closing
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from cligram.instagram import InstagramError, Snapshot
from cligram.mirror import DownloadError, download_video, sync_collection


REEL = {"product_type": "clips", "video_versions": [{"url": "https://test.cdninstagram.com/reel.mp4"}]}


class MirrorTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.database = self.folder / "state.sqlite3"
        self.instagram = Mock()
        download = patch("cligram.mirror.download_video", side_effect=lambda _, file, stop: file.write(b"mp4 fixture"))
        self.download = download.start()
        self.addCleanup(download.stop)

    def sync(self, posts, *, complete=True):
        self.instagram.snapshot.return_value = Snapshot(posts, complete)
        return sync_collection(self.instagram, "123", self.folder, self.database, Event())

    def test_initial_sync_and_confirmed_unsave(self):
        self.assertEqual(self.sync({"First": REEL, "Image": {"media_type": 1}}).downloaded, 1)
        self.assertEqual(self.sync({"First": REEL, "Second": REEL}).downloaded, 1)
        self.assertEqual(self.download.call_count, 2)
        self.assertFalse((self.folder / "Image.mp4").exists())
        self.instagram.media.assert_not_called()

        self.assertEqual(self.sync({"Second": REEL}).pending_removal, 1)
        self.assertTrue((self.folder / "First.mp4").exists())
        self.assertEqual(self.sync({"Second": REEL}).deleted, 1)
        self.assertFalse((self.folder / "First.mp4").exists())
        self.assertEqual(self.sync({"First": REEL, "Second": REEL}).downloaded, 1)

    def test_incomplete_scan_cannot_remove_files_and_reappearance_resets_confirmation(self):
        self.sync({"Reel": REEL})
        self.assertEqual(self.sync({}).pending_removal, 1)
        self.assertEqual(self.sync({}, complete=False).deleted, 0)
        self.sync({"Reel": REEL}, complete=False)
        self.instagram.snapshot.side_effect = InstagramError("Page two failed")
        with self.assertRaises(InstagramError):
            self.sync({})
        self.instagram.snapshot.side_effect = None
        self.assertEqual(self.sync({}).pending_removal, 1)
        self.assertTrue((self.folder / "Reel.mp4").exists())

    def test_user_files_are_preserved(self):
        preexisting = self.folder / "AlreadyHere.mp4"
        preexisting.write_bytes(b"personal file")
        self.assertEqual(self.sync({"Reel": REEL, "AlreadyHere": REEL}).skipped, 1)
        replacement = self.folder / "Reel.mp4"
        replacement.write_bytes(b"personal replacement")
        self.sync({})
        self.assertEqual(self.sync({}).deleted, 0)
        self.assertEqual(preexisting.read_bytes(), b"personal file")
        self.assertEqual(replacement.read_bytes(), b"personal replacement")

    def test_failed_transfer_leaves_no_output_and_is_retried(self):
        def fail_after_partial_write(url, output, stop):
            output.write(b"part")
            raise DownloadError("Interrupted")

        with patch("cligram.mirror.download_video", side_effect=fail_after_partial_write):
            self.assertEqual(self.sync({"Reel": REEL}).skipped, 1)
        self.assertFalse((self.folder / "Reel.mp4").exists())
        self.assertFalse(list(self.folder.glob("*.part")))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM managed").fetchone()[0], 0)
        self.assertEqual(self.sync({"Reel": REEL}).downloaded, 1)


class DownloadTest(unittest.TestCase):
    def test_mp4_transfer_detects_truncated_content_length(self):
        payload = b"\x00\x00\x00\x14ftypisom" + b"fixture contents"
        output = BytesIO()
        url = "https://fixture.cdninstagram.com/media.mp4"
        for size in (len(payload), len(payload) + 1):
            with self.subTest(content_length=size):
                header = f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode()
                socket = Mock()
                socket.makefile.return_value = BytesIO(header + payload)
                response = HTTPResponse(socket)
                response.begin()
                with patch("cligram.mirror.build_opener") as opener:
                    opener.return_value.open.return_value = response
                    if size == len(payload):
                        download_video(url, output, Event())
                        self.assertEqual(output.getvalue(), payload)
                    else:
                        with self.assertRaises(DownloadError):
                            download_video(url, BytesIO(), Event())


if __name__ == "__main__":
    unittest.main()
