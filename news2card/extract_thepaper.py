"""澎湃新闻（thepaper.cn）文章详情抽取器 —— 硬编码，零 LLM。

数据来源（Next.js SSR，页面内嵌 JSON）：
- <script id="__NEXT_DATA__"> → props.pageProps.detailData.contentDetail
- 字段: name(标题) / pubTime(本地时间) / author(署名) / summary(摘要) /
  nodeInfo.name(频道) / pic(头图) / content(正文 HTML，p 与 img 按文档序) / images[](图注映射) / videoDTOList(视频)
"""

import json
import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup, Tag

from .models import Article, BodyBlock, BodyImage

SOURCE_NAME = "澎湃新闻"
SOURCE_ID = "thepaper"
SOURCE_HOME = "https://www.thepaper.cn"

_AUTHOR_PREFIX = re.compile(r"^澎湃新闻(?:资深)?(?:记者|编辑|评论员|主播)?\s*")


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


def _extract_cd(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return d["props"]["pageProps"]["detailData"]["contentDetail"]
    except (ValueError, KeyError, TypeError):
        return {}


def _split_summary(text: str, max_items: int = 3) -> List[str]:
    """澎湃摘要为单段，按句号切成条目供卡片使用。"""
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？])", text) if p.strip()]
    return parts[:max_items] or [text.strip()]


def _build_caption_map(cd: dict) -> Dict[str, str]:
    """images[] 数组: imgId → 图注(name / description)。"""
    out = {}
    for img in cd.get("images") or []:
        cap = _clean_text(img.get("name") or "") or _clean_text(img.get("description") or "")
        if cap:
            out[str(img.get("imgId"))] = cap
    return out


def _extract_blocks(content_html: str, captions: Dict[str, str], has_video: bool) -> List[BodyBlock]:
    soup = BeautifulSoup(content_html or "", "lxml")
    root = soup.body or soup
    blocks: List[BodyBlock] = []
    note_added = False

    for tag in root.find_all(["p", "img", "video", "iframe"]):
        if tag.name in ("video", "iframe"):
            if not note_added:
                blocks.append(BodyBlock(type="note", text="【本文含视频/交互内容，详见原文】"))
                note_added = True
            continue
        if tag.name == "img":
            src = tag.get("data-src") or tag.get("src") or ""
            if not src or "thepaper.cn" not in src:
                continue
            cap = ""
            img_id = tag.get("data-imageid") or tag.get("data-id")
            if img_id:
                cap = captions.get(str(img_id), "")
            blocks.append(BodyBlock(type="image", image=BodyImage(url=src, caption=cap)))
            continue
        # <p>
        text = _clean_text(tag.get_text(" ", strip=True))
        if text:
            blocks.append(BodyBlock(type="paragraph", text=text))

    if has_video and not note_added:
        blocks.append(BodyBlock(type="note", text="【本文含视频，详见原文】"))
    return blocks


def extract(url: str) -> Article:
    """抽取澎湃新闻文章。 Raises ExtractError。"""
    if "thepaper.cn" not in url:
        raise ExtractError(f"仅支持澎湃新闻链接: {url}")

    session = _new_session()
    try:
        resp = session.get(url, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ExtractError(f"下载页面失败: {e}")

    resp.encoding = "utf-8"
    cd = _extract_cd(resp.text)
    if not cd:
        raise ExtractError("未找到 __NEXT_DATA__ 数据，页面结构可能已变更")

    title = _clean_text(cd.get("name") or "")
    content_html = cd.get("content") or ""
    if not title or not content_html:
        raise ExtractError("非标准文章页（可能为图集/视频/专题），暂不支持")

    display_time = _clean_text(str(cd.get("pubTime") or cd.get("publishTime") or ""))

    author = _clean_text(cd.get("author") or "")
    author = _AUTHOR_PREFIX.sub("", author).strip() or author

    section = ""
    node = cd.get("nodeInfo") or {}
    if isinstance(node, dict):
        section = _clean_text(node.get("name") or "")

    blocks = _extract_blocks(content_html, _build_caption_map(cd),
                             bool(cd.get("videoDTOList")))
    paragraphs = [b.text for b in blocks if b.type == "paragraph"]
    lead = paragraphs[0] if paragraphs else ""

    return Article(
        url=url.split("?")[0],
        source=cd.get("source") or SOURCE_NAME,
        source_id=SOURCE_ID,
        title=title,
        author=author,
        section=section,
        published_iso=display_time,
        published_display=display_time,
        summary=_split_summary(_clean_text(cd.get("summary") or "")),
        lead=lead,
        blocks=blocks,
        hero_url=cd.get("pic") or "",
        hero_caption="",
        word_count=sum(len(p) for p in paragraphs),
    )
