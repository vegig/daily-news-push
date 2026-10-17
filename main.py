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

# ==================== 1. 全局参数与 9 大分类订阅源配置 ====================
MAX_WECOM_BYTES = 1980          # 严格死守企业微信 text 消息 2048 字节红线
MAX_ITEMS_PER_FEED = 8          # 每个订阅源最大提取条数
HISTORY_FILE = "sent_history.json"  # 本地去重历史记录文件
HISTORY_RETENTION_HOURS = 48    # 历史记录保留 48 小时，防止跨天重复

CANDIDATE_MODELS = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.0-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]


def build_google_news_rss(query: str, lang: str = "en-US", country: str = "US") -> str:
    encoded_query = urllib.parse.quote(query)
    if lang == "zh-CN":
        return f"https://news.google.com/rss/search?q={encoded_query}+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return f"https://news.google.com/rss/search?q={encoded_query}+when:1d&hl={lang}&gl={country}&ceid={country}:en"


RSS_FEEDS = {
    "1_全球宏观与地缘政经": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        build_google_news_rss("Reuters world OR geopolitics OR central bank"),
    ],
    "2_商业与金融市场": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        build_google_news_rss("Wall Street OR global markets OR M&A OR earnings"),
        build_google_news_rss("宏观经济 OR 金融市场 OR 财报 OR 并购", lang="zh-CN"),
    ],
    "3_科技与人工智能": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://36kr.com/feed",
        "https://www.ithome.com/rss/",
    ],
    "4_汽车工业与新能源": [
        build_google_news_rss("automotive OR EV OR electric vehicle OR powertrain OR battery"),
        build_google_news_rss("新能源汽车 OR 动力电池 OR 电驱 OR 车企 OR 自动驾驶", lang="zh-CN"),
    ],
    "5_X平台热点与全网讨论": [
        build_google_news_rss("X Twitter viral OR trending OR Elon Musk OR backlash OR debate"),
        "https://hnrss.org/frontpage?points=150",
        build_google_news_rss("热搜 OR 网友热议 OR 舆论 OR 争议 OR 刷屏", lang="zh-CN"),
    ],
    "6_港澳台地区动态": [
        build_google_news_rss("Hong Kong OR Macau OR Taiwan economy"),
        build_google_news_rss("香港 OR 澳门 OR 台湾 OR 大湾区", lang="zh-CN"),
    ],
    "7_全球体育赛事": [
        build_google_news_rss("sports OR Champions League OR NBA OR Premier League OR tennis"),
        build_google_news_rss("体育 OR 足球 OR 篮球 OR 欧冠 OR NBA OR 赛事", lang="zh-CN"),
    ],
    "8_文娱与影视音乐": [
        build_google_news_rss("Hollywood OR box office OR Netflix OR streaming OR celebrity"),
        build_google_news_rss("娱乐 OR 电影 OR 剧集 OR 票房 OR 演唱会", lang="zh-CN"),
    ],
    "9_产业供应链与全球综合": [
        build_google_news_rss("supply chain OR semiconductor manufacturing OR energy transition"),
        build_google_news_rss("供应链 OR 高端制造 OR 能源 OR 国际贸易", lang="zh-CN"),
    ]
}


# ==================== 2. 新增：历史去重记忆库管理（早晚报防重核心） ====================
def make_fingerprint(text: str) -> str:
    """生成新闻标题或链接的唯一 MD5 指纹"""
    clean = "".join(text.lower().split())
    return hashlib.md5(clean.encode("utf-8")).hexdigest()


def load_sent_history() -> tuple[set, List[str], dict]:
    """读取过去 48 小时内已推送过的新闻指纹与 AI 已报标题"""
    now_ts = time.time()
    cutoff_ts = now_ts - (HISTORY_RETENTION_HOURS * 3600)

    raw_data = {"fingerprints": {}, "reported_events": []}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            print(f"[提示] 读取历史记录文件异常，将重新初始化: {e}")

    # 仅保留 48 小时以内的有效记录，自动清理过期记录防止文件膨胀
    valid_fps = {
        fp: ts for fp, ts in raw_data.get("fingerprints", {}).items() if ts > cutoff_ts
    }
    valid_events = [
        ev for ev in raw_data.get("reported_events", []) if ev.get("ts", 0) > cutoff_ts
    ]

    recent_ai_titles = [ev["title"] for ev in valid_events[-35:]]
    cleaned_store = {"fingerprints": valid_fps, "reported_events": valid_events}

    print(f"📚 已加载历史去重库：包含 {len(valid_fps)} 条历史原文指纹，{len(recent_ai_titles)} 条近期已报事件。")
    return set(valid_fps.keys()), recent_ai_titles, cleaned_store


def save_sent_history(
    history_store: dict,
    used_raw_titles: List[str],
    used_raw_urls: List[str],
    ai_generated_titles: List[str],
):
    """将本次已推送的新闻写入 sent_history.json，供下一次（如晚报）去重使用"""
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
    history_store["reported_events"] = events[-80:]  # 最多保留最近80条AI事件标题

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_store, f, ensure_ascii=False, indent=2)
    print(f"💾 已更新去重记录库 ({HISTORY_FILE})，新增 {len(ai_generated_titles)} 条已推送事件。")


# ==================== 3. 抓取新闻并执行第一重物理过滤 ====================
def fetch_recent_news(seen_fingerprints: set) -> tuple[str, dict, dict]:
    print("开始抓取全球 9 大分类新闻与社交热点源...")
    now = datetime.now(timezone.utc)
    # 配合一天两次推送，优先聚焦最近 14 小时内的新鲜资讯
    cutoff_time = now - timedelta(hours=14)

    collected_sections = []
    current_run_titles = set()
    url_map = {}
    raw_title_map = {}
    counter = 1
    skipped_history_count = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }

    for category_key, urls in RSS_FEEDS.items():
        cat_items = []
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

                    # 【第一重去重】检查是否已在早上（或前一天）推送过
                    title_fp = make_fingerprint(title)
                    link_fp = make_fingerprint(link) if link else ""
                    if title_fp in seen_fingerprints or (link_fp and link_fp in seen_fingerprints):
                        skipped_history_count += 1
                        continue

                    # 检查发布时间
                    pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub_parsed:
                        pub_dt = datetime.fromtimestamp(time.mktime(pub_parsed), tz=timezone.utc)
                        if pub_dt < cutoff_time:
                            continue

                    current_run_titles.add(title)
                    news_id = f"N{counter}"
                    url_map[news_id] = link
                    raw_title_map[news_id] = title
                    counter += 1

                    clean_summary = summary[:140].replace("\n", " ").replace("\r", " ")
                    cat_items.append(f"[{news_id}] 标题: {title} | 摘要: {clean_summary}")
            except Exception as e:
                print(f"[警告] 抓取源跳过 ({url[:45]}...): {e}")

        if cat_items:
            collected_sections.append(f"=== 分类候选池：{category_key} ===\n" + "\n".join(cat_items))

    print(
        f"✅ 新闻抓取完成：共获取 {counter - 1} 条全新候选新闻 "
        f"（已自动拦截过滤 {skipped_history_count} 条往期已推送旧闻）。"
    )
    return "\n\n".join(collected_sections), url_map, raw_title_map


# ==================== 4. 调用 Gemini 生成情报（带第二重语义去重） ====================
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
    """根据北京时间自动判断当前是【早报】还是【晚报】"""
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

    # 【第二重去重】将上一期（如早上9点）已发送的标题列表注入给 AI，防止换汤不换药的同事件报道
    dedup_instruction = ""
    if recent_ai_titles:
        joined_titles = "\n".join(f"- {t}" for t in recent_ai_titles)
        dedup_instruction = f"""
【🚨 跨期严禁重复指令（极重要）】
以下是上一期（如今日早报）已经向用户推送过的事件清单。
即使候选池中有其他媒体用不同角度或不同语言再次报道以下同一事件，也**必须坚决剔除**，绝不能重复入选（除非出现了颠覆性的全新反转进展）：
{joined_titles}
"""

    system_prompt = f"""你是一位顶级的全球商业、科技与社交舆情首席情报官。当前北京时间是 {today_str}。
请从用户提供的 9 大分类原始新闻与社媒候选池中，跨源去重、剔除噪音，严格输出 JSON 格式的全球情报。
{dedup_instruction}
【必须严格输出的 JSON 格式示例】
{{
  "top_3_highlights": [
    "本期最重磅要闻1（26字以内）",
    "本期最重磅要闻2（26字以内）",
    "本期最重磅要闻3（26字以内）"
  ],
  "categories": [
    {{
      "category_index": 1,
      "category_title": "全球宏观与地缘政经",
      "summary_line": "该领域本期一句话风向综述（24字以内）",
      "items": [
        {{
          "news_id": "N1",
          "title": "中文精炼标题（18字以内，不加星号）",
          "fact": "核心事实或讨论焦点（40字以内）",
          "insight": "舆论倾向或行业影响洞察（32字以内）"
        }}
      ]
    }}
  ]
}}

【核心要求】
1. `categories` 数组必须严格包含序号 1 到 9 的 9 个分类对象，一个都不能少：
   - 1: 全球宏观与地缘政经
   - 2: 商业与金融市场
   - 3: 科技与人工智能
   - 4: 汽车工业与新能源
   - 5: X平台热点与全网讨论
   - 6: 港澳台地区动态
   - 7: 全球体育赛事
   - 8: 文娱与影视音乐
   - 9: 产业供应链与全球综合
2. 每个分类下的 `items` 精选 2 到 3 条最具价值的新鲜动态，准确填写对应的 `news_id`（如 N12）。
3. 所有外文内容必须翻译为地道、专业的中文，不要使用 Markdown 加粗星号（**）。
"""

    models_to_try = list(CANDIDATE_MODELS)
    for dm in discover_available_flash_models(client):
        if dm not in models_to_try:
            models_to_try.append(dm)

    last_error = None
    for model_name in models_to_try:
        try:
            print(f"正在调用模型 [{model_name}] 生成结构化 9 分类情报...")
            response = client.models.generate_content(
                model=model_name,
                contents=f"以下是最新抓取的全球新闻与社媒候选池：\n\n{raw_news_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            raw_json = (response.text or "").strip()
            if raw_json.startswith("```"):
                raw_json = raw_json.strip("`").removeprefix("json").strip()
            data = json.loads(raw_json)
            cats = data.get("categories", [])
            if not cats:
                raise ValueError("模型返回的 categories 列表为空！")
            print(f"✅ 模型 [{model_name}] 调用成功！共生成 {len(cats)} 个分类块。")
            return data
        except Exception as e:
            print(f"⚠️ 模型 [{model_name}] 调用失败: {e}")
            last_error = e
            time.sleep(2)

    print(f"❌ 所有候选模型均调用失败，最后报错: {last_error}")
    sys.exit(1)


# ==================== 5. 短链接压缩与 2048 字节安全组装器 ====================
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


def build_9_wechat_text_messages(
    report: dict, url_map: dict, raw_title_map: dict
) -> tuple[List[str], List[str], List[str], List[str]]:
    date_str, edition_tag = get_edition_label()

    emojis = {1: "🌍", 2: "📈", 3: "🤖", 4: "🚗", 5: "🐦", 6: "🇭🇰", 7: "⚽", 8: "🎬", 9: "🏭"}
    num_icons = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}
    messages = []

    used_raw_titles = []
    used_raw_urls = []
    ai_generated_titles = []

    categories = report.get("categories", [])
    top_3 = report.get("top_3_highlights", [])
    sorted_categories = sorted(categories, key=lambda x: x.get("category_index", 99))
    total_cats = len(sorted_categories[:9])

    for idx, cat in enumerate(sorted_categories[:9], start=1):
        emoji = emojis.get(idx, "📌")
        cat_title = str(cat.get("category_title", f"分类情报 {idx}")).replace("**", "")
        summary_line = str(cat.get("summary_line", "本期重点动态汇总")).replace("**", "")
        items = cat.get("items", [])

        lines = [
            f"【{idx}/{total_cats}】{emoji} {cat_title} ({date_str} {edition_tag})",
            f"💡 风向：{summary_line}",
            "━━━━━━━━━━━━━━"
        ]

        if idx == 1 and top_3:
            lines.append(f"🔥 本期全球重磅速览（{edition_tag}）：")
            for h_text in top_3[:3]:
                clean_h = str(h_text).replace("**", "")
                lines.append(f"• {clean_h}")
            lines.append("━━━━━━━━━━━━━━")

        for item_idx, item in enumerate(items, start=1):
            news_id = str(item.get("news_id", ""))
            raw_url = url_map.get(news_id, "")
            raw_title = raw_title_map.get(news_id, "")
            short_link = shorten_url(raw_url)
            icon = num_icons.get(item_idx, f"{item_idx}.")

            clean_title = str(item.get("title", "")).replace("**", "")
            clean_fact = str(item.get("fact", "")).replace("**", "")
            clean_insight = str(item.get("insight", "")).replace("**", "")

            # 记录本次被选中的新闻，供下一次推送时去重
            if raw_title:
                used_raw_titles.append(raw_title)
            if raw_url:
                used_raw_urls.append(raw_url)
            if clean_title:
                ai_generated_titles.append(clean_title)

            link_line = f"\n🔗 {short_link}" if short_link else ""
            item_block_with_link = (
                f"{icon} {clean_title}\n"
                f"▸ 事实/话题：{clean_fact}\n"
                f"▸ 舆论/洞察：{clean_insight}{link_line}"
            )
            item_block_no_link = (
                f"{icon} {clean_title}\n"
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

    return messages, used_raw_titles, used_raw_urls, ai_generated_titles


# ==================== 6. 依次发送 9 条纯文本消息 ====================
def push_9_messages_to_wecom(messages: List[str]):
    webhook_url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("❌ [错误] 未检测到 WECOM_WEBHOOK_URL 环境变量！")
        sys.exit(1)

    total = len(messages)
    if total == 0:
        print("❌ [错误] 组装出的消息列表为空（0条），终止发送并报错！")
        sys.exit(1)

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

    # 1. 加载过去 48 小时已推送记录
    seen_fingerprints, recent_ai_titles, history_store = load_sent_history()

    # 2. 抓取最新新闻并剔除历史已发原文
    raw_news_text, url_map, raw_title_map = fetch_recent_news(seen_fingerprints)
    if not raw_news_text.strip():
        print("⚠️ 本时段没有抓取到任何未推送过的新闻，程序结束。")
        return

    # 3. AI 筛选分析（注入上一期标题列表进行跨事件语义去重）
    report = analyze_news_with_ai(raw_news_text, recent_ai_titles)

    # 4. 组装 9 条消息并收集本次已使用的标题与链接
    messages, used_raw_titles, used_raw_urls, ai_titles = build_9_wechat_text_messages(
        report, url_map, raw_title_map
    )

    # 5. 推送至企业微信/个人微信
    push_9_messages_to_wecom(messages)

    # 6. 推送成功后，将本期新闻写入 sent_history.json 供下一次去重
    save_sent_history(history_store, used_raw_titles, used_raw_urls, ai_titles)
    print(f"🎉 {date_str} {edition_tag} 9 大分类情报全部推送并归档完毕！")


if __name__ == "__main__":
    main()
