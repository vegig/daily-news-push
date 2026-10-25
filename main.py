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
                        if title_fp in seen_fingerprints or (link_fp and link_fp in seen_fingerprints):
                            skipped_history_count += 1
                            continue

                        hours_ago = max(0.1, round((now - pub_dt).total_seconds() / 3600, 1))
                        current_run_titles.add(title)

                        # 【核心改造】打上 EN_ 或 ZH_ 专属语种编号
                        if lang_code == "en":
                            news_id = f"EN_{en_counter}"
                            en_counter += 1
                        else:
                            news_id = f"ZH_{zh_counter}"
                            zh_counter += 1

                        url_map[news_id] = link
                        raw_title_map[news_id] = title
                        lang_map[news_id] = lang_code

                        clean_summary = summary[:140].replace("\n", " ").replace("\r", " ")
                        line_str = f"[{news_id}] ({hours_ago}h前) 标题: {title} | 摘要: {clean_summary}"

                        if lang_code == "en":
                            en_items.append(line_str)
                        else:
                            zh_items.append(line_str)
                except Exception as e:
                    print(f"[警告] 抓取源跳过 ({url[:45]}...): {e}")

        # 控制每个分类喂给 AI 的中英候选数量均衡（各最多取 12 条）
        en_slice = en_items[:12]
        zh_slice = zh_items[:12]

        if en_slice or zh_slice:
            section_text = f"=== 分类候选池：{category_key} ===\n"
            if en_slice:
                section_text += "【🌐 英文外媒候选 (编号 EN_开头)】:\n" + "\n".join(en_slice) + "\n"
            if zh_slice:
                section_text += "【🇨🇳 中文媒体候选 (编号 ZH_开头)】:\n" + "\n".join(zh_slice)
            collected_sections.append(section_text.strip())
        else:
            print(f"ℹ️ 分类 [{category_key}] 本时段无满足 {MAX_NEWS_AGE_HOURS} 小时内的新鲜资讯。")

    print(
        f"✅ 双池抓取完成：英文外媒 {en_counter - 1} 条 + 中文媒体 {zh_counter - 1} 条 | "
        f"拦截超期/无日期旧闻: {skipped_old_or_nodate_count} 条 | "
        f"拦截往期重复: {skipped_history_count} 条。"
    )
    return "\n\n".join(collected_sections), url_map, raw_title_map, lang_map


# ==================== 4. 调用 Gemini 生成情报（强制 1:1 中英配额 + 中文精译） ====================
def discover_available_flash_models(client: genai.Client) -> List[str]:
    discovered = []
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            if "flash" in name and not any(
                skip in name for skip in ["2.0", "2.5", "tts", "image", "audio", "live", "thinking"]
            ):
                discovered.append(name)
    except Exception:
        pass
    return discovered


def get_edition_label() -> tuple[str, str]:
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(beijing_tz)
    date_str = now_bj.strftime("%m月%d日")
    if now_bj.hour < 14:
        return date_str, "🌅 早报"
    else:
        return date_str, "🌙 晚报"


def analyze_news_with_ai(raw_news_text: str, recent_ai_titles: List[str]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("❌ [错误] 未检测到 GEMINI_API_KEY 环境变量！")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    beijing_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(beijing_tz).strftime("%Y年%m月%d日 %H:%M")

    dedup_instruction = ""
    if recent_ai_titles:
        joined_titles = "\n".join(f"- {t}" for t in recent_ai_titles)
        dedup_instruction = f"""
【🚨 跨期严禁重复指令】
以下是近期已经向用户推送过的事件清单。即使候选池中有其他媒体换角度再次报道以下同一事件，也**必须坚决剔除**：
{joined_titles}
"""

    system_prompt = f"""你是一位精通中英双语的全球商业、科技与舆情首席情报官。当前北京时间是 {today_str}。
请从用户提供的“中英双池”候选新闻中，跨源去重、剔除噪音，严格输出 JSON 格式的今日全球情报。
{dedup_instruction}
【🚨 核心铁律：50% 英文外媒 + 50% 中文媒体配额 & 专业中文翻译】
1. **中英对半硬性配额（1:1 比例）**：
   - 候选池中的新闻已明确分为 `EN_数字`（英文外媒）和 `ZH_数字`（中文媒体）。
   - 在每个分类挑选 2~4 条新闻时，**必须严格执行一半来自 `EN_` 英文源、一半来自 `ZH_` 中文源**！（例如：某分类选 2 条，则必须 1 条 `EN_` + 1 条 `ZH_`；选 3~4 条，则至少包含 1~2 条 `EN_` 和 1~2 条 `ZH_`。只有当某分类下某一语种候选为空时才允许例外）。
   - 全篇报告的总入选新闻中，`EN_` 开头的新闻占比必须达到 **50% 左右**，严禁偷懒只挑中文新闻！
2. **强制信达雅中文翻译**：
   - 所有选中的 `EN_` 英文外媒新闻，其 `title`（标题）、`fact`（核心事实）、`insight`（洞察）**必须全部翻译为地道、流畅、专业的简体中文**！严禁直接输出英文长句（可保留公认行业缩写如 Fed、OEM、800V、LLM、M&A、UFC、NBA 等）。
3. **宁缺毋滥与防幻觉**：
   - 严禁脱离候选池编造新闻！`news_id` 必须原样填写如 `"EN_12"` 或 `"ZH_5"`。
   - 若某分类下没有任何有价值的新鲜动态，直接将该分类的 `items` 设为空数组 `[]`。

【必须严格输出的 JSON 格式示例】
{{
  "top_3_highlights": [
    "本期最重磅要闻1（26字以内，全中文）",
    "本期最重磅要闻2（26字以内，全中文）",
    "本期最重磅要闻3（26字以内，全中文）"
  ],
  "categories": [
    {{
      "category_index": 1,
      "category_title": "全球宏观与地缘政经",
      "summary_line": "该领域本期一句话风向综述（24字以内，全中文）",
      "items": [
        {{
          "news_id": "EN_3",
          "title": "外媒新闻翻译后的中文精炼标题（18字以内，不加星号）",
          "fact": "翻译后的中文核心事实或数据（40字以内）",
          "insight": "舆论倾向或行业影响洞察（32字以内）"
        }},
        {{
          "news_id": "ZH_2",
          "title": "中文媒体新闻的精炼标题（18字以内，不加星号）",
          "fact": "核心事实或讨论焦点（40字以内）",
          "insight": "舆论倾向或行业影响洞察（32字以内）"
        }}
      ]
    }}
  ]
}}

【分类序号对照表】
1: 全球宏观与地缘政经 | 2: 商业与金融市场 | 3: 科技与人工智能 | 4: 汽车工业与新能源 | 5: X平台热点与全网讨论 | 6: 港澳台地区动态 | 7: 全球体育赛事 | 8: 文娱与影视音乐 | 9: 产业供应链与全球综合
"""

    models_to_try = list(CANDIDATE_MODELS)
    for dm in discover_available_flash_models(client):
        if dm not in models_to_try:
            models_to_try.append(dm)

    last_error = None
    for model_name in models_to_try:
        try:
            print(f"正在调用模型 [{model_name}] 进行 1:1 中英配额筛选与中文精译...")
            response = client.models.generate_content(
                model=model_name,
                contents=f"以下是过去 {MAX_NEWS_AGE_HOURS} 小时内抓取的中英双语候选池：\n\n{raw_news_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.15,
                    response_mime_type="application/json",
                ),
            )
            raw_json = (response.text or "").strip()
            if raw_json.startswith("```"):
                raw_json = raw_json.strip("`").removeprefix("json").strip()
            data = json.loads(raw_json)
            print(f"✅ 模型 [{model_name}] 筛选与翻译完成！")
            return data
        except Exception as e:
            print(f"⚠️ 模型 [{model_name}] 调用失败: {e}")
            last_error = e
            time.sleep(2)

    print(f"❌ 所有候选模型均调用失败，最后报错: {last_error}")
    sys.exit(1)


# ==================== 5. 动态组装消息（带 [🌐外媒精译] 与 [🇨🇳中文] 标识） ====================
def shorten_url(long_url: str) -> str:
    if not long_url or len(long_url) <= 60:
        return long_url
    try:
        api_url = f"[https://tinyurl.com/api-create.php?url=](https://tinyurl.com/api-create.php?url=){urllib.parse.quote(long_url, safe='')}"
        resp = requests.get(api_url, timeout=3)
        if resp.status_code == 200 and resp.text.startswith("http"):
            return resp.text.strip()
    except Exception:
        pass
    return long_url


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "...(详见链接)".encode("utf-8")
    cut_bytes = encoded[: max_bytes - len(suffix)]
    return cut_bytes.decode("utf-8", errors="ignore") + "...(详见链接)"


def build_dynamic_wechat_messages(
    report: dict, url_map: dict, raw_title_map: dict, lang_map: dict
) -> tuple[List[str], List[str], List[str], List[str]]:
    date_str, edition_tag = get_edition_label()

    emojis = {1: "🌍", 2: "📈", 3: "🤖", 4: "🚗", 5: "🐦", 6: "🇭🇰", 7: "⚽", 8: "🎬", 9: "🏭"}
    num_icons = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}
    messages = []

    used_raw_titles = []
    used_raw_urls = []
    ai_generated_titles = []

    en_selected_count = 0
    zh_selected_count = 0

    raw_categories = report.get("categories", [])
    top_3 = report.get("top_3_highlights", [])

    # 只保留真实存在且非空的分类
    valid_categories = []
    for cat in sorted(raw_categories, key=lambda x: x.get("category_index", 99)):
        valid_items = []
        for item in cat.get("items", []):
            nid = str(item.get("news_id", "")).strip().upper()
            if nid in url_map:
                item["news_id"] = nid
                valid_items.append(item)
        if valid_items:
            cat["items"] = valid_items
            valid_categories.append(cat)

    total_cats = len(valid_categories)

    for msg_idx, cat in enumerate(valid_categories, start=1):
        cat_idx = cat.get("category_index", msg_idx)
        emoji = emojis.get(cat_idx, "📌")
        cat_title = str(cat.get("category_title", f"分类情报 {msg_idx}")).replace("**", "")
        summary_line = str(cat.get("summary_line", "本期重点动态汇总")).replace("**", "")
        items = cat.get("items", [])

        lines = [
            f"【{msg_idx}/{total_cats}】{emoji} {cat_title} ({date_str} {edition_tag})",
            f"💡 风向：{summary_line}",
            "━━━━━━━━━━━━━━"
        ]

        if msg_idx == 1 and top_3:
            lines.append(f"🔥 本期全球重磅速览（{edition_tag}）：")
            for h_text in top_3[:3]:
                clean_h = str(h_text).replace("**", "")
                lines.append(f"• {clean_h}")
            lines.append("━━━━━━━━━━━━━━")

        for item_idx, item in enumerate(items, start=1):
            news_id = item["news_id"]
            raw_url = url_map.get(news_id, "")
            raw_title = raw_title_map.get(news_id, "")
            source_lang = lang_map.get(news_id, "zh")

            if source_lang == "en":
                lang_badge = "[🌐外媒]"
                en_selected_count += 1
            else:
                lang_badge = "[🇨🇳中文]"
                zh_selected_count += 1

            short_link = shorten_url(raw_url)
            icon = num_icons.get(item_idx, f"{item_idx}.")

            clean_title = str(item.get("title", "")).replace("**", "")
            clean_fact = str(item.get("fact", "")).replace("**", "")
            clean_insight = str(item.get("insight", "")).replace("**", "")

            if raw_title:
                used_raw_titles.append(raw_title)
            if raw_url:
                used_raw_urls.append(raw_url)
            if clean_title:
                ai_generated_titles.append(clean_title)

            link_line = f"\n🔗 {short_link}" if short_link else ""
            item_block_with_link = (
                f"{icon} {lang_badge} {clean_title}\n"
                f"▸ 事实/话题：{clean_fact}\n"
                f"▸ 舆论/洞察：{clean_insight}{link_line}"
            )
            item_block_no_link = (
                f"{icon} {lang_badge} {clean_title}\n"
                f"▸ 事实/话题：{clean_fact}\n"
                f"▸ 舆论/洞察：{clean_insight}"
            )

            current_text = "\n".join(lines)
            candidate_with_link = f"{current_text}\n\n{item_block_with_link}"
            if utf8_len(candidate_with_link) <= MAX_WECOM_BYTES:
                lines.append(item_block_with_link)
                continue

            candidate_no_link = f"{current_text}\n\n{item_block_no_link}"
            if utf8_len(candidate_no_link) <= MAX_WECOM_BYTES:
                lines.append(item_block_no_link)
            else:
                remaining_bytes = MAX_WECOM_BYTES - utf8_len(current_text) - 4
                if remaining_bytes > 90:
                    lines.append(truncate_to_bytes(item_block_no_link, remaining_bytes))
                break

        header_part = "\n".join(lines[:3])
        body_items = lines[3:]
        final_msg = header_part + "\n" + "\n\n".join(body_items)
        final_msg = truncate_to_bytes(final_msg.strip(), MAX_WECOM_BYTES)
        messages.append(final_msg)

    print(
        f"📊 最终入选语种统计：🌐 英文外媒精译 {en_selected_count} 条 | "
        f"🇨🇳 中文媒体 {zh_selected_count} 条（共 {total_cats} 条分类消息）。"
    )
    return messages, used_raw_titles, used_raw_urls, ai_generated_titles


# ==================== 6. 依次发送消息至企业微信/个人微信 ====================
def push_messages_to_wecom(messages: List[str]):
    webhook_url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("❌ [错误] 未检测到 WECOM_WEBHOOK_URL 环境变量！")
        sys.exit(1)

    total = len(messages)
    if total == 0:
        print("⚠️ 本期经过严格过滤后无值得推送的新鲜重磅消息，跳过推送。")
        return

    print(f"准备向企业微信/个人微信依次发送 {total} 条分类纯文本消息...")

    for idx, msg in enumerate(messages, start=1):
        byte_size = utf8_len(msg)
        payload = {
            "msgtype": "text",
            "text": {
                "content": msg
            }
        }

        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp_json = resp.json() if resp.status_code == 200 else {}
        errcode = resp_json.get("errcode", -1)

        print(
            f"-> 第 [{idx}/{total}] 条推送完成 | "
            f"字节数: {byte_size}/2048 Bytes | "
            f"状态码: {resp.status_code} | 返回结果: {resp.text}"
        )

        if errcode != 0:
            print(f"❌ [错误告警] 第 {idx} 条消息发送失败，企业微信返回: {resp.text}")
            sys.exit(1)

        if idx < total:
            time.sleep(2.5)


def main():
    date_str, edition_tag = get_edition_label()
    print(f"========== 启动全球情报系统 ({date_str} {edition_tag}) ==========")

    # 1. 加载过去 72 小时已推送记录
    seen_fingerprints, recent_ai_titles, history_store = load_sent_history()

    # 2. 中英分池抓取过去 15 小时内的新鲜新闻
    raw_news_text, url_map, raw_title_map, lang_map = fetch_recent_news(seen_fingerprints)
    if not raw_news_text.strip():
        print("⚠️ 本时段没有抓取到任何满足 15 小时内的新鲜未推新闻，程序正常结束。")
        return

    # 3. AI 筛选分析（强制 1:1 中英配额 + 英文自动精译为中文）
    report = analyze_news_with_ai(raw_news_text, recent_ai_titles)

    # 4. 动态组装非空分类消息
    messages, used_raw_titles, used_raw_urls, ai_titles = build_dynamic_wechat_messages(
        report, url_map, raw_title_map, lang_map
    )

    # 5. 推送至企业微信/个人微信
    push_messages_to_wecom(messages)

    # 6. 保存已推送记录
    if ai_titles:
        save_sent_history(history_store, used_raw_titles, used_raw_urls, ai_titles)
    print(f"🎉 {date_str} {edition_tag} 情报任务执行完毕！")


if __name__ == "__main__":
    main()
