import os
import sys
import json
import time
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List
import feedparser
import requests
from google import genai
from google.genai import types

# ==================== 1. 全局参数与中英双池订阅源配置 ====================
MAX_WECOM_BYTES = 1980          # 企业微信 text 消息 2048 字节红线
MAX_ITEMS_PER_FEED = 8          # 每个 RSS 源提取的最大候选条数
MAX_NEWS_AGE_HOURS = 15         # 严格锁死过去 15 小时内的新鲜资讯
HISTORY_FILE = "sent_history.json"
HISTORY_RETENTION_HOURS = 72    # 历史记录保留 72 小时

CANDIDATE_MODELS = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.0-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]


def build_google_news_rss(query: str, lang: str = "en-US", country: str = "US") -> str:
    """用括号包裹整个 OR 查询语句后再附加 when:1d，严防旧闻漏网"""
    strict_query = f"({query}) when:1d"
    encoded_query = urllib.parse.quote(strict_query)
    if lang == "zh-CN":
        return f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    if lang == "zh-HK":
        return f"https://news.google.com/rss/search?q={encoded_query}&hl=zh-HK&gl=HK&ceid=HK:zh-Hant"
    return f"https://news.google.com/rss/search?q={encoded_query}&hl={lang}&gl={country}&ceid={country}:en"


# 【核心升级】每个分类显式拆分为 "en"（英文媒体池）与 "zh"（中文媒体池），源头保证 1:1 比例
RSS_FEEDS = {
    "1_全球宏观与地缘政经": {
        "en": [
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            build_google_news_rss("Reuters OR Associated Press OR geopolitics OR central bank"),
        ],
        "zh": [
            build_google_news_rss("国际局势 OR 外交 OR 宏观政策 OR 央行 OR 关税", lang="zh-CN"),
            build_google_news_rss("国际政经 OR 地缘政治 OR 全球宏观", lang="zh-HK"),
        ],
    },
    "2_商业与金融市场": {
        "en": [
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
            "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
            build_google_news_rss("Wall Street OR Bloomberg OR Financial Times OR M&A OR earnings"),
        ],
        "zh": [
            build_google_news_rss("A股 OR 港股 OR 宏观经济 OR 财报 OR 并购 OR 汇率", lang="zh-CN"),
        ],
    },
    "3_科技与人工智能": {
        "en": [
            "https://techcrunch.com/feed/",
            "https://www.theverge.com/rss/index.xml",
            "https://www.wired.com/feed/rss",
            build_google_news_rss("OpenAI OR Google AI OR NVIDIA OR LLM OR robotics"),
        ],
        "zh": [
            "https://36kr.com/feed",
            "https://www.ithome.com/rss/",
        ],
    },
    "4_汽车工业与新能源": {
        "en": [
            "https://insideevs.com/rss/articles/all/",
            "https://electrek.co/feed/",
            build_google_news_rss("automotive OEM OR electric vehicle OR powertrain OR solid state battery"),
        ],
        "zh": [
            build_google_news_rss("新能源汽车 OR 动力电池 OR 电驱 OR 车企 OR 智能驾驶", lang="zh-CN"),
        ],
    },
    "5_X平台热点与全网讨论": {
        "en": [
            "https://hnrss.org/frontpage?points=150",
            build_google_news_rss("X Twitter viral OR trending OR Elon Musk OR tech debate"),
        ],
        "zh": [
            build_google_news_rss("热搜 OR 网友热议 OR 舆论焦点 OR 突发争议", lang="zh-CN"),
        ],
    },
    "6_港澳台地区动态": {
        "en": [
            "https://www.scmp.com/rss/2/feed",  # 南华早报 (SCMP) 英文版
            build_google_news_rss("Hong Kong OR Macau OR Taiwan economy OR TSMC"),
        ],
        "zh": [
            build_google_news_rss("香港 OR 澳门 OR 台湾 OR 大湾区", lang="zh-CN"),
            build_google_news_rss("香港經濟 OR 台灣產業 OR 港澳", lang="zh-HK"),
        ],
    },
    "7_全球体育赛事": {
        "en": [
            "http://feeds.bbci.co.uk/sport/rss.xml",
            "https://www.espn.com/espn/rss/news",
            build_google_news_rss("Champions League OR NBA OR UFC OR Premier League OR F1"),
        ],
        "zh": [
            build_google_news_rss("体育赛事 OR 欧冠 OR 英超 OR NBA OR UFC OR 国足 OR 夺冠", lang="zh-CN"),
        ],
    },
    "8_文娱与影视音乐": {
        "en": [
            "https://variety.com/feed/",
            "https://www.hollywoodreporter.com/feed/",
            build_google_news_rss("Hollywood OR box office OR Netflix OR anime OR gaming"),
        ],
        "zh": [
            build_google_news_rss("电影票房 OR 新片定档 OR 影视剧 OR 动漫 OR 演唱会", lang="zh-CN"),
        ],
    },
    "9_产业供应链与全球综合": {
        "en": [
            build_google_news_rss("global supply chain OR semiconductor manufacturing OR energy transition OR shipping"),
        ],
        "zh": [
            build_google_news_rss("供应链 OR 高端制造 OR 能源转型 OR 国际航运 OR 进出口", lang="zh-CN"),
        ],
    },
}


# ==================== 2. 历史去重记忆库管理（保留 72 小时） ====================
def make_fingerprint(text: str) -> str:
    clean = "".join(text.lower().split())
    return hashlib.md5(clean.encode("utf-8")).hexdigest()


def load_sent_history() -> tuple[set, List[str], dict]:
    now_ts = time.time()
    cutoff_ts = now_ts - (HISTORY_RETENTION_HOURS * 3600)

    raw_data = {"fingerprints": {}, "reported_events": []}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            print(f"[提示] 读取历史记录文件异常，将重新初始化: {e}")

    valid_fps = {
        fp: ts for fp, ts in raw_data.get("fingerprints", {}).items() if ts > cutoff_ts
    }
    valid_events = [
        ev for ev in raw_data.get("reported_events", []) if ev.get("ts", 0) > cutoff_ts
    ]

    recent_ai_titles = [ev["title"] for ev in valid_events[-45:]]
    cleaned_store = {"fingerprints": valid_fps, "reported_events": valid_events}

    print(f"📚 已加载历史去重库：{len(valid_fps)} 条原文指纹，{len(recent_ai_titles)} 条近期已推事件。")
    return set(valid_fps.keys()), recent_ai_titles, cleaned_store


def save_sent_history(
    history_store: dict,
    used_raw_titles: List[str],
    used_raw_urls: List[str],
    ai_generated_titles: List[str],
):
    now_ts = time.time()
    fps = history_store.get("fingerprints", {})
    events = history_store.get("reported_events", [])

    for t in used_raw_titles:
        if t:
            fps[make_fingerprint(t)] = now_ts
    for u in used_raw_urls:
        if u:
            fps[make_fingerprint(u)] = now_ts
    for ai_t in ai_generated_titles:
        if ai_t:
            events.append({"title": ai_t, "ts": now_ts})

    history_store["fingerprints"] = fps
    history_store["reported_events"] = events[-100:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_store, f, ensure_ascii=False, indent=2)
    print(f"💾 已更新去重记录库 ({HISTORY_FILE})，新增 {len(ai_generated_titles)} 条已推送事件。")


# ==================== 3. 中英分池抓取与严格时间戳过滤 ====================
def fetch_recent_news(seen_fingerprints: set) -> tuple[str, dict, dict, dict]:
    print(f"开始中英分池抓取全球新闻源（严格锁定过去 {MAX_NEWS_AGE_HOURS} 小时内）...")
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(hours=MAX_NEWS_AGE_HOURS)

    collected_sections = []
    current_run_titles = set()
    url_map = {}
    raw_title_map = {}
    lang_map = {}  # 记录每条新闻的原始语种 ("en" 或 "zh")

    en_counter = 1
    zh_counter = 1
    skipped_history_count = 0
    skipped_old_or_nodate_count = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }

    for category_key, lang_dict in RSS_FEEDS.items():
        en_items = []
        zh_items = []

        for lang_code, urls in lang_dict.items():
            for url in urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=12)
                    feed = feedparser.parse(resp.content)
                    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
                        title = entry.get("title", "").strip()
                        link = entry.get("link", "").strip()
                        summary = entry.get("summary", "").strip()

                        if not title or title in current_run_titles:
                            continue

                        # 1. 严格时间戳审查（无发布时间或超过 15 小时一律丢弃）
                        pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                        if not pub_parsed:
                            skipped_old_or_nodate_count += 1
                            continue

                        pub_dt = datetime.fromtimestamp(time.mktime(pub_parsed), tz=timezone.utc)
                        if pub_dt < cutoff_time or pub_dt > (now + timedelta(hours=2)):
                            skipped_old_or_nodate_count += 1
                            continue

                        # 2. 历史去重审查
                        title_fp = make_fingerprint(title)
                        link_fp = make_fingerprint(link) if link else ""
                        if title_fp in seen_fingerprints or (link_fp and link
