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
# 强制从环境变量读取，绝对不要写在代码里！
GNEWS_API_KEY = os.environ.get("GNEWS_API_KEY", "")

# 五类新闻配置：标准分类使用 category，关键词搜索使用 q
CATEGORIES = {
    "国际重大新闻": "world",
    "国内重大新闻": "China",
    "台湾新闻": "Taiwan",     
    "香港新闻": "Hong Kong",   
    "体育新闻": "sports",
    "财经新闻": "business",
    "娱乐新闻": "entertainment"
}

# GNews 免费计划单次最大 10 条
MAX_PER_CATEGORY = 10

# 推送渠道配置（按需配置）
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")
EMAIL_SMTP_SERVER = os.environ.get("EMAIL_SMTP_SERVER", "")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT") or "465")
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# GNews 标准分类列表，用于判断使用 category 还是 q 参数
GNEWS_STANDARD_CATEGORIES = [
    "world", "nation", "business", "technology",
    "entertainment", "sports", "science", "health"
]


# ========== 新闻获取 ==========
def fetch_news(category: str, max_articles: int = 10) -> list[dict]:
    """调用 GNews API 获取新闻"""
    url = "https://gnews.io/api/v4/top-headlines"
    params = {
        "lang": "zh",           # 建议改回中文，以获取繁体中文新闻
        "max": max_articles,
        "apikey": GNEWS_API_KEY,
    }

    # 根据分类名称，设置不同的请求参数
    if category == "台湾新闻":
        params["country"] = "tw"  # 添加台湾地区代码
        params["q"] = "台灣"       # 可选：添加关键词让结果更精准
    elif category == "香港新闻":
        params["country"] = "hk"  # 添加香港地区代码
        params["q"] = "香港"       # 可选：添加关键词让结果更精准
    elif category in GNEWS_STANDARD_CATEGORIES:
        params["category"] = category
    else:
        params["q"] = category

    # ... 后续的请求代码不变

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
        time.sleep(1.2)  # 免费计划限流，避免触发 429
    return result


# ========== 新闻去重 ==========
def deduplicate_news(articles: list[dict]) -> list[dict]:
    """按标题归一化后去重"""
    seen = set()
    unique = []
    for art in articles:
        title = art.get("title", "").strip().lower()
        normalized = "".join(c for c in title if c.isalnum())
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(art)
    return unique


# ========== 格式化输出 ==========
def format_category_text(category_name: str, articles: list[dict]) -> str:
    """生成单个分类的文本，消息长度可控"""
    articles = deduplicate_news(articles)
    if not articles:
        return f"======== {category_name} ========\n（暂无新闻）"

    lines = [f"======== {category_name} ========\n"]
    for i, art in enumerate(articles, 1):
        title = art.get("title", "无标题")
        source = art.get("source", {}).get("name", "未知来源")
        url = art.get("url", "")
        lines.append(f"{i}. {title}\n   来源：{source}  |  {url}\n")
    return "\n".join(lines)


# ========== 推送：企业微信 ==========
def push_to_wecom(text: str):
    """推送到企业微信群机器人"""
    if not WECOM_WEBHOOK:
        return
    payload = {
        "msgtype": "text",
        "text": {"content": text[:2000]},  # 强制截断到 2000 字节以内
    }
    try:
        resp = requests.post(WECOM_WEBHOOK, json=payload, timeout=10)
        print(f"[企业微信] 发送结果: {resp.json()}")
    except Exception as e:
        print(f"[企业微信] 发送失败: {e}")
    time.sleep(0.5)


# ========== 推送：飞书（备用） ==========
def gen_feishu_sign(secret: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    s = f"{timestamp}\n{secret}"
    h = hmac.new(s.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(h).decode("utf-8")
    return timestamp, sign


def push_to_feishu(text: str):
    if not FEISHU_WEBHOOK:
        return
    body = {
        "msg_type": "text",
        "content": {"text": text[:2000]},
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


# ========== 推送：邮件（备用） ==========
def push_to_email(content: str):
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

    if not GNEWS_API_KEY:
        print("[致命错误] 没有找到 GNEWS_API_KEY，请检查 GitHub Secrets 配置！")
        return

    # 1. 获取新闻
    news = fetch_all_news()

    # 2. 逐类推送，避免长消息被截断
    for category_name, articles in news.items():
        # 格式化这一类新闻
        text_content = format_category_text(category_name, articles)
        
        print(f"\n正在推送：{category_name}（{len(deduplicate_news(articles))} 条）")
        
        # 推送到各渠道
        push_to_wecom(text_content)
        push_to_feishu(text_content)
    
    # 邮件可以发一封完整的（如果需要）
    # full_content = "\n\n".join([format_category_text(k, v) for k, v in news.items()])
    # push_to_email(full_content)

    print("\n执行完毕。")


if __name__ == "__main__":
    main()
