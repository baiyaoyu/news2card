"""联合早报（zaobao.com）文章详情抽取器 —— 硬编码选择器，零 LLM。

2026-11 起早报全站改版（旧 URL 301 至新模板），当前结构：
- JSON-LD NewsArticle: headline / datePublished / image（新 CDN cassette.sphdigital.com.sg）
- 正文: <article class="max-w-full"> 内 p/img 按文档序；无 byline/AI 摘要/面包屑
- 频道: URL 路径映射兜底（/news/china → 中国）；作者多为空（电讯稿无署名）
- 干扰过滤: base64 占位图、/assets/ 图标、推广文案
"""

import json
import re
from datetime import datetime
from typing import List, Optional

import requests
from bs4 import BeautifulSoup, Tag

from .models import Article, BodyBlock, BodyImage

SOURCE_NAME = "联合早报"
SOURCE_HOME = "https://www.zaobao.com"

# 正文推广文本过滤关键词
_PROMO_KEYWORDS = ("下载联合早报", "点击订阅", "立即订阅", "输入电邮", "newsletter", "newsletter", "订阅")


class ExtractError(Exception):
    """抽取失败（页面结构变更 / 非文章页 / 网络错误）"""


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SOURCE_HOME + "/",
    })
    return s


def _clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _display_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt:%H:%M}"


def _extract_jsonld(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                return item
    return {}


def _resolve_stream(arr, v, depth=0):
    if isinstance(v, dict) and v and all(k.startswith("_") for k in v):
        out = {}
        for tok, m in v.items():
            # 规则: {"_N": M} → 键 = arr[N], 值 = resolve(arr[M])
            ni = int(tok[1:]) if tok[1:].isdigit() else -1
            if not (0 <= ni < len(arr) and isinstance(m, int) and 0 <= m < len(arr)):
                continue
            k = arr[ni]
            if isinstance(k, str):
                out[k] = _resolve_stream(arr, arr[m], depth + 1)
        return out
    if isinstance(v, list):
        # 列表内的 int 为引用下标（如 author=[168] → arr[168]）
        return [_resolve_stream(arr, arr[x], depth + 1)
                if isinstance(x, int) and 0 <= x < len(arr) else
                (_resolve_stream(arr, x, depth + 1) if isinstance(x, (dict, list)) else x)
                for x in v]
    return v


def _parse_rr_stream(html: str) -> Optional[dict]:
    """解析 React Router 脱水状态流（单 fetch 元组格式）。

    结构: loaderData → section → payload → article（含 teaser/stand_first/
    custom_byline/author/sections 等）。返回主 article 负载；失败返回 None。
    """
    chunks = re.findall(r'streamController\.enqueue\("((?:[^"\\]|\\.)*)"\)', html)
    if not chunks:
        return None
    try:
        raw = "".join(json.loads(f'"{c}"') for c in chunks)
        arr = json.loads(raw)
    except (ValueError, TypeError):
        return None
    try:
        root = _resolve_stream(arr, arr[0])
        return root["loaderData"]["section"]["payload"]["article"]
    except (KeyError, IndexError, TypeError):
        return None


def _extract_summary(soup: BeautifulSoup) -> List[str]:
    el = soup.find("zb-ai-summary")
    if not el:
        return []
    text = el.get_text(" ", strip=True)
    # 站内格式: 以 " - " 分隔的条目，且整体可能以 "- " 开头
    text = re.sub(r"^-\s*", "", text)
    parts = [re.sub(r"^-\s*", "", p).strip() for p in re.split(r"\s+-\s+", text) if p.strip()]
    # 兜底: 整段作为一条
    if len(parts) <= 1 and text:
        parts = [text]
    return parts[:6]


def _extract_section(soup: BeautifulSoup, ld: dict, url: str = "") -> str:
    """频道版面: 优先 DOM 面包屑标签（中文），兜底 URL 路径映射，最后 JSON-LD。"""
    a = soup.find("a", class_="byline_area_small")
    if a:
        t = _clean_text(a.get_text(" ", strip=True))
        if t:
            return t
    m = re.search(r"zaobao\.com(?:\.\w+)?/(?:[a-z]+/)?([a-z]+)/", url)
    if m and m.group(1) in _URL_SECTION:
        return _URL_SECTION[m.group(1)]
    section = ld.get("articleSection") or ""
    if isinstance(section, list):
        section = " / ".join(str(s) for s in section if s)
    return _clean_text(str(section))


def _extract_update_display(soup: BeautifulSoup) -> str:
    """页面标注的"更新 / ..."或"发布 / ..."（SGT），优先于 JSON-LD 发布时间。"""
    node = soup.find(string=re.compile(r"(?:更新|发布)\s*/"))
    if node:
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", node)
        if m:
            y, mo, d, hh, mm = map(int, m.groups())
            return f"{y}年{mo}月{d}日 {hh:02d}:{mm:02d}"
    return ""


def _extract_author(soup: BeautifulSoup) -> str:
    for el in soup.select(".byline_area"):
        t = _clean_text(el.get_text(" ", strip=True))
        # 无署名快讯只有"发布 / ..."或"更新 / ..."时间行
        if not t or "更新" in t or "发布" in t:
            continue
        t = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "", t).strip(" ,|")
        if t:
            return t
    return ""


# URL 路径 → 中文频道（面包屑缺失时的兜底）
_URL_SECTION = {
    "china": "中国", "world": "国际", "singapore": "新加坡",
    "seasia": "东南亚", "opinion": "观点", "finance": "财经",
    "culture": "文化生活", "entertainment": "影音", "sports": "体育",
    "technology": "科技",
}

# RR 流 sections.name 机器键 → 中文
_SECTION_KEY_MAP = {
    "news_china": "中国", "news_world": "国际", "news_singapore": "新加坡",
    "news_southeast_asia": "东南亚", "opinion": "观点", "finance": "财经",
    "culture": "文化生活", "sports": "体育", "technology": "科技",
}


def _payload_author(payload: dict) -> str:
    for a in payload.get("author") or []:
        for p in a.get("profiles") or []:
            if p.get("language") == "zh" and p.get("name"):
                return _clean_text(p["name"])
    return ""


def _payload_section(payload: dict) -> str:
    for s in payload.get("sections") or []:
        n = s.get("name") or ""
        if n in _SECTION_KEY_MAP:
            return _SECTION_KEY_MAP[n]
    return ""


def _is_promo_p(p: Tag) -> bool:
    if p.find_parent(class_="further-link"):
        return True
    text = p.get_text(strip=True)
    if not text:
        return True
    low = text.lower()
    return any(k.lower() in low for k in _PROMO_KEYWORDS)


def _is_body_photo(img: Tag) -> bool:
    src = img.get("src") or ""
    if not src or src.endswith(".svg"):
        return False
    if img.find_parent(class_="further-link"):
        return False
    if "read-on-app" in src or "_astro/" in src:
        return False
    return "/image/" in src


def _is_real_image(src: str) -> bool:
    """新 CDN(cassette.sphdigital.com.sg/image/zaobao/...) 与旧 CDN 都接受；
    排除 base64 占位与 /assets/ 图标。"""
    return src.startswith("http") and ("image/zaobao" in src or "zbstatic" in src)


def _extract_hero(soup: BeautifulSoup, ld: dict, article_tag: Tag) -> Optional[BodyImage]:
    img = ld.get("image")
    url = ""
    if isinstance(img, list) and img and isinstance(img[0], dict):
        url = img[0].get("url") or ""
    elif isinstance(img, dict):
        url = img.get("url") or ""
    if url:
        return BodyImage(url=url, caption="")
    for im in article_tag.find_all("img"):
        src = im.get("src") or ""
        if _is_real_image(src):
            return BodyImage(url=src, caption=_clean_text(im.get("alt") or ""))
    return None


def _is_body_photo_legacy(img: Tag) -> bool:
    src = img.get("src") or ""
    if not src or src.endswith(".svg"):
        return False
    if img.find_parent(class_="further-link"):
        return False
    if "read-on-app" in src or "_astro/" in src:
        return False
    return "/image/" in src


def _extract_blocks_legacy(soup: BeautifulSoup) -> List[BodyBlock]:
    """旧模板（www.zaobao.com）: div.order-8 > article 下的 p/img。"""
    container = soup.find("div", class_="order-8")
    article_el = container.find("article") if container else None
    if article_el is None:
        return []
    blocks: List[BodyBlock] = []
    note_added = False
    seen: set = set()
    for tag in article_el.find_all(["p", "img", "iframe"]):
        if tag.find_parent(class_="further-link"):
            continue
        if tag.name == "iframe":
            if not note_added:
                blocks.append(BodyBlock(type="note", text="【此处含交互图表，详见原文】"))
                note_added = True
            continue
        if tag.name == "img":
            if not _is_body_photo_legacy(tag):
                continue
            src = tag["src"]
            if src in seen:
                continue
            seen.add(src)
            caption = _clean_text(tag.get("data-title") or tag.get("title") or "")
            blocks.append(BodyBlock(type="image", image=BodyImage(url=src, caption=caption)))
            continue
        if tag.find("img"):
            continue
        if _is_promo_p(tag):
            continue
        text = _clean_text(tag.get_text(" ", strip=True))
        if not text:
            continue
        blocks.append(BodyBlock(type="paragraph", text=text))
    return blocks


def _extract_blocks(article_tag: Tag, standfirst: str = "") -> List[BodyBlock]:
    blocks: List[BodyBlock] = []
    if standfirst:
        # 引语框（speakable-summary）是编辑导语，插在正文最前保留到产物
        blocks.append(BodyBlock(type="paragraph", text=standfirst))

    note_added = False
    seen_img: set = set()
    for tag in article_tag.find_all(["p", "img", "iframe"]):
        if tag.find_parent("p") or tag.find_parent(id="speakable-summary"):
            continue
        if tag.name == "iframe":
            if not note_added:
                blocks.append(BodyBlock(type="note", text="【此处含交互图表，详见原文】"))
                note_added = True
            continue
        if tag.name == "img":
            src = tag.get("src") or tag.get("data-src") or ""
            if not _is_real_image(src) or src in seen_img:
                continue
            seen_img.add(src)
            caption = _clean_text(tag.get("alt") or "")
            blocks.append(BodyBlock(type="image", image=BodyImage(url=src, caption=caption)))
            continue
        text = _clean_text(tag.get_text(" ", strip=True))
        if not text or _is_promo_p(tag):
            continue
        if blocks and blocks[-1].type == "paragraph" and blocks[-1].text == text:
            continue
        blocks.append(BodyBlock(type="paragraph", text=text))
    return blocks


def extract(url: str) -> Article:
    """抽取联合早报文章。 Raises ExtractError。"""
    if "zaobao.com" not in url:
        raise ExtractError(f"V1 仅支持联合早报链接: {url}")

    session = _new_session()
    try:
        resp = session.get(url, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ExtractError(f"下载页面失败: {e}")

    resp.encoding = "utf-8"
    html = resp.text
    soup = BeautifulSoup(html, "lxml")
    ld = _extract_jsonld(soup)
    payload = _parse_rr_stream(html) or {}

    title = _clean_text(ld.get("headline") or "")
    if not title:
        h1 = soup.find("h1")
        title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        raise ExtractError("未找到标题，页面结构可能已变更")

    # 时间: 优先页面标注的"更新 / ..."（SGT），兜底 JSON-LD 发布时间
    dt = _parse_iso(ld.get("datePublished") or "")
    display_time = _extract_update_display(soup) or _display_time(dt)

    section = _payload_section(payload) or _extract_section(soup, ld, url)

    article_tag = soup.find("article")
    if article_tag is None:
        raise ExtractError("未找到正文容器 <article>，早报页面结构可能已变更")

    # 双模板: 旧版(www.zaobao.com, div.order-8) / 新版(www.zaobao.com.sg, <article>)
    legacy = soup.find("div", class_="order-8") is not None
    if legacy:
        blocks = _extract_blocks_legacy(soup)
        standfirst = ""
        scope = article_tag
    else:
        standfirst_el = article_tag.select_one("#speakable-summary")
        standfirst = _clean_text(standfirst_el.get_text(" ", strip=True)) if standfirst_el else ""
        blocks = _extract_blocks(article_tag, standfirst)
        scope = article_tag

    hero = _extract_hero(soup, ld, scope)

    lead = ""
    paragraphs = [b.text for b in blocks if b.type == "paragraph"]
    if paragraphs:
        lead = paragraphs[0]

    # 摘要与署名增强: RR 流内 article 负载（主文章，url_path 已校验）
    summary = _extract_summary(soup)
    custom_byline = ""
    if not summary:
        teaser = _clean_text(str(payload.get("teaser") or ""))
        if teaser:
            summary = [teaser]
        sf = payload.get("stand_first")
        if not summary and isinstance(sf, str) and _clean_text(sf):
            summary = [_clean_text(sf)]
    if isinstance(payload.get("custom_byline"), str):
        custom_byline = _clean_text(payload["custom_byline"])
    author = _extract_author(soup) or _payload_author(payload) or custom_byline

    return Article(
        url=url.split("?")[0],
        source=SOURCE_NAME,
        source_id="zaobao",
        title=title,
        author=author,
        section=_clean_text(str(section)),
        published_iso=(dt.isoformat() if dt else (ld.get("datePublished") or "")),
        published_display=display_time,
        summary=summary,
        lead=lead,
        blocks=blocks,
        hero_url=hero.url if hero else "",
        hero_caption=hero.caption if hero else "",
        word_count=int(ld.get("wordCount") or sum(len(p) for p in paragraphs)),
    )
