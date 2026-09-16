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
  3. 把英文 / 日文等非中文来源的标题、摘要，以及赛事预告里的球队名，
     自动翻译成简体中文（优先用 deep-translator 免费调用 Google 翻译，不需要 API Key；
     Google 被限流时自动切换到 MyMemory 这个独立的免费翻译接口兜底）；
     已经是中文的内容会自动跳过翻译，避免多此一举；两个翻译接口都失败/都被限流时保留原文，
     不影响抓取流程（下次抓取会再次尝试翻译）；
     球队名、日本球员姓名等专有名词优先查内置的官方/通行中文译名词典（TEAM_NAME_ZH /
     _JP_KANJI_TO_ZH），保证是国内体育媒体通行的准确译名，而不是机器翻译的直译结果；
     词典没有收录的球队再退回机器翻译兜底；
  4. 从 balldontlie（NBA）和 football-data.org（足球）抓取近期赛程，
     生成"重点赛事预告"数据；
  5. 与仓库里已有的 data.json 合并去重，裁剪到合理条数，写回 data.json。

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
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from deep_translator import GoogleTranslator, MyMemoryTranslator

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
# 自动翻译为简体中文
# ==============================================================================
# 目标：网站上展示的所有文字内容（资讯标题/摘要、赛事预告里的球队名）都是中文，
# 不需要你手动翻译或手动维护。翻译用 deep-translator 库免费调用 Google 翻译网页版接口，
# 不需要注册、不需要 API Key；翻译失败（网络问题/被限流等）时会保留原文，不会导致整个
# 抓取任务失败——个别条目暂时是原文属于正常现象，下次抓取到同一条时会再次尝试翻译。

# 日文假名（平假名 + 片假名）的 Unicode 范围：文本里出现假名，基本可判定为日文（即使夹杂汉字）
_KANA_RE = re.compile(u"[぀-ヿ]")
# 中日韩统一表意文字（汉字）的 Unicode 范围
_HAN_RE = re.compile(u"[一-鿿]")

# 同一次运行内，相同文本只翻译一次并复用结果（球队名等重复率很高，能大幅减少请求数）
_TRANSLATION_CACHE = {}

# GitHub Actions 的 runner 出口 IP 是共享池，经常被 Google 翻译的免费接口判定为"请求过多"。
# 一旦在本次运行中检测到限流，就不再继续重试 Google（避免浪费大量指数退避的等待时间），
# 直接切换到 MyMemory 这个限流策略完全独立的免费接口兜底；两个接口都不可用时才保留原文。
_GOOGLE_BLOCKED = False
_MYMEMORY_BLOCKED = False


def _looks_like_rate_limit(err):
    """粗略判断一个翻译异常是否属于"请求过多/限流"类型，用于决定是否切换翻译接口。"""
    msg = str(err).lower()
    return (
        "too many requests" in msg
        or "429" in msg
        or "quota" in msg
        or "limit" in msg
    )

# 日文汉字与中文简体字形不同的常用字对照（主要覆盖 JAPAN_PLAYER_KEYWORDS 里出现的球员姓名用字）。
# Google 翻译经常会原样保留日文汉字写法（比如"鎌田"不会自动转成"镰田"），
# 这里做一次字形规范化，让球员姓名显示为国内媒体通行的简体字写法。
_JP_KANJI_TO_ZH = {
    "薫": "薰", "冨": "富", "実": "实", "鈴": "铃", "艶": "艳", "勢": "势",
    "輝": "辉", "樹": "树", "瀬": "濑", "歩": "步", "夢": "梦", "綺": "绮",
    "颯": "飒", "橋": "桥", "岡": "冈", "遠": "远", "鎌": "镰", "倉": "仓",
}


def normalize_jp_kanji_to_zh(text):
    """把文本里残留的日文汉字写法替换成对应的中文简体字形，用于修正球员姓名等专有名词。"""
    if not text:
        return text
    for jp, zh in _JP_KANJI_TO_ZH.items():
        if jp in text:
            text = text.replace(jp, zh)
    return text


def needs_translation(text):
    """粗略判断一段文本是否需要翻译成中文：
    - 含假名 -> 判定为日文，需要翻译；
    - 不含假名但含汉字 -> 判定为已经是中文，跳过；
    - 既不含假名也不含汉字（比如纯英文/西欧语言）-> 需要翻译。"""
    if not text:
        return False
    if _KANA_RE.search(text):
        return True
    if _HAN_RE.search(text):
        return False
    return True


# MyMemory 免费接口单次请求的文本长度有上限（官方约 500 字符），超过会报错。
# 比这个阈值留一点余量，超长文本按句子边界切成多段分别翻译再拼接，
# 而不是整段跳过——这样长摘要也能被翻译，而不是因为长度超限就直接保留原文。
_MYMEMORY_CHUNK_LIMIT = 450
# 用于按句子边界切分的标点（中/日/英文常见结尾标点）
_SENTENCE_BOUNDARY_RE = re.compile(u"([。！？；\\.\\!\\?;]+)")


def _split_into_chunks(text, limit):
    """把长文本按句子边界切成若干段，每段不超过 limit 个字符；
    单个句子本身超过 limit 时，直接按字符硬切，保证一定能切完。"""
    parts = _SENTENCE_BOUNDARY_RE.split(text)
    # re.split 加了捕获组，结果里标点和正文是分开的元素，两两拼回完整"句子"
    sentences = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        if i + 1 < len(parts):
            sentence += parts[i + 1]
        if sentence:
            sentences.append(sentence)

    chunks = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(sentence), limit):
                chunks.append(sentence[i:i + limit])
            continue
        if len(current) + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks or [text]


def _guess_source_lang(text):
    """MyMemory 接口不支持 source="auto" 自动检测语言（传 auto 会直接返回一句报错文本，
    而不是抛异常，如果不处理会把这句报错当成"翻译结果"存下来），必须显式指定源语言；
    而且它要求的是带地区后缀的语言代码（比如 "en-GB"、"ja-JP"），裸的 "en"/"ja"
    会被它自己的语言校验拒绝，报"No support for the provided language"。
    这里按本项目实际用到的信源简单判断：含日文假名判定为日语，否则按英语处理
    （FEED_SOURCES 里非中文源除了日文（ゲキサカ）以外都是英文站点）。"""
    return "ja-JP" if _KANA_RE.search(text) else "en-GB"


def _is_mymemory_error_text(translated):
    """MyMemory 出错时会把错误说明当成"译文"返回（比如源语言不支持、超出长度限制等），
    而不是抛异常。这里识别几种常见的错误提示，识别到就当作翻译失败处理，避免把英文的
    错误说明当成中文译文存进 data.json。"""
    if not translated:
        return True
    lowered = translated.lower()
    error_markers = (
        "invalid source language",
        "invalid target language",
        "is not yet supported",
        "must translate",
        "query length limit exceeded",
        "language pair not supported",
        "no support for the provided language",
    )
    return any(marker in lowered for marker in error_markers)


def _mymemory_translate(text):
    """调用 MyMemory 接口翻译文本；超过单次请求长度上限时自动分段翻译再拼接。
    MyMemory 返回错误提示文本（而不是抛异常）时，主动转换成异常，交给上层的重试/
    切换兜底逻辑处理，避免把错误提示当成译文存下来。"""
    source_lang = _guess_source_lang(text)

    def _translate_one(chunk):
        translated = MyMemoryTranslator(source=source_lang, target="zh-CN").translate(chunk)
        translated = translated.strip() if translated else chunk
        if _is_mymemory_error_text(translated):
            raise ValueError(f"MyMemory 返回了错误提示而不是译文：{translated[:80]}")
        return translated

    if len(text) <= _MYMEMORY_CHUNK_LIMIT:
        return _translate_one(text)

    results = []
    for chunk in _split_into_chunks(text, _MYMEMORY_CHUNK_LIMIT):
        results.append(_translate_one(chunk))
        time.sleep(0.5)  # 分段请求之间也稍作停顿，降低触发限流的概率
    return "".join(results)


def translate_to_chinese(text):
    """把非中文文本翻译成简体中文；已是中文或空文本直接原样返回。
    Google 翻译免费接口在 GitHub Actions 的共享出口 IP 上经常被限流，因此这里做了多重保护：
    1) 同一次运行内对相同文本做缓存，避免重复请求；
    2) 每次成功请求之间固定停顿，主动放慢速度；
    3) 遇到限流/网络错误时按指数退避重试，但只重试很少几次——一旦确认是"请求过多"这类限流错误，
       就不再对 Google 继续重试（重试也没用，只会白白浪费时间），改用 MyMemory 这个限流策略完全
       独立的免费翻译接口兜底；
    4) 两个接口都失败/都被限流时，保留原文并记录警告，不影响抓取流程（下次抓取到同一条时会再次
       尝试翻译，届时限流很可能已经解除）。"""
    global _GOOGLE_BLOCKED, _MYMEMORY_BLOCKED

    if not text or not needs_translation(text):
        return text
    if text in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[text]

    last_err = None

    if not _GOOGLE_BLOCKED:
        delay = 2.0
        for attempt in range(2):
            try:
                translated = GoogleTranslator(source="auto", target="zh-CN").translate(text)
                result = translated.strip() if translated else text
                result = normalize_jp_kanji_to_zh(result)
                _TRANSLATION_CACHE[text] = result
                time.sleep(1.2)  # 主动限速，降低被翻译接口限流的概率
                return result
            except Exception as e:
                last_err = e
                if _looks_like_rate_limit(e):
                    _GOOGLE_BLOCKED = True
                    log("    [提示] Google 翻译接口本次运行已被限流，改用 MyMemory 接口兜底")
                    break
                time.sleep(delay)
                delay *= 2

    if not _MYMEMORY_BLOCKED:
        delay = 2.0
        for attempt in range(2):
            try:
                result = _mymemory_translate(text)
                result = normalize_jp_kanji_to_zh(result)
                _TRANSLATION_CACHE[text] = result
                time.sleep(1.0)
                return result
            except Exception as e:
                last_err = e
                if _looks_like_rate_limit(e):
                    _MYMEMORY_BLOCKED = True
                    log("    [提示] MyMemory 翻译接口本次运行也已被限流，后续内容将保留原文")
                    break
                time.sleep(delay)
                delay *= 2

    log(f"    [警告] 翻译失败，保留原文：{last_err}")
    _TRANSLATION_CACHE[text] = text
    return text


# ==============================================================================
# 球队名词典：赛事预告里的球队名优先用国内体育媒体通行的官方/习惯中文译名，
# 不经过机器翻译，保证准确；词典没有收录的球队再退回 translate_to_chinese 兜底。
# ==============================================================================

# 俱乐部官方全名里常见的后缀/编号词，查词典前先去掉，比如：
# "Tottenham Hotspur FC" -> "tottenham hotspur"，"1. FC Köln" -> "koln"
# 注意："club"/"sc" 不放进来，因为它们在个别球队的正式队名里是有实际含义的词
# （Athletic Club、Club Brugge、SC Freiburg），全局剥离反而会导致查不到词典，
# 这些个例改成直接在词典里保留完整写法作为 key。
_CLUB_NOISE_WORDS = {
    "fc", "cf", "afc", "sad", "sa", "bc", "cfc", "ac", "acf", "hsc", "sco",
    "ssc", "ss", "us", "usc", "ogc", "cd", "ca", "ud", "sl", "fk", "fsv",
    "calcio", "the", "&", "and",
    "1907", "1909", "1910", "1913", "1899", "1901", "05", "04", "29",
}


def _ascii_fold(s):
    """把带重音符号的字母转成对应的基础拉丁字母（比如 "ö" -> "o"），
    避免因为重音符号写法不同导致词典查不到（比如 "München" / "Munchen"）。"""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _normalize_team_name(name):
    """把球队官方全名标准化成词典查找用的 key：去重音 -> 连字符/斜杠转空格
    -> 去掉开头编号 -> 去掉常见俱乐部后缀词 -> 转小写。"""
    n = _ascii_fold(name or "").strip()
    n = re.sub(r"^\d+\.\s*", "", n)  # "1. FC Köln" -> "FC Koln"
    n = n.replace("-", " ").replace("/", " ")  # "Paris Saint-Germain" / "Bodo/Glimt"
    tokens = [t.strip(".").lower() for t in n.split()]
    tokens = [t for t in tokens if t and t not in _CLUB_NOISE_WORDS]
    return " ".join(tokens)


# key 是经过 _normalize_team_name() 处理后的标准化球队名，value 是国内体育媒体通行译名
TEAM_NAME_ZH = {
    # ---- NBA（30 支球队官方通行译名）----
    "atlanta hawks": "亚特兰大老鹰", "boston celtics": "波士顿凯尔特人",
    "brooklyn nets": "布鲁克林篮网", "charlotte hornets": "夏洛特黄蜂",
    "chicago bulls": "芝加哥公牛", "cleveland cavaliers": "克利夫兰骑士",
    "dallas mavericks": "达拉斯独行侠", "denver nuggets": "丹佛掘金",
    "detroit pistons": "底特律活塞", "golden state warriors": "金州勇士",
    "houston rockets": "休斯顿火箭", "indiana pacers": "印第安纳步行者",
    "la clippers": "洛杉矶快船", "los angeles clippers": "洛杉矶快船",
    "los angeles lakers": "洛杉矶湖人", "memphis grizzlies": "孟菲斯灰熊",
    "miami heat": "迈阿密热火", "milwaukee bucks": "密尔沃基雄鹿",
    "minnesota timberwolves": "明尼苏达森林狼", "new orleans pelicans": "新奥尔良鹈鹕",
    "new york knicks": "纽约尼克斯", "oklahoma city thunder": "俄克拉荷马城雷霆",
    "orlando magic": "奥兰多魔术", "philadelphia 76ers": "费城76人",
    "phoenix suns": "菲尼克斯太阳", "portland trail blazers": "波特兰开拓者",
    "sacramento kings": "萨克拉门托国王", "san antonio spurs": "圣安东尼奥马刺",
    "toronto raptors": "多伦多猛龙", "utah jazz": "犹他爵士",
    "washington wizards": "华盛顿奇才",

    # ---- 英超 ----
    "arsenal": "阿森纳", "aston villa": "阿斯顿维拉", "bournemouth": "伯恩茅斯",
    "brentford": "布伦特福德", "brighton hove albion": "布莱顿",
    "brighton": "布莱顿", "burnley": "伯恩利", "chelsea": "切尔西",
    "crystal palace": "水晶宫", "everton": "埃弗顿", "fulham": "富勒姆",
    "leeds united": "利兹联", "liverpool": "利物浦", "manchester city": "曼城",
    "manchester united": "曼联", "newcastle united": "纽卡斯尔联", "newcastle": "纽卡斯尔联",
    "nottingham forest": "诺丁汉森林", "sunderland": "桑德兰",
    "tottenham hotspur": "托特纳姆热刺", "tottenham": "托特纳姆热刺",
    "west ham united": "西汉姆联", "west ham": "西汉姆联",
    "wolverhampton wanderers": "狼队", "wolves": "狼队",

    # ---- 西甲 ----
    "real madrid": "皇家马德里", "barcelona": "巴塞罗那",
    "atletico madrid": "马德里竞技", "atletico de madrid": "马德里竞技",
    "club atletico de madrid": "马德里竞技",
    "real sociedad": "皇家社会", "real sociedad de futbol": "皇家社会",
    "real betis": "皇家贝蒂斯", "real betis balompie": "皇家贝蒂斯", "sevilla": "塞维利亚",
    "villarreal": "比利亚雷亚尔", "athletic bilbao": "毕尔巴鄂竞技", "athletic club": "毕尔巴鄂竞技",
    "athletic club de bilbao": "毕尔巴鄂竞技",
    "valencia": "瓦伦西亚", "celta vigo": "塞尔塔维戈", "rc celta": "塞尔塔维戈",
    "girona": "赫罗纳", "getafe": "赫塔费", "osasuna": "奥萨苏纳",
    "mallorca": "马略卡", "rcd mallorca": "马略卡",
    "rayo vallecano": "巴列卡诺", "rayo vallecano de madrid": "巴列卡诺",
    "espanyol": "西班牙人", "rcd espanyol de barcelona": "西班牙人", "rcd espanyol": "西班牙人",
    "alaves": "阿拉维斯", "deportivo alaves": "阿拉维斯", "levante": "莱万特",
    "real oviedo": "奥维耶多", "elche": "埃尔切",

    # ---- 德甲 ----
    "bayern munchen": "拜仁慕尼黑", "bayern munich": "拜仁慕尼黑",
    "borussia dortmund": "多特蒙德", "rb leipzig": "莱比锡红牛",
    "bayer leverkusen": "勒沃库森", "bayer 04 leverkusen": "勒沃库森",
    "eintracht frankfurt": "法兰克福", "vfb stuttgart": "斯图加特",
    "borussia monchengladbach": "门兴格拉德巴赫", "vfl wolfsburg": "沃尔夫斯堡",
    "sc freiburg": "弗赖堡", "union berlin": "柏林联合",
    "werder bremen": "云达不来梅", "sv werder bremen": "云达不来梅",
    "tsg hoffenheim": "霍芬海姆", "hoffenheim": "霍芬海姆",
    "mainz 05": "美因茨05", "mainz": "美因茨05",
    "fc augsburg": "奥格斯堡", "augsburg": "奥格斯堡",
    "koln": "科隆", "fc st pauli": "圣保利", "st pauli": "圣保利",
    "hamburger sv": "汉堡", "holstein kiel": "基尔",

    # ---- 意甲 ----
    "juventus": "尤文图斯", "milan": "AC米兰", "internazionale": "国际米兰",
    "internazionale milano": "国际米兰",
    "inter": "国际米兰", "roma": "罗马", "as roma": "罗马", "napoli": "那不勒斯",
    "lazio": "拉齐奥", "atalanta": "亚特兰大", "fiorentina": "佛罗伦萨",
    "torino": "都灵", "bologna": "博洛尼亚", "udinese": "乌迪内斯",
    "sassuolo": "萨索洛", "genoa": "热那亚", "cagliari": "卡利亚里",
    "hellas verona": "维罗纳", "verona": "维罗纳", "parma": "帕尔马",
    "como": "科莫", "como 1907": "科莫", "lecce": "莱切", "empoli": "恩波利",
    "venezia": "威尼斯", "pisa": "比萨", "cremonese": "克雷莫纳",

    # ---- 法甲 ----
    "paris saint germain": "巴黎圣日耳曼", "marseille": "马赛",
    "olympique de marseille": "马赛", "lyon": "里昂", "olympique lyonnais": "里昂",
    "monaco": "摩纳哥", "as monaco": "摩纳哥", "lille": "里尔", "losc lille": "里尔",
    "nice": "尼斯", "rennes": "雷恩", "stade rennais": "雷恩",
    "lens": "朗斯", "rc lens": "朗斯", "strasbourg": "斯特拉斯堡",
    "rc strasbourg alsace": "斯特拉斯堡",
    "toulouse": "图卢兹", "nantes": "南特", "montpellier": "蒙彼利埃",
    "reims": "兰斯", "stade de reims": "兰斯", "le havre": "勒阿弗尔",
    "auxerre": "欧塞尔", "aj auxerre": "欧塞尔", "angers": "昂热",
    "brest": "布雷斯特", "stade brestois": "布雷斯特", "metz": "梅斯",
    "paris": "巴黎FC",

    # ---- 欧冠常客（英德意法西以外）----
    "ajax": "阿贾克斯", "benfica": "本菲卡", "porto": "波尔图",
    "sporting cp": "葡萄牙体育", "sporting lisbon": "葡萄牙体育",
    "celtic": "凯尔特人", "rangers": "流浪者", "psv eindhoven": "埃因霍温",
    "psv": "埃因霍温", "feyenoord": "费耶诺德", "shakhtar donetsk": "顿涅茨克矿工",
    "club brugge": "布鲁日", "galatasaray": "加拉塔萨雷", "fenerbahce": "费内巴切",
    "olympiacos": "奥林匹亚科斯", "slavia praha": "布拉格斯拉维亚",
    "bodo glimt": "博德闪耀", "qarabag": "卡拉巴克", "fc copenhagen": "哥本哈根",
    "copenhagen": "哥本哈根", "union saint gilloise": "圣吉罗斯联",
    "kairat almaty": "凯拉特", "pafos": "帕福斯",
}


def translate_team_name(name):
    """球队名优先查 TEAM_NAME_ZH 词典（国内体育媒体通行译名），命中直接返回，
    不消耗翻译请求额度；词典没有收录的球队，退回 translate_to_chinese 机器翻译兜底。"""
    if not name:
        return name
    key = _normalize_team_name(name)
    if key in TEAM_NAME_ZH:
        return TEAM_NAME_ZH[key]
    return translate_to_chinese(name)


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
            title_raw = strip_html(entry.get("title", "")).strip()
            if not title_raw:
                continue
            link = entry.get("link", "").strip()
            if not link:
                continue
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary_raw = strip_html(summary_raw)

            # 关键词过滤（比如日本旅欧球员）用原文匹配，翻译前后语义一致，不影响筛选结果
            if not matches_keyword_filter(title_raw, summary_raw, keyword_filter):
                continue

            # 翻译成中文：已经是中文的来源（量子位/36氪）会被 needs_translation 自动跳过
            title = translate_to_chinese(title_raw)
            summary = truncate(translate_to_chinese(summary_raw), 120)

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
    previews = previews[:MAX_MATCH_PREVIEWS]

    # 只翻译最终会展示的场次（而不是翻译抓到的全部原始数据），减少不必要的翻译请求。
    # 赛事的 competition 字段已经在 FOOTBALL_DATA_COMPETITIONS / fetch_nba_previews 里
    # 用中文命名了，这里只需要翻译球队名。
    for p in previews:
        p["home_team"] = translate_team_name(p["home_team"])
        p["away_team"] = translate_team_name(p["away_team"])

    return previews


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
