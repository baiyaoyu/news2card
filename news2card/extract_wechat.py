"""微信公众号文章（mp.weixin.qq.com）抽取器 —— 硬编码，零 LLM。

数据来源（服务端直出）：
- 标题/封面: og:title / og:image
- 公众号名: #js_name（作为来源与署名）；发布时间: 页面 var ct 时间戳(+08:00)
- 正文: #js_content 内 p/img/iframe 按文档序；img 用 data-src 交换（懒加载）
- 反混淆: 移除 visibility:hidden / display:none 元素与 HTML 注释
- 风控识别: 无 js_content 或出现"环境异常/安全验证"文案 → ExtractError
注意: 文章 URL 的 query(poc_token) 必须保留，抽取与产物引用都用原始完整 URL。
"""

import re
from datetime import datetime, timedelta, timezone
from typing import List

import requests
from bs4 import BeautifulSoup, Comment

from .models import Article, BodyBlock, BodyImage

SOURCE_NAME_FALLBACK = "微信公众号"
SOURCE_ID = "wechat"
SOURCE_HOME = "https://mp.weixin.qq.com"

_CST = timezone(timedelta(hours=8))

# 风控页特征（命中即视为被拦截）
_RISK_MARKERS = ("环境异常", "完成安全验证", "操作频繁")

# 文末运营 boilerplate 过滤（短文本且含这些线索才丢弃）
_PROMO_CLUES = ("阅读原文", "点击上方", "设为星标", "长按识别", "二维码关注",
                "微信搜索", "点击关注", "往期回顾", "阅读原文")

# 文末采编署名行
_CREDIT = re.compile(r"^(实习编辑|编辑|责任编辑|校对|美编|排版)[:：]")


class ExtractError(Exception):
    """抽取失败（风控页 / 结构变更 / 网络错误）"""


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


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag else ""


def _publish_display(html: str) -> str:
    m = re.search(r'var ct = "?(\d{9,13})"?', html)
    if not m:
        return ""
    dt = datetime.fromtimestamp(int(m.group(1)), tz=_CST)
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt:%H:%M}"


def _is_promo(text: str) -> bool:
    return len(text) <= 22 and any(k in text for k in _PROMO_CLUES)


def _extract_blocks(content) -> List[BodyBlock]:
    # 反混淆：删除隐藏样式元素与注释节点
    for el in content.find_all(style=True):
        style = el["style"].replace(" ", "").lower()
        if "visibility:hidden" in style or "display:none" in style:
            el.decompose()
    for c in content.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    blocks: List[BodyBlock] = []
    note_added = False
    seen = set()
    for tag in content.find_all(["p", "img", "iframe"]):
        if tag.find_parent("p"):          # 嵌套 p 交给外层
            continue
        if tag.name == "iframe":
            if not note_added:
                blocks.append(BodyBlock(type="note", text="【本文含视频/嵌入内容，详见原文】"))
                note_added = True
            continue
        if tag.name == "img":
            url = tag.get("data-src") or tag.get("src") or ""
            if not url.startswith("http") or "mmbiz" not in url:
                continue
            if url in seen:
                continue
            seen.add(url)
            alt = _clean_text(tag.get("alt") or "")
            blocks.append(BodyBlock(type="image",
                                    image=BodyImage(url=url, caption=alt)))
            continue
        text = _clean_text(tag.get_text(" ", strip=True))
        if not text or _is_promo(text) or _CREDIT.match(text):
            continue
        if blocks and blocks[-1].type == "paragraph" and blocks[-1].text == text:
            continue                          # mp 常见相邻重复段
        blocks.append(BodyBlock(type="paragraph", text=text))
    return blocks


def extract(url: str) -> Article:
    """抽取公众号文章。 Raises ExtractError。"""
    if "mp.weixin.qq.com" not in url:
        raise ExtractError(f"仅支持微信公众号链接: {url}")

    session = _new_session()
    try:
        resp = session.get(url, timeout=25)      # 保留完整 query（poc_token）
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ExtractError(f"下载页面失败: {e}")

    resp.encoding = "utf-8"
    html = resp.text
    if 'id="js_content"' not in html:
        if any(m in html for m in _RISK_MARKERS):
            raise ExtractError(
                "触发公众号风控验证页（环境异常）。请在浏览器打开该链接完成滑块验证，"
                "然后用地址栏中带 poc_token 的完整 URL 重试")
        raise ExtractError(
            "该内容为 JS 动态渲染类型（如图片故事/每日一景），服务端 HTML 不含正文，"
            "静态抓取暂不支持；请换普通图文消息链接")

    soup = BeautifulSoup(html, "lxml")

    title = _meta(soup, "og:title")
    if not title:
        h1 = soup.select_one("#activity-name")
        title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        raise ExtractError("未找到标题，页面结构可能已变更")

    cover = _meta(soup, "og:image")

    account = ""
    name_el = soup.select_one("#js_name")
    if name_el:
        account = _clean_text(name_el.get_text(" ", strip=True))

    content_el = soup.select_one("#js_content")
    blocks = _extract_blocks(content_el) if content_el else []

    paragraphs = [b.text for b in blocks if b.type == "paragraph"]
    lead = paragraphs[0] if paragraphs else ""

    return Article(
        url=url,                          # 保留 poc_token 的完整 URL
        source=account or SOURCE_NAME_FALLBACK,
        source_id=SOURCE_ID,
        title=title,
        author=account,
        section="",
        published_iso="",
        published_display=_publish_display(html),
        summary=[],
        lead=lead,
        blocks=blocks,
        hero_url=cover,
        hero_caption="",
        word_count=sum(len(p) for p in paragraphs),
    )
