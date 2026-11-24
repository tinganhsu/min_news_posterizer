import importlib
import sys
import types
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

from min_news_posterizer.taiwan_news import (
    DEFAULT_TAIWAN_FEED_ID,
    NewsItem,
    fetch_taiwan_news_items,
    format_news_digest,
    get_taiwan_feed_options,
    parse_feed_xml,
)


SAMPLE_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title><![CDATA[\xe5\x8f\xb0\xe7\xa9\x8d\xe9\x9b\xbb\xe6\x96\xb0\xe5\xbb\xa0\xe5\x8b\x95\xe5\xb7\xa5 - \xe4\xb8\xad\xe5\xa4\xae\xe7\xa4\xbe CNA]]></title>
      <description><![CDATA[<p>\xe5\x8f\xb0\xe7\xa9\x8d\xe9\x9b\xbb\xe5\xae\xa3\xe5\xb8\x83\xe5\x9c\xa8\xe5\x8f\xb0\xe5\x8d\x97\xe6\x93\xb4\xe5\xbb\xba\xe5\x85\x88\xe9\x80\xb2\xe8\xa3\xbd\xe7\xa8\x8b\xe6\x96\xb0\xe5\xbb\xa0 &amp; \xe5\xa2\x9e\xe5\x8a\xa0\xe6\x8a\x95\xe8\xb3\x87\xe3\x80\x82</p>]]></description>
      <pubDate>Tue, 24 Nov 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>\xe5\x9c\x8b\xe9\x9a\x9b\xe6\xb2\xb9\xe5\x83\xb9\xe9\x87\x8d\xe6\x8c\xab</title>
      <description>\xe5\x9c\x8b\xe9\x9a\x9b\xe6\xb2\xb9\xe5\x83\xb9\xe9\x87\x8d\xe6\x8c\xab</description>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>PTS News</title>
  <entry>
    <title>\xe5\x8f\xb0\xe7\xa9\x8d\xe9\x9b\xbb\xe6\x96\xb0\xe5\xbb\xa0\xe5\x8b\x95\xe5\xb7\xa5</title>
    <summary>\xe5\x85\xac\xe8\xa6\x96\xe5\xa0\xb1\xe5\xb0\x8e\xe5\x85\x88\xe9\x80\xb2\xe8\xa3\xbd\xe7\xa8\x8b\xe6\x96\xb0\xe5\xbb\xa0\xe9\x80\xb2\xe5\xba\xa6\xe3\x80\x82</summary>
    <updated>2026-11-24T08:30:00+08:00</updated>
  </entry>
  <entry>
    <title>\xe6\xb0\xa3\xe5\x80\x99\xe5\xb3\xb0\xe6\x9c\x83\xe9\x81\x94\xe6\x88\x90\xe6\xb8\x9b\xe7\xa2\xb3\xe6\x96\xb0\xe5\x85\xb1\xe8\xad\x98</title>
    <summary>\xe5\x90\x84\xe5\x9c\x8b\xe4\xbb\xa3\xe8\xa1\xa8\xe6\x89\xbf\xe8\xab\xbe\xe5\x8a\xa0\xe9\x80\x9f\xe8\x83\xbd\xe6\xba\x90\xe8\xbd\x89\xe5\x9e\x8b\xe3\x80\x82</summary>
    <updated>2026-11-24T07:00:00+08:00</updated>
  </entry>
</feed>
"""


def _load_plugin_module_with_stubs():
    """Import min_news_posterizer.min_news_posterizer with lightweight InkyPi stubs."""
    stub_modules = {}

    blueprints_pkg = types.ModuleType("blueprints")
    plugin_mod = types.ModuleType("blueprints.plugin")

    class _DummyBlueprint:
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

    plugin_mod.plugin_bp = _DummyBlueprint()
    stub_modules["blueprints"] = blueprints_pkg
    stub_modules["blueprints.plugin"] = plugin_mod

    plugins_pkg = types.ModuleType("plugins")
    base_pkg = types.ModuleType("plugins.base_plugin")
    base_mod = types.ModuleType("plugins.base_plugin.base_plugin")

    class _DummyBasePlugin:
        def __init__(self, config):
            self.config = config

        def generate_settings_template(self):
            return {}

    base_mod.BasePlugin = _DummyBasePlugin
    stub_modules["plugins"] = plugins_pkg
    stub_modules["plugins.base_plugin"] = base_pkg
    stub_modules["plugins.base_plugin.base_plugin"] = base_mod

    utils_pkg = types.ModuleType("utils")
    img_utils_mod = types.ModuleType("utils.image_utils")
    img_utils_mod.pad_image_blur = lambda img, dims: img.resize(dims)
    stub_modules["utils"] = utils_pkg
    stub_modules["utils.image_utils"] = img_utils_mod

    flask_mod = types.ModuleType("flask")
    flask_mod.abort = lambda code: (_ for _ in ()).throw(RuntimeError(f"abort {code}"))
    flask_mod.jsonify = lambda obj: obj
    flask_mod.request = MagicMock()
    stub_modules["flask"] = flask_mod

    dotenv_mod = types.ModuleType("dotenv")
    dotenv_mod.find_dotenv = lambda **_kwargs: ""
    dotenv_mod.load_dotenv = lambda **_kwargs: False
    stub_modules["dotenv"] = dotenv_mod

    if "requests" not in sys.modules:
        try:
            import requests as _real_requests  # noqa: F401
        except ImportError:
            req_mod = types.ModuleType("requests")

            class _HTTPError(Exception):
                pass

            class _Response:
                pass

            req_mod.HTTPError = _HTTPError
            req_mod.Response = _Response
            req_mod.get = MagicMock()
            req_mod.post = MagicMock()
            req_mod.head = MagicMock()
            stub_modules["requests"] = req_mod

    for name, mod in stub_modules.items():
        sys.modules.setdefault(name, mod)

    return importlib.import_module("min_news_posterizer.min_news_posterizer")


class TaiwanNewsTests(unittest.TestCase):
    def test_parse_rss_xml_cleans_html_and_redundant_summary(self):
        items = parse_feed_xml(SAMPLE_RSS_XML, source_name="中央社 CNA", limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].source, "中央社 CNA")
        self.assertIn("台積電新廠動工", items[0].title)
        self.assertEqual(items[0].summary, "台積電宣布在台南擴建先進製程新廠 & 增加投資。")
        self.assertEqual(items[1].title, "國際油價重挫")
        self.assertEqual(items[1].summary, "")

    def test_parse_atom_xml(self):
        items = parse_feed_xml(SAMPLE_ATOM_XML, source_name="公視新聞", limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "台積電新廠動工")
        self.assertEqual(items[1].title, "氣候峰會達成減碳新共識")

    def test_fetch_deduplicates_and_tolerates_partial_failures(self):
        session = MagicMock()

        ok_atom = MagicMock()
        ok_atom.content = SAMPLE_ATOM_XML
        ok_atom.raise_for_status.return_value = None

        ok_rss = MagicMock()
        ok_rss.content = SAMPLE_RSS_XML
        ok_rss.raise_for_status.return_value = None

        session.get.side_effect = [
            ok_atom,
            RuntimeError("network timeout"),
            ok_rss,
            RuntimeError("404"),
            RuntimeError("503"),
        ]

        items, focus = fetch_taiwan_news_items(DEFAULT_TAIWAN_FEED_ID, session=session)
        titles = [item.title for item in items]
        # "台積電新廠動工" appears in both Atom and RSS (with publisher suffix) and should be deduplicated.
        self.assertEqual(len(items), 3)
        self.assertIn("台積電新廠動工", titles)
        self.assertIn("氣候峰會達成減碳新共識", titles)
        self.assertIn("國際油價重挫", titles)
        self.assertTrue(focus)

        digest = format_news_digest(items)
        self.assertIn("1. [公視新聞] 台積電新廠動工", digest)

    def test_custom_rss_requires_valid_url(self):
        with self.assertRaises(RuntimeError):
            fetch_taiwan_news_items("custom_rss", custom_rss_url="not-a-url")

    def test_feed_options_include_default_and_custom(self):
        ids = [opt["id"] for opt in get_taiwan_feed_options()]
        self.assertIn(DEFAULT_TAIWAN_FEED_ID, ids)
        self.assertIn("custom_rss", ids)


class PluginIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_plugin_module_with_stubs()

    def test_resolve_source_type_backward_compatibility(self):
        self.assertEqual(self.mod._resolve_source_type({}), "taiwan_rss")
        self.assertEqual(self.mod._resolve_source_type({"newspaperSlug": "ny_nyt"}), "newspaper")
        self.assertEqual(
            self.mod._resolve_source_type({"sourceType": "taiwan_rss", "newspaperSlug": "ny_nyt"}),
            "taiwan_rss",
        )
        self.assertEqual(self.mod._resolve_source_type({"sourceType": "newspaper"}), "newspaper")

    def test_generate_image_taiwan_rss_end_to_end(self):
        plugin = self.mod.NewspaperPoster(config={})

        device_config = MagicMock()
        device_config.load_env_key.return_value = "test-api-key"
        device_config.get_config.return_value = "horizontal"
        device_config.get_resolution.return_value = (800, 480)

        fake_png = BytesIO()
        Image.new("RGB", (1024, 640), color=(20, 40, 80)).save(fake_png, format="PNG")
        fake_png_bytes = fake_png.getvalue()

        analysis_resp = MagicMock()
        analysis_resp.raise_for_status.return_value = None
        analysis_resp.json.return_value = {
            "aiRecord": {
                "aiRecordDetail": {
                    "resultObject": [
                        "HEADLINE: 台積電台南新廠動工\n"
                        "SUBHEAD: 擴大先進製程投資帶動供應鏈\n"
                        "SCENE: A stylized semiconductor cleanroom and modern factory skyline in Tainan with glowing silicon wafers."
                    ]
                }
            }
        }

        image_resp = MagicMock()
        image_resp.raise_for_status.return_value = None
        image_resp.json.return_value = {
            "aiRecord": {"temporaryUrl": "https://asset.1min.ai/mock-poster.png"}
        }

        download_resp = MagicMock()
        download_resp.raise_for_status.return_value = None
        download_resp.content = fake_png_bytes

        sample_items = [
            NewsItem(source="公視新聞", title="台積電新廠動工", summary="擴大先進製程投資"),
        ]

        with (
            patch.object(self.mod, "fetch_taiwan_news_items", return_value=(sample_items, "Taiwan news")),
            patch.object(self.mod.requests, "post", side_effect=[analysis_resp, image_resp]) as mock_post,
            patch.object(self.mod.requests, "get", return_value=download_resp),
        ):
            img = plugin.generate_image(
                {
                    "sourceType": "taiwan_rss",
                    "twNewsFeed": "tw_global_digest",
                    "vibe_id": "risograph",
                },
                device_config,
            )

        self.assertEqual(img.size, (800, 480))
        self.assertEqual(mock_post.call_count, 2)
        # First POST is text-only news analysis (no image attachments).
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        self.assertNotIn("attachments", first_payload["promptObject"])
        self.assertIn("台積電新廠動工", first_payload["promptObject"]["prompt"])
        # Second POST is image generation with Traditional Chinese prompt rules.
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        self.assertIn("台積電台南新廠動工", second_payload["promptObject"]["prompt"])
        self.assertIn("Traditional Chinese (Taiwan, 繁體中文)", second_payload["promptObject"]["prompt"])


if __name__ == "__main__":
    unittest.main()
