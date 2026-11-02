"""联合早报（zaobao.com）文章详情抽取器 —— 硬编码选择器，零 LLM。

数据来源（均为服务端直出，无需 JS 渲染）：
- JSON-LD NewsArticle: headline / datePublished / dateModified / articleSection / image.url / wordCount
- DOM: h1 标题、.byline_area 署名、<zb-ai-summary> AI 摘要（属性内嵌）
- 正文: div.order-8 > article 下的 <p> 与 <img>（document order）
干扰过滤: .further-link 相关阅读、read-on-app 推广图、订阅推广、iframe 交互图表（跳过并留占位）
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
    m = re.search(r"zaobao\.com/(?:[a-z]+/)?([a-z]+)/", url)
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


def _extract_hero(soup: BeautifulSoup) -> Optional[BodyImage]:
    banner = soup.find(attrs={"data-testid": "article-banner"})
    scope = banner or soup
    for img in scope.find_all("img"):
        if _is_body_photo(img):
            caption = _clean_text(img.get("data-title") or img.get("title") or "")
            return BodyImage(url=img["src"], caption=caption)
    return None


def _extract_blocks(soup: BeautifulSoup) -> List[BodyBlock]:
    container = soup.find("div", class_="order-8")
    article_el = container.find("article") if container else None
    if article_el is None:
        return []

    blocks: List[BodyBlock] = []
    note_added = False
    seen_img: set = set()

    for tag in article_el.find_all(["p", "img", "iframe"]):
        if tag.find_parent(class_="further-link"):
            continue
        if tag.name == "iframe":
            if not note_added:
                blocks.append(BodyBlock(type="note", text="【此处含交互图表，详见原文】"))
                note_added = True
            continue
        if tag.name == "img":
            if not _is_body_photo(tag):
                continue
            src = tag["src"]
            if src in seen_img:
                continue
            seen_img.add(src)
            caption = _clean_text(tag.get("data-title") or tag.get("title") or "")
            blocks.append(BodyBlock(type="image", image=BodyImage(url=src, caption=caption)))
            continue
        # <p>
        if tag.find("img"):          # 含图的段落交给 img 分支处理
            continue
        if _is_promo_p(tag):
            continue
        text = _clean_text(tag.get_text(" ", strip=True))
        if not text:
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
    soup = BeautifulSoup(resp.text, "lxml")
    ld = _extract_jsonld(soup)

    title = _clean_text(ld.get("headline") or "")
    if not title:
        h1 = soup.find("h1")
        title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        raise ExtractError("未找到标题，页面结构可能已变更")

    # 时间: 优先页面标注的"更新 / ..."（SGT），兜底 JSON-LD 发布时间
    dt = _parse_iso(ld.get("datePublished") or "")
    display_time = _extract_update_display(soup) or _display_time(dt)

    section = _extract_section(soup, ld, url)

    hero = _extract_hero(soup)
    blocks = _extract_blocks(soup)

    lead = ""
    paragraphs = [b.text for b in blocks if b.type == "paragraph"]
    if paragraphs:
        lead = paragraphs[0]

    return Article(
        url=url.split("?")[0],
        source=SOURCE_NAME,
        source_id="zaobao",
        title=title,
        author=_extract_author(soup),
        section=_clean_text(str(section)),
        published_iso=(dt.isoformat() if dt else (ld.get("datePublished") or "")),
        published_display=display_time,
        summary=_extract_summary(soup),
        lead=lead,
        blocks=blocks,
        hero_url=hero.url if hero else "",
        hero_caption=hero.caption if hero else "",
        word_count=int(ld.get("wordCount") or 0),
    )
