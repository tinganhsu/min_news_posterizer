import html
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; 1min-news-posterizer/1.0)"
DEFAULT_TIMEOUT = (5, 15)

# Verified Taiwan & international Traditional Chinese RSS/Atom feeds.
TAIWAN_NEWS_FEEDS = {
    "tw_global_digest": {
        "label": "台灣 / 國際每日焦點綜合（公視・中央社・BBC・Google 新聞）",
        "focus": "Taiwan and major international news",
        "sources": [
            ("公視新聞", "https://news.pts.org.tw/xml/newsfeed.xml", 8),
            ("中央社 CNA", "https://news.google.com/rss/search?q=site:cna.com.tw+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 8),
            ("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml", 6),
            ("Google 台灣焦點", "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 6),
            ("Google 國際焦點", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 6),
        ],
    },
    "tw_local_focus": {
        "label": "台灣焦點新聞（公視・中央社・Google 台灣・自由時報）",
        "focus": "major Taiwan domestic, political, economic, and societal news",
        "sources": [
            ("公視新聞", "https://news.pts.org.tw/xml/newsfeed.xml", 8),
            ("中央社 CNA", "https://news.google.com/rss/search?q=site:cna.com.tw+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 8),
            ("Google 台灣焦點", "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 6),
            ("自由時報政治", "https://news.ltn.com.tw/rss/politics.xml", 5),
            ("自由時報財經", "https://news.ltn.com.tw/rss/business.xml", 5),
        ],
    },
    "tw_world_focus": {
        "label": "國際重要消息（BBC 中文・Google 國際・自由時報國際・公視）",
        "focus": "major international and global news relevant to Taiwan readers",
        "sources": [
            ("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml", 8),
            ("Google 國際焦點", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 8),
            ("自由時報國際", "https://news.ltn.com.tw/rss/world.xml", 8),
            ("公視新聞", "https://news.pts.org.tw/xml/newsfeed.xml", 6),
        ],
    },
    "pts_news": {
        "label": "公視新聞網 (PTS)",
        "focus": "Taiwan and international news from PTS",
        "sources": [
            ("公視新聞", "https://news.pts.org.tw/xml/newsfeed.xml", 15),
        ],
    },
    "cna_news": {
        "label": "中央社 (CNA)",
        "focus": "Taiwan and international news from Central News Agency",
        "sources": [
            ("中央社 CNA", "https://news.google.com/rss/search?q=site:cna.com.tw+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", 18),
        ],
    },
    "bbc_chinese": {
        "label": "BBC 中文（繁體）",
        "focus": "international and Taiwan-related news from BBC Chinese",
        "sources": [
            ("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml", 15),
        ],
    },
    "ltn_news": {
        "label": "自由時報（政治・國際・財經）",
        "focus": "Taiwan politics, economy, and international news from Liberty Times",
        "sources": [
            ("自由時報國際", "https://news.ltn.com.tw/rss/world.xml", 7),
            ("自由時報政治", "https://news.ltn.com.tw/rss/politics.xml", 7),
            ("自由時報財經", "https://news.ltn.com.tw/rss/business.xml", 6),
        ],
    },
    "custom_rss": {
        "label": "自訂 RSS / Atom 網址",
        "focus": "the most important news story from the provided feed",
        "sources": [],
    },
}

DEFAULT_TAIWAN_FEED_ID = "tw_global_digest"


@dataclass
class NewsItem:
    source: str
    title: str
    summary: str = ""
    published: str = ""


def get_taiwan_feed_options() -> List[dict]:
    """Return feed options formatted for settings.html."""
    return [
        {"id": feed_id, "label": meta["label"]}
        for feed_id, meta in TAIWAN_NEWS_FEEDS.items()
    ]


def _clean_text(raw: Optional[str], max_len: int = 160) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _normalize_title_key(title: str) -> str:
    # Strip trailing publisher suffix like " - 中央社 CNA" for deduplication.
    base = re.sub(r"\s*[-|｜]\s*[^-|｜]{1,20}$", "", title or "")
    return re.sub(r"[\W_]+", "", base).casefold()


def parse_feed_xml(xml_bytes: bytes, source_name: str, limit: int = 10) -> List[NewsItem]:
    """Parse RSS 2.0 or Atom XML bytes into a list of NewsItem."""
    if not xml_bytes:
        return []

    root = ET.fromstring(xml_bytes)
    items: List[NewsItem] = []

    rss_items = root.findall(".//item")
    if rss_items:
        for elem in rss_items:
            title = _clean_text(elem.findtext("title"), max_len=120)
            if not title:
                continue
            summary = _clean_text(elem.findtext("description"), max_len=140)
            # Google News descriptions mostly repeat the title & related links; drop redundant ones.
            if summary and title[:15] in summary and len(summary) < len(title) + 25:
                summary = ""
            published = _clean_text(elem.findtext("pubDate"), max_len=40)
            items.append(NewsItem(source=source_name, title=title, summary=summary, published=published))
            if len(items) >= limit:
                break
        return items

    atom_entries = root.findall(f".//{ATOM_NS}entry") or root.findall(".//entry")
    for elem in atom_entries:
        title = _clean_text(
            elem.findtext(f"{ATOM_NS}title") or elem.findtext("title"),
            max_len=120,
        )
        if not title:
            continue
        summary = _clean_text(
            elem.findtext(f"{ATOM_NS}summary")
            or elem.findtext(f"{ATOM_NS}content")
            or elem.findtext("summary")
            or elem.findtext("content"),
            max_len=140,
        )
        published = _clean_text(
            elem.findtext(f"{ATOM_NS}published")
            or elem.findtext(f"{ATOM_NS}updated")
            or elem.findtext("published")
            or elem.findtext("updated"),
            max_len=40,
        )
        items.append(NewsItem(source=source_name, title=title, summary=summary, published=published))
        if len(items) >= limit:
            break

    return items


def _parse_custom_urls(custom_rss_url: str) -> List[Tuple[str, str, int]]:
    urls = [
        u.strip()
        for u in re.split(r"[\n,]+", custom_rss_url or "")
        if u.strip().startswith(("http://", "https://"))
    ]
    if not urls:
        return []
    per_source = max(6, min(15, 24 // len(urls)))
    return [(f"自訂來源 {idx + 1}" if len(urls) > 1 else "自訂來源", url, per_source) for idx, url in enumerate(urls)]


def _fetch_url_bytes(url: str, session: Optional[Any] = None) -> bytes:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    http = session or requests
    if http is not None:
        resp = http.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.content

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT[1]) as resp:
        return resp.read()


def fetch_taiwan_news_items(
    feed_id: str = DEFAULT_TAIWAN_FEED_ID,
    custom_rss_url: str = "",
    max_total_items: int = 28,
    session: Optional[Any] = None,
) -> Tuple[List[NewsItem], str]:
    """
    Fetch and deduplicate latest news items for the selected Taiwan/international feed.
    Returns (items, focus_description).
    """
    feed_id = (feed_id or DEFAULT_TAIWAN_FEED_ID).strip()
    if feed_id not in TAIWAN_NEWS_FEEDS:
        logger.warning("Unknown Taiwan news feed '%s', falling back to %s", feed_id, DEFAULT_TAIWAN_FEED_ID)
        feed_id = DEFAULT_TAIWAN_FEED_ID

    feed_meta = TAIWAN_NEWS_FEEDS[feed_id]
    if feed_id == "custom_rss":
        sources = _parse_custom_urls(custom_rss_url)
        if not sources:
            raise RuntimeError("Custom RSS feed selected, but no valid http(s) RSS URL was provided.")
    else:
        sources = feed_meta["sources"]

    collected: List[NewsItem] = []
    seen_keys = set()

    for source_name, url, limit in sources:
        try:
            content = _fetch_url_bytes(url, session=session)
            parsed = parse_feed_xml(content, source_name=source_name, limit=limit)
            for item in parsed:
                key = _normalize_title_key(item.title)
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                collected.append(item)
                if len(collected) >= max_total_items:
                    break
        except Exception as e:
            logger.warning("Failed to fetch RSS feed '%s' (%s): %s", source_name, url, e)

        if len(collected) >= max_total_items:
            break

    return collected, feed_meta["focus"]


def format_news_digest(items: Iterable[NewsItem]) -> str:
    """Format news items into a concise numbered list for the analysis model."""
    lines = []
    for idx, item in enumerate(items, start=1):
        line = f"{idx}. [{item.source}] {item.title}"
        if item.summary:
            line += f" — {item.summary}"
        lines.append(line)
    return "\n".join(lines)
