from django.test import Client, SimpleTestCase

from .instagram import parse_instagram_url, shortcode_to_pk


class InstagramUrlTests(SimpleTestCase):
    def test_reel_url(self):
        url, info = parse_instagram_url("https://www.instagram.com/reel/DUPDSt3EUZ1/")
        self.assertEqual(url, "https://www.instagram.com/reel/DUPDSt3EUZ1/")
        self.assertEqual(info["type"], "post")
        self.assertEqual(info["kind"], "reel")
        self.assertEqual(info["shortcode"], "DUPDSt3EUZ1")

    def test_highlight_url(self):
        _, info = parse_instagram_url(
            "https://instagram.com/stories/highlights/18090946048123978/"
        )
        self.assertEqual(info["type"], "story")
        self.assertEqual(info["username"], "highlights")
        self.assertEqual(info["story_id"], "18090946048123978")

    def test_invalid_host(self):
        with self.assertRaises(ValueError):
            parse_instagram_url("https://example.com/reel/DUPDSt3EUZ1/")

    def test_shortcode_to_pk(self):
        self.assertEqual(shortcode_to_pk("DUPDSt3EUZ1"), "3823289088993216117")


class ViewTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Instagram Story Downloader")

    def test_invalid_download_body(self):
        response = self.client.post(
            "/api/download",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_invalid_download_url(self):
        response = self.client.post(
            "/api/download",
            data='{"url": "https://example.com/reel/DUPDSt3EUZ1/"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
