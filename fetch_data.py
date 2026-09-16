#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
fetch_data.py —— 自动抓取真实资讯数据，生成 data.json
==============================================================================
本脚本由 GitHub Actions 定时执行（见 .github/workflows/update-data.yml），
职责：
  1. 从下面 FEED_SOURCES 中配置的各个真实 RSS 源抓取最新资讯；
  2. 把每条资讯归一化成网站需要的字段结构（与原先 mock 数据字段完全一致）；
  3. 从 balldontlie（NBA）和 football-data.org（足球）抓取近期赛程，
     生成"重点赛事预告"数据；
  4. 与仓库里已有的 data.json 合并去重，裁剪到合理条数，写回 data.json。

本地手动运行方式：
    pip install -r requirements.txt
    export FOOTBALL_DATA_API_KEY=你的football-data.org密钥   # 可选
    export BALLDONTLIE_API_KEY=你的balldontlie密钥           # 可选
    python3 fetch_data.py

没有配置 API Key 时，赛事预告部分会跳过对应数据源并打印提示，不会导致脚本失败。
==============================================================================
"""

import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ==============================================================================
# 基础配置
# ==============================================================================

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

# 每次请求使用较为常见的浏览器 UA，降低被部分站点的反爬虫策略拦截的概率
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 15  # 秒

# 每个分类最终保留的最大资讯条数（避免 data.json 随时间无限增长）
MAX_ITEMS_PER_CATEGORY = 80
# 赛事预告最多保留的场次数
MAX_MATCH_PREVIEWS = 20
# 赛事预告只展示未来多少天以内的比赛
MATCH_PREVIEW_WINDOW_DAYS = 10

# ==============================================================================
# 分类元数据（要新增/调整分类，同时也要改 index.html 里的 CATEGORY_META，两边需保持一致）
# ==============================================================================

# 判定"重要新闻"的关键词（标题命中任意一个即标记为 is_important=True）
# 可按需增删；中英文都会匹配（大小写不敏感）
IMPORTANT_KEYWORDS = [
    "突发", "快讯", "重磅", "官宣", "夺冠", "绝杀", "爆冷", "淘汰", "世界杯", "总决赛",
    "冠军", "退役", "引退", "确认", "破纪录",
    "breaking", "confirmed", "official", "exclusive", "champion", "record",
]

# 用于从"海外サッカー"综合外电中筛出"日本旅欧球员"相关报道的关键词（球员姓名，可持续补充）
JAPAN_PLAYER_KEYWORDS = [
    "三笘", "三苫", "久保建英", "久保", "冨安", "南野", "伊東純也", "伊东纯也",
    "遠藤航", "遠藤", "鎌田", "板倉", "旗手", "前田大然", "浅野拓磨", "堂安",
    "鈴木彩艶", "菅原由勢", "伊藤洋輝", "町田浩樹", "瀬古歩夢", "中村敬斗", "田中碧",
    "上田綺世", "北野颯太", "橋岡大樹", "谷口彰悟",
]

# feed 源配置：category -> 该分类下要抓取的一个或多个 RSS 源
# 每个源: url(必填) / source(展示用来源名) / keyword_filter(可选，仅保留标题命中关键词的条目)
FEED_SOURCES = {
    "ai_cn": [
        {"url": "https://www.qbitai.com/feed", "source": "量子位"},
    ],
    "ai_intl": [
        {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "source": "TechCrunch"},
        {"url": "https://venturebeat.com/category/ai/feed/", "source": "VentureBeat"},
    ],
    "biz_cn": [
        {"url": "https://36kr.com/feed", "source": "36氪"},
    ],
    "biz_intl": [
        {"url": "https://finance.yahoo.com/news/rssindex", "source": "Yahoo Finance"},
    ],
    "jp_football_euro": [
        # "海外サッカー"是欧洲足球综合报道，混杂日本旅欧球员与其他欧洲足球新闻，
        # 用 JAPAN_PLAYER_KEYWORDS 过滤，只保留提到日本球员的条目
        {"url": "https://web.gekisaka.jp/feed?category=foreign", "source": "ゲキサカ", "keyword_filter": JAPAN_PLAYER_KEYWORDS},
        # 日本代表（国家队）新闻：主力多为旅欧球员，整体归入该分类，不做关键词过滤
        {"url": "https://web.gekisaka.jp/feed?category=nationalteam", "source": "ゲキサカ 日本代表"},
    ],
    "jp_football_highschool": [
        {"url": "https://web.gekisaka.jp/feed?category=youth", "source": "ゲキサカ"},
    ],
    "football_intl": [
        {"url": "https://sports.yahoo.com/soccer/rss.xml", "source": "Yahoo Sports"},
    ],
    "nba": [
        {"url": "https://sports.yahoo.com/nba/rss.xml", "source": "Yahoo Sports"},
    ],
}

# football-data.org 的赛事代码 -> 中文展示名（免费版可访问的主要赛事，可按需增删）
FOOTBALL_DATA_COMPETITIONS = {
    "PL": "英超",
    "CL": "欧冠",
    "PD": "西甲",
    "BL1": "德甲",
    "SA": "意甲",
    "FL1": "法甲",
}


# ==============================================================================
# 工具函数
# ==============================================================================

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def strip_html(raw_html):
    """去除 RSS 摘要里的 HTML 标签，并把常见 HTML 实体转回普通字符"""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", "", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, max_len=120):
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def make_id(prefix, unique_key):
    """用来源URL等唯一信息生成稳定的短哈希ID，保证同一篇文章多次抓取时ID不变（便于去重）"""
    digest = hashlib.md5(unique_key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def parse_entry_time(entry):
    """从 feedparser 的 entry 中提取发布时间，统一转换为 ISO 8601 字符串（UTC）"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                continue
    # 实在没有时间信息时，退化使用当前抓取时间（极少数不规范的 feed 会走到这里）
    return datetime.now(timezone.utc).isoformat()


def is_important(title):
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in IMPORTANT_KEYWORDS)


def matches_keyword_filter(title, summary, keywords):
    if not keywords:
        return True
    haystack = f"{title} {summary}"
    return any(kw in haystack for kw in keywords)


# ==============================================================================
# 抓取资讯（RSS）
# ==============================================================================

def fetch_feed(url):
    """请求并解析单个 RSS 源；网络或解析失败时返回空列表，不让整个脚本崩溃"""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log(f"  [警告] 请求失败：{url} -> {e}")
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log(f"  [警告] 解析失败或无内容：{url} -> {parsed.bozo_exception}")
        return []
    return parsed.entries


def fetch_category_news(category, sources):
    log(f"抓取分类 [{category}] ...")
    items = []
    for src in sources:
        url = src["url"]
        source_name = src.get("source", "")
        keyword_filter = src.get("keyword_filter")

        entries = fetch_feed(url)
        log(f"  - {source_name or url}: 获取到 {len(entries)} 条")

        for entry in entries:
            title = strip_html(entry.get("title", "")).strip()
            if not title:
                continue
            link = entry.get("link", "").strip()
            if not link:
                continue
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = truncate(strip_html(summary_raw), 120)

            if not matches_keyword_filter(title, summary, keyword_filter):
                continue

            publish_time = parse_entry_time(entry)
            tags = [source_name] if source_name else []

            items.append({
                "id": make_id(category, link),
                "title": title,
                "summary": summary or "（原文暂无摘要，点击查看详情）",
                "source_url": link,
                "publish_time": publish_time,
                "category": category,
                "is_important": is_important(title),
                "tags": tags,
            })

    return items


def fetch_all_news():
    all_items = []
    for category, sources in FEED_SOURCES.items():
        all_items.extend(fetch_category_news(category, sources))
    return all_items


# ==============================================================================
# 抓取赛事预告（NBA: balldontlie / 足球: football-data.org）
# ==============================================================================

def fetch_nba_previews():
    api_key = os.environ.get("BALLDONTLIE_API_KEY")
    if not api_key:
        log("[提示] 未配置 BALLDONTLIE_API_KEY，跳过 NBA 赛程抓取（不影响其他数据）")
        return []

    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=MATCH_PREVIEW_WINDOW_DAYS)).isoformat()

    try:
        resp = requests.get(
            "https://api.balldontlie.io/v1/games",
            headers={"Authorization": api_key, **REQUEST_HEADERS},
            params={"start_date": date_from, "end_date": date_to, "per_page": 100},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"[警告] NBA 赛程请求失败：{e}")
        return []

    previews = []
    for game in data.get("data", []):
        try:
            home = game["home_team"]["full_name"]
            away = game["visitor_team"]["full_name"]
            game_date = game.get("date") or game.get("datetime")
            if not game_date:
                continue
            # balldontlie 的 date 可能只有日期没有具体时间，统一按 UTC 处理
            dt = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            previews.append({
                "id": make_id("mp-nba", str(game["id"])),
                "sport": "basketball",
                "competition": "NBA常规赛",
                "home_team": home,
                "away_team": away,
                "match_time": dt.isoformat(),
                "source_url": f"https://www.nba.com/game/{game['id']}",
                "tags": [],
            })
        except Exception as e:
            log(f"  [警告] 跳过一条异常的 NBA 赛程数据：{e}")
            continue

    log(f"NBA 赛程：获取到 {len(previews)} 场")
    return previews


def fetch_football_previews():
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        log("[提示] 未配置 FOOTBALL_DATA_API_KEY，跳过足球赛程抓取（不影响其他数据）")
        return []

    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=MATCH_PREVIEW_WINDOW_DAYS)).isoformat()

    previews = []
    for code, cn_name in FOOTBALL_DATA_COMPETITIONS.items():
        try:
            resp = requests.get(
                f"https://api.football-data.org/v4/competitions/{code}/matches",
                headers={"X-Auth-Token": api_key, **REQUEST_HEADERS},
                params={"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log(f"  [警告] {cn_name}({code}) 赛程请求失败：{e}")
            continue

        for match in data.get("matches", []):
            try:
                previews.append({
                    "id": make_id("mp-fb", str(match["id"])),
                    "sport": "football",
                    "competition": cn_name,
                    "home_team": match["homeTeam"]["name"],
                    "away_team": match["awayTeam"]["name"],
                    "match_time": match["utcDate"],
                    "source_url": f"https://www.football-data.org/",
                    "tags": [],
                })
            except Exception as e:
                log(f"  [警告] 跳过一条异常的 {cn_name} 赛程数据：{e}")
                continue

    log(f"足球赛程：获取到 {len(previews)} 场")
    return previews


def fetch_all_match_previews():
    previews = fetch_nba_previews() + fetch_football_previews()
    # 只保留未来的比赛，按开赛时间升序排列
    now = datetime.now(timezone.utc)
    previews = [p for p in previews if datetime.fromisoformat(p["match_time"]) >= now]
    previews.sort(key=lambda p: p["match_time"])
    return previews[:MAX_MATCH_PREVIEWS]


# ==============================================================================
# 合并、去重、裁剪、写回 data.json
# ==============================================================================

def load_existing_data():
    if not os.path.exists(DATA_FILE):
        return {"news": [], "matchPreviews": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("news", [])
            data.setdefault("matchPreviews", [])
            return data
    except Exception as e:
        log(f"[警告] 读取已有 data.json 失败，将视为空数据重新开始：{e}")
        return {"news": [], "matchPreviews": []}


def merge_news(existing_news, new_news):
    """按 id 去重合并，同一 id 以新抓取的版本为准（标题/摘要若有更新会覆盖）；
    然后按分类分别裁剪到 MAX_ITEMS_PER_CATEGORY 条，避免文件无限增长。"""
    by_id = {item["id"]: item for item in existing_news}
    for item in new_news:
        by_id[item["id"]] = item

    merged = list(by_id.values())

    by_category = {}
    for item in merged:
        by_category.setdefault(item["category"], []).append(item)

    result = []
    for category, items in by_category.items():
        items.sort(key=lambda x: x["publish_time"], reverse=True)
        result.extend(items[:MAX_ITEMS_PER_CATEGORY])

    return result


def main():
    log("=" * 60)
    log(f"开始抓取，时间：{datetime.now(timezone.utc).isoformat()}")

    existing = load_existing_data()

    new_news = fetch_all_news()
    merged_news = merge_news(existing["news"], new_news)

    match_previews = fetch_all_match_previews()
    # 赛事预告如果本次抓取为空（比如两个 API Key 都没配），保留旧数据里仍未过期的部分，
    # 而不是直接清空，避免"没配 Key 就整个板块消失"
    if not match_previews:
        now = datetime.now(timezone.utc)
        match_previews = [
            p for p in existing.get("matchPreviews", [])
            if datetime.fromisoformat(p["match_time"]) >= now
        ]
        match_previews.sort(key=lambda p: p["match_time"])

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "news": merged_news,
        "matchPreviews": match_previews,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"完成：news={len(merged_news)} 条，matchPreviews={len(match_previews)} 场")
    log(f"已写入 {DATA_FILE}")
    log("=" * 60)


if __name__ == "__main__":
    main()

