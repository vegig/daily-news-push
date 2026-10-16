#!/usr/bin/env python3
"""每日新闻推送脚本 —— 通过 GNews API 获取五类新闻并推送"""

import os
import time
import hmac
import hashlib
import base64
import smtplib
import requests
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.header import Header

# ========== 配置区 ==========
GNEWS_API_KEY = "2bd124deec58b6881adedc0e4c04eaad"

# 五类新闻的 GNews 分类映射
CATEGORIES = {
    "国际重大新闻": "world",
    "国内重大新闻": "nation",
    "体育新闻": "sports",
    "财经新闻": "business",
    "娱乐新闻": "entertainment",
}

# 每类获取条数（免费计划最大 10，付费可调至 20）
MAX_PER_CATEGORY = 10

# 推送渠道配置（至少配置一个）
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")       # 企业微信群机器人
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")      # 飞书自定义机器人
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")        # 飞书签名密钥（可选）
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_SMTP_SERVER = os.environ.get("EMAIL_SMTP_SERVER", "")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT") or "465")
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")


# ========== 新闻获取 ==========
def fetch_news(category: str, max_articles: int = 10) -> list[dict]:
    """调用 GNews Top Headlines 接口获取指定分类新闻"""
    url = "https://gnews.io/api/v4/top-headlines"
    params = {
        "q": category,
        "lang": "en",           # 中文新闻；如需英文改为 "en"
        "max": max_articles,
        "apikey": GNEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("articles", [])
    except Exception as e:
        print(f"[错误] 获取 {category} 新闻失败: {e}")
        return []


def fetch_all_news() -> dict[str, list[dict]]:
    """获取全部五类新闻"""
    result = {}
    for name, cat in CATEGORIES.items():
        print(f"正在获取：{name} ...")
        result[name] = fetch_news(cat, MAX_PER_CATEGORY)
        time.sleep(1.2)  # 免费计划限流 1 请求/秒，留足间隔
    return result


# ========== 新闻去重 ==========
def deduplicate_news(articles: list[dict]) -> list[dict]:
    """按标题归一化后去重，保留最早出现的"""
    seen = set()
    unique = []
    for art in articles:
        title = art.get("title", "").strip().lower()
        # 归一化：去标点、去空白
        normalized = "".join(c for c in title if c.isalnum())
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(art)
    return unique


# ========== 格式化输出 ==========
def format_plain_text(news_dict: dict[str, list[dict]]) -> str:
    """生成纯文本格式，适合企业微信/Telegram"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"📰 每日新闻简报  {now} UTC\n"]
    for category, articles in news_dict.items():
        articles = deduplicate_news(articles)
        lines.append(f"\n{'='*8} {category} {'='*8}\n")
        if not articles:
            lines.append("  （暂无新闻）\n")
            continue
        for i, art in enumerate(articles, 1):
            title = art.get("title", "无标题")
            source = art.get("source", {}).get("name", "未知来源")
            url = art.get("url", "")
            lines.append(f"{i}. {title}")
            lines.append(f"   来源：{source}  |  {url}\n")
    return "\n".join(lines)


def format_markdown(news_dict: dict[str, list[dict]]) -> str:
    """生成 Markdown 格式，适合飞书/邮件"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"# 📰 每日新闻简报  {now} UTC\n"]
    for category, articles in news_dict.items():
        articles = deduplicate_news(articles)
        lines.append(f"\n## {category}\n")
        if not articles:
            lines.append("*（暂无新闻）*\n")
            continue
        for i, art in enumerate(articles, 1):
            title = art.get("title", "无标题")
            source = art.get("source", {}).get("name", "未知来源")
            url = art.get("url", "")
            lines.append(f"{i}. [{title}]({url})  \n   *{source}*\n")
    return "\n".join(lines)


# ========== 推送：企业微信 ==========
def push_to_wecom(content: str):
    """推送到企业微信群机器人（按分类拆分为多条，避免超长）"""
    if not WECOM_WEBHOOK:
        return
    # 企业微信文本消息限制约 2048 字节，按分类拆分发送
    sections = content.split("\n=" * 8)
    for section in sections:
        if not section.strip():
            continue
        payload = {
            "msgtype": "text",
            "text": {"content": section[:2000]},
        }
        try:
            resp = requests.post(WECOM_WEBHOOK, json=payload, timeout=10)
            print(f"[企业微信] 发送结果: {resp.json()}")
        except Exception as e:
            print(f"[企业微信] 发送失败: {e}")
        time.sleep(0.5)


# ========== 推送：飞书 ==========
def gen_feishu_sign(secret: str) -> tuple[str, str]:
    """生成飞书签名"""
    timestamp = str(int(time.time()))
    s = f"{timestamp}\n{secret}"
    h = hmac.new(s.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(h).decode("utf-8")
    return timestamp, sign


def push_to_feishu(content: str):
    """推送到飞书自定义机器人"""
    if not FEISHU_WEBHOOK:
        return
    body = {
        "msg_type": "text",
        "content": {"text": content[:2000]},
    }
    if FEISHU_SECRET:
        ts, sign = gen_feishu_sign(FEISHU_SECRET)
        body["timestamp"] = ts
        body["sign"] = sign
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=body, timeout=10)
        print(f"[飞书] 发送结果: {resp.json()}")
    except Exception as e:
        print(f"[飞书] 发送失败: {e}")


# ========== 推送：Telegram ==========
def push_to_telegram(content: str):
    """推送到 Telegram（按 4000 字符分段）"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram 单条消息限制 4096 字符，按段落拆分
    chunks = []
    current = ""
    for line in content.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            print(f"[Telegram] 发送结果: {resp.status_code}")
        except Exception as e:
            print(f"[Telegram] 发送失败: {e}")
        time.sleep(0.5)


# ========== 推送：邮件 ==========
def push_to_email(content: str):
    """通过 SMTP 发送邮件"""
    if not EMAIL_SMTP_SERVER or not EMAIL_USERNAME or not EMAIL_TO:
        return
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(f"每日新闻简报 {datetime.now().strftime('%Y-%m-%d')}", "utf-8")
    msg["From"] = EMAIL_USERNAME
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT, timeout=30) as smtp:
            smtp.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print("[邮件] 发送成功")
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")


# ========== 主流程 ==========
def main():
    print("=" * 50)
    print(f"开始执行新闻推送 {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)

    # 1. 获取新闻
    news = fetch_all_news()
    total = sum(len(v) for v in news.values())
    print(f"\n共获取 {total} 条新闻")

    # 2. 格式化
    plain = format_plain_text(news)
    markdown = format_markdown(news)

    # 3. 推送（按需启用）
    push_to_wecom(plain)
    push_to_feishu(markdown)
    push_to_telegram(plain)
    push_to_email(plain)

    print("\n执行完毕。")


if __name__ == "__main__":
    main()
