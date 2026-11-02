import qrcode
import re
import sys
import io
from pathlib import Path

from news2card.models import Article
from news2card.images import download_image, to_data_uri

def log(msg: str) -> None:
    print(f"[news2card] {msg}", file=sys.stderr)


def make_qr_datauri(url: str, style_name: str) -> str:
    """生成指向原文的二维码（深色风格用白底反色，其余黑模块）。"""
    fill = "#ffffff" if style_name == "dark" else "#000000"
    back = "#14161a" if style_name == "dark" else "#ffffff"
    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill, back_color=back)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    import base64
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def slug_from_url(url: str) -> str:
    m = re.search(r"story(\d+-\d+)", url)
    if m:
        return f"story{m.group(1)}"
    m = re.search(r"(newsDetail_forward_\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/([^/?]+)$", url.split("?")[0])
    return re.sub(r"[^\w-]+", "_", m.group(1))[:60] if m else "article"


# 默认输出根目录（共享平铺目录，文件按 NNN- 全局递增编号）
DEFAULT_OUTDIR = Path("/home/share/news-pics")


def next_seq(outdir: Path, start: int = 0) -> int:
    """根据目录内已有 NNN- 前缀文件，返回下一个可用序号。"""
    seq = start
    if outdir.exists():
        for f in outdir.iterdir():
            m = re.match(r"^(\d{3})-", f.name)
            if m:
                seq = max(seq, int(m.group(1)))
    return seq + 1


def load_hero(article: Article, use_images: bool) -> str:
    if not use_images or not article.hero_url:
        return ""
    try:
        return to_data_uri(article.hero_url, download_image(article.hero_url))
    except Exception as e:
        log(f"头图下载失败（已跳过）: {e}")
        return ""


def load_inline_images(article: Article, use_images: bool) -> dict:
    uris = {}
    if not use_images:
        return uris
    urls = [b.image.url for b in article.blocks if b.type == "image" and b.image]
    for u in urls:
        try:
            uris[u] = to_data_uri(u, download_image(u))
        except Exception as e:
            log(f"正文图片下载失败（已跳过）: {u[:70]} -> {e}")
    return uris