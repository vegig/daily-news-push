import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List
import feedparser
import requests
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==================== 1. 全局参数与 5 大分类订阅源配置 ====================
# 企业微信单条硬性上限为 2048 字节，这里设置 1980 字节作为安全红线
MAX_WECOM_BYTES = 1980
# 每个 RSS 源提取的最大候选条数
MAX_ITEMS_PER_FEED = 10
# 推送格式：可选 "markdown"（企微App内排版最佳）或 "text"（适合个人微信插件直接阅读）
WECOM_MSG_TYPE = os.environ.get("WECOM_MSG_TYPE", "markdown").lower()

# 模型优先级列表：优先使用报错提示推荐的 gemini-3.8-flash 与官方动态别名 gemini-flash-latest
CANDIDATE_MODELS = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.0-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]


def build_google_news_rss(query: str, lang: str = "en-US", country: str = "US") -> str:
    """构造 Google News 过去 24 小时关键词动态 RSS"""
    encoded_query = urllib.parse.quote(query)
    if lang == "zh-CN":
        return f"https://news.google.com/rss/search?q={encoded_query}+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return f"https://news.google.com/rss/search?q={encoded_query}+when:1d&hl={lang}&gl={country}&ceid={country}:en"


# 严格对应 5 条独立消息的 5 大分类源
RSS_FEEDS = {
    "1_全球宏观与地缘政经": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        build_google_news_rss("site:reuters.com world OR geopolitics OR central bank"),
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
    "5_产业供应链与全球综合": [
        build_google_news_rss("supply chain OR semiconductor manufacturing OR energy transition OR shipping"),
        build_google_news_rss("供应链 OR 高端制造 OR 能源 OR 国际贸易", lang="zh-CN"),
    ]
}


# ==================== 2. 定义 Gemini 结构化输出 Schema ====================
class NewsItem(BaseModel):
    news_id: str = Field(description="对应原始新闻池中的编号，如 N1, N15")
    title: str = Field(description="中文精炼标题（控制在14-22个汉字以内）")
    fact: str = Field(description="核心事实与关键数据（控制在40-55个汉字以内，精炼准确）")
    insight: str = Field(description="商业或行业影响洞察（控制在25-40个汉字以内，一针见血）")


class CategoryBlock(BaseModel):
    category_index: int = Field(description="分类序号，严格为 1 到 5")
    category_title: str = Field(description="分类标题，如：全球宏观与地缘政经")
    summary_line: str = Field(description="该领域今日一句话风向综述（控制在28个汉字以内）")
    items: List[NewsItem] = Field(description="该分类下精选的 3 到 4 条核心新闻")


class DailyIntelligenceReport(BaseModel):
    top_3_highlights: List[str] = Field(
        description="全天跨领域最重磅的 3 条核心要闻速览，每条控制在 30 个汉字以内"
    )
    categories: List[CategoryBlock] = Field(
        description="严格按顺序排列的 5 个分类情报块（序号 1 到 5）"
    )


# ==================== 3. 抓取新闻并建立短编号映射 ====================
def fetch_recent_news() -> tuple[str, dict]:
    print("开始抓取全球 5 大分类新闻源...")
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(hours=26)

    collected_sections = []
    seen_titles = set()
    url_map = {}
    counter = 1

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }

    for category_key, urls in RSS_FEEDS.items():
        cat_items = []
        for url in urls:
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                feed = feedparser.parse(resp.content)
                for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", "").strip()

                    if not title or title in seen_titles:
                        continue

                    pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub_parsed:
                        pub_dt = datetime.fromtimestamp(time.mktime(pub_parsed), tz=timezone.utc)
                        if pub_dt < cutoff_time:
                            continue

                    seen_titles.add(title)
                    news_id = f"N{counter}"
                    url_map[news_id] = link
                    counter += 1

                    clean_summary = summary[:180].replace("\n", " ")
                    cat_items.append(f"[{news_id}] 标题: {title} | 摘要: {clean_summary}")
            except Exception as e:
                print(f"[警告] 抓取源失败 ({url}): {e}")

        if cat_items:
            collected_sections.append(f"=== 分类候选池：{category_key} ===\n" + "\n".join(cat_items))

    print(f"✅ 新闻抓取完成，共获取 {counter - 1} 条候选新闻。")
    return "\n\n".join(collected_sections), url_map


# ==================== 4. 调用 Gemini 生成结构化 5 分类情报（支持自动切换模型） ====================
def discover_available_flash_models(client: genai.Client) -> List[str]:
    """当预设模型名称发生变更时，动态从 API 查询当前 Key 支持的 Flash 模型"""
    discovered = []
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            # 选择包含 flash 且不包含旧版 2.0/2.5 及语音/图片专用后缀的模型
            if "flash" in name and not any(
                skip in name for skip in ["2.0", "2.5", "tts", "image", "audio", "live", "thinking"]
            ):
                discovered.append(name)
    except Exception as e:
        print(f"[提示] 动态查询模型列表失败: {e}")
    return discovered


def analyze_news_with_ai(raw_news_text: str) -> DailyIntelligenceReport:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("未检测到 GEMINI_API_KEY 环境变量！")

    client = genai.Client(api_key=api_key)
    beijing_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(beijing_tz).strftime("%Y年%m月%d日")

    system_prompt = f"""你是一位顶级的全球商业与科技首席情报官。今天是 {today_str}。
请从用户提供的 5 大分类原始新闻候选池中，跨源去重、剔除噪音，严格按照指定的 5 个分类整理出今日全球情报：
- 分类 1：全球宏观与地缘政经
- 分类 2：商业与金融市场
- 分类 3：科技与人工智能
- 分类 4：汽车工业与新能源
- 分类 5：产业供应链与全球综合

【核心要求】
1. 每个分类精选 3 到 4 条最具价值的新闻，必须准确填写对应的 `news_id`（如 N12）。
2. 所有外文内容必须翻译为地道、专业的中文（保留关键行业缩写如 OEM、800V、Fed、LLM、M&A 等）。
3. 语言务必高度凝练！严格遵守每个字段的字数限制，以便单条分类消息控制在企业微信 2048 字节以内。
"""

    # 合并预设候选模型与动态探测模型
    models_to_try = list(CANDIDATE_MODELS)
    for dm in discover_available_flash_models(client):
        if dm not in models_to_try:
            models_to_try.append(dm)

    last_error = None
    for model_name in models_to_try:
        try:
            print(f"正在调用模型 [{model_name}] 生成结构化 5 分类情报...")
            response = client.models.generate_content(
                model=model_name,
                contents=f"以下是过去24小时抓取的全球新闻池：\n\n{raw_news_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=DailyIntelligenceReport,
                ),
            )
            print(f"✅ 模型 [{model_name}] 调用成功！")
            return DailyIntelligenceReport.model_validate_json(response.text)
        except Exception as e:
            print(f"⚠️ 模型 [{model_name}] 不可用或调用失败: {e}")
            last_error = e
            time.sleep(1.5)

    print(f"❌ 所有候选模型均调用失败，最后报错: {last_error}")
    sys.exit(1)


# ==================== 5. 短链接压缩与 2048 字节安全组装器 ====================
def shorten_url(long_url: str) -> str:
    """将冗长的 Google News 链接压缩为短链，大幅节省字节额度"""
    if not long_url or len(long_url) <= 60:
        return long_url
    try:
        api_url = f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(long_url, safe='')}"
        resp = requests.get(api_url, timeout=4)
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
    suffix = "...(详见原文)".encode("utf-8")
    cut_bytes = encoded[: max_bytes - len(suffix)]
    return cut_bytes.decode("utf-8", errors="ignore") + "...(详见原文)"


def build_5_wecom_messages(report: DailyIntelligenceReport, url_map: dict) -> List[str]:
    beijing_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(beijing_tz).strftime("%m月%d日")
    emojis = {1: "🌍", 2: "📈", 3: "🤖", 4: "🚗", 5: "🏭"}
    messages = []

    sorted_categories = sorted(report.categories, key=lambda x: x.category_index)

    for idx, cat in enumerate(sorted_categories[:5], start=1):
        emoji = emojis.get(idx, "📌")
        lines = []

        if WECOM_MSG_TYPE == "markdown":
            lines.append(f"**【{idx}/5】{emoji} {cat.category_title} ({today_str})**")
            lines.append(f"> 💡 **风向**：{cat.summary_line}")
        else:
            lines.append(f"【{idx}/5】{emoji} {cat.category_title} ({today_str})")
            lines.append(f"💡 风向：{cat.summary_line}")

        if idx == 1 and report.top_3_highlights:
            lines.append("")
            lines.append("🔥 **今日全球重磅速览**：" if WECOM_MSG_TYPE == "markdown" else "🔥 今日全球重磅速览：")
            for h_text in report.top_3_highlights[:3]:
                lines.append(f"• {h_text}")

        lines.append("")

        for item_idx, item in enumerate(cat.items, start=1):
            raw_url = url_map.get(item.news_id, "")
            short_link = shorten_url(raw_url)

            if WECOM_MSG_TYPE == "markdown":
                link_part = f" [原文]({short_link})" if short_link else ""
                item_block_with_link = (
                    f"**{item_idx}. {item.title}**{link_part}\n"
                    f"- **事实**：{item.fact}\n"
                    f"- **洞察**：{item.insight}"
                )
                item_block_no_link = (
                    f"**{item_idx}. {item.title}**\n"
                    f"- **事实**：{item.fact}\n"
                    f"- **洞察**：{item.insight}"
                )
            else:
                link_part = f"\n🔗 {short_link}" if short_link else ""
                item_block_with_link = (
                    f"{item_idx}. 【{item.title}】\n"
                    f"• 事实：{item.fact}\n"
                    f"• 洞察：{item.insight}{link_part}"
                )
                item_block_no_link = (
                    f"{item_idx}. 【{item.title}】\n"
                    f"• 事实：{item.fact}\n"
                    f"• 洞察：{item.insight}"
                )

            current_text = "\n".join(lines)
            candidate_with_link = f"{current_text}\n\n{item_block_with_link}"
            if utf8_len(candidate_with_link) <= MAX_WECOM_BYTES:
                lines.append(item_block_with_link)
                lines.append("")
                continue

            candidate_no_link = f"{current_text}\n\n{item_block_no_link}"
            if utf8_len(candidate_no_link) <= MAX_WECOM_BYTES:
                lines.append(item_block_no_link)
                lines.append("")
            else:
                remaining_bytes = MAX_WECOM_BYTES - utf8_len(current_text) - 4
                if remaining_bytes > 90:
                    lines.append(truncate_to_bytes(item_block_no_link, remaining_bytes))
                break

        final_msg = truncate_to_bytes("\n".join(lines).strip(), MAX_WECOM_BYTES)
        messages.append(final_msg)

    return messages


# ==================== 6. 依次发送 5 条消息至企业微信 ====================
def push_5_messages_to_wecom(messages: List[str]):
    webhook_url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise ValueError("未检测到 WECOM_WEBHOOK_URL 环境变量！")

    total = len(messages)
    print(f"准备向企业微信依次发送 {total} 条分类消息（模式: {WECOM_MSG_TYPE}）...")

    for idx, msg in enumerate(messages, start=1):
        byte_size = utf8_len(msg)
        if WECOM_MSG_TYPE == "markdown":
            payload = {"msgtype": "markdown", "markdown": {"content": msg}}
        else:
            payload = {"msgtype": "text", "text": {"content": msg}}

        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp_json = resp.json() if resp.status_code == 200 else {}
        errcode = resp_json.get("errcode", -1)

        print(
            f"-> 第 [{idx}/{total}] 条推送完成 | "
            f"字节数: {byte_size}/2048 Bytes | "
            f"状态码: {resp.status_code} | 返回结果: {resp.text}"
        )

        if errcode != 0:
            print(f"[错误告警] 第 {idx} 条消息发送异常，请检查上方返回结果！")
            sys.exit(1)

        if idx < total:
            time.sleep(1.5)


def main():
    raw_news_text, url_map = fetch_recent_news()
    if not raw_news_text.strip():
        print("未抓取到过去 24 小时的有效新闻，程序退出。")
        return

    report = analyze_news_with_ai(raw_news_text)
    messages = build_5_wecom_messages(report, url_map)
    push_5_messages_to_wecom(messages)
    print("✅ 今日 5 条分类情报全部推送完毕！")


if __name__ == "__main__":
    main()
