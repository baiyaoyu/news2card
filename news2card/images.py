"""图片下载与内嵌（base64 data URI），供无头 Chrome 离线渲染。"""

import base64
import mimetypes
from urllib.parse import urlparse

import requests

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

# 各图床要求 Referer 与主站一致（不匹配会 403 或返回假图）；不在表中的图床不带 Referer
_REFERER_BY_HOST = {
    "zbstatic": "https://www.zaobao.com/",
    "thepaper.cn": "https://www.thepaper.cn/",
    "qpic.cn": "https://mp.weixin.qq.com/",
}

# 小于该字节的图片视为防盗链占位假图（如 qpic 跨站 Referer 返回的 ~2KB 假 200）
_MIN_IMAGE_BYTES = 2048


def _headers_for(url: str) -> dict:
    h = dict(_HEADERS)
    for key, referer in _REFERER_BY_HOST.items():
        if key in url:
            h["Referer"] = referer
            break
    return h

# Chrome 渲染安全的 MIME（webp Chrome 支持，原样保留）
_DEFAULT_MIME = "image/jpeg"


def download_image(url: str, timeout: int = 20, retries: int = 2) -> bytes:
    """下载图片；对 403/429/5xx 做有限重试（图床 WAF 会偶发瞬时封禁）。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_headers_for(url), timeout=timeout)
            resp.raise_for_status()
            if len(resp.content) < _MIN_IMAGE_BYTES:
                raise ValueError(
                    f"图片仅 {len(resp.content)}B，疑似防盗链占位假图: {url[:60]}")
            return resp.content
        except requests.RequestException as e:
            last_err = e
            status = getattr(e.response, "status_code", None)
            if status in (403, 429, 500, 502, 503) and attempt < retries:
                import time
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last_err


def guess_mime(url: str, content: bytes) -> str:
    path = urlparse(url).path
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return mime
    # 通过魔数兜底
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return _DEFAULT_MIME


def to_data_uri(url: str, content: bytes) -> str:
    mime = guess_mime(url, content)
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{b64}"
