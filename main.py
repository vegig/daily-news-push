import os
import re
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

# ==================== 1. 全局参数与中英全球热门订阅源配置 ====================
MAX_WECOM_BYTES = 1980          # 企业微信 text 消息 2048 字节安全红线
MAX_ITEMS_PER_FEED = 8          # 每个 RSS 源提取的最大候选条数
MAX_NEWS_AGE_HOURS = 18         # 锁死过去 18 小时内的新鲜资讯
HISTORY_FILE = "sent_history.json"
HISTORY_RETENTION_HOURS = 72    # 历史记录保留 72 小时，防止跨期重复

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
            "https://www.scmp.com/rss/2/feed",
            build_google_news_rss("Hong Kong OR Macau OR Taiwan economy OR TSMC"),
        ],
        "zh": [
            build_google_news_rss("香港 OR 澳门 OR 台湾 OR 大湾区", lang="zh-CN"),
            build_google_news_rss("香港經濟 OR 台灣產業 OR 港澳", lang="zh-HK"),
        ],
    },
    "7_全球体育
