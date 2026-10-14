"""Offline behavior checks for the standalone scraper."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bilibili_audio_scraper import AudioScraper, ScraperConfig, get_video_urls_from_collection
from bilibili_audio_scraper.cli import main


class CollectionTests(unittest.TestCase):
    def test_pagination_and_deduplication(self):
        class Response:
            def __init__(self, archives, total):
                self.archives = archives
                self.total = total

            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 0, "data": {
                    "archives": [{"bvid": bvid} for bvid in self.archives],
                    "page": {"total": self.total},
                }}

        class Session:
            def __init__(self):
                self.pages = []

            def mount(self, *args):
                pass

            def get(self, url, params, headers, timeout):
                self.pages.append(params["pn"])
                if params["pn"] == 1:
                    return Response([f"BV{i}" for i in range(30)], 31)
                return Response(["BV29", "BV30"], 31)

            def close(self):
                pass

        session = Session()
        with patch("bilibili_audio_scraper.collection.requests.Session", return_value=session):
            urls = get_video_urls_from_collection(
                "https://space.bilibili.com/45502/lists/31284?type=series"
            )
        self.assertEqual(len(urls), 31)
        self.assertEqual(session.pages, [1, 2])
        self.assertEqual(urls[-1], "https://www.bilibili.com/video/BV30")

    def test_invalid_collection_url(self):
        with self.assertRaises(ValueError):
            get_video_urls_from_collection("https://example.com/45502/lists/31284")


class DownloadTests(unittest.TestCase):
    def test_existing_audio_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BV123.m4a"
            path.write_bytes(b"audio")
            with patch("bilibili_audio_scraper.download.YoutubeDL") as downloader:
                sources = AudioScraper(ScraperConfig(output_dir=Path(directory))).download_audio(
                    "https://www.bilibili.com/video/BV123"
                )
            self.assertEqual(sources[0].path, path)
            downloader.assert_not_called()

    def test_download_returns_persistent_audio_path(self):
        with tempfile.TemporaryDirectory() as directory:
            class Downloader:
                def __init__(self, options):
                    self.options = options

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def extract_info(self, url, download):
                    (Path(directory) / "BV123.m4a").write_bytes(b"audio")
                    return {"id": "BV123", "title": "Example", "duration": 12}

            with patch("bilibili_audio_scraper.download.YoutubeDL", Downloader):
                sources = AudioScraper(ScraperConfig(output_dir=Path(directory))).download_audio(
                    "https://www.bilibili.com/video/BV123"
                )
            self.assertEqual(sources[0].title, "Example")
            self.assertTrue(sources[0].path.exists())


class CliTests(unittest.TestCase):
    def test_list_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "urls.txt"
            with patch("bilibili_audio_scraper.cli.get_video_urls_from_collection",
                       return_value=["https://www.bilibili.com/video/BV123"]), \
                 patch("bilibili_audio_scraper.cli.AudioScraper") as scraper:
                result = main(["list", "https://space.bilibili.com/1/lists/2",
                               "--output", str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "https://www.bilibili.com/video/BV123\n")
            scraper.assert_not_called()

    def test_download_continues_after_individual_failure(self):
        with patch("bilibili_audio_scraper.cli.AudioScraper") as scraper_type:
            scraper = scraper_type.return_value
            scraper.download_audio.side_effect = [RuntimeError("unavailable"), []]
            result = main(["download", "--video", "https://www.bilibili.com/video/BV1",
                           "--video", "https://www.bilibili.com/video/BV2"])
        self.assertEqual(result, 1)
        self.assertEqual(scraper.download_audio.call_count, 2)


if __name__ == "__main__":
    unittest.main()
