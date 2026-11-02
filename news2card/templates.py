"""硬编码 HTML 模板构建：分享卡片（PNG）与全文页（PDF）。

- 所有文本经 html.escape 转义
- 图片一律 base64 data URI 内嵌（无头 Chrome 离线渲染）
"""

import html
from datetime import datetime
from typing import Dict, Optional

from .models import Article
from .presets import get_size, get_style, style_vars_css

_FONT = '"WenQuanYi Micro Hei","AR PL UMing CN",sans-serif'

# 全文 PDF 强调色（主色 / 摘要框底色），按 source_id 匹配，缺省中性灰
_PDF_ACCENTS = {
    "zaobao": ("#b4001e", "#faf6ef"),
    "thepaper": ("#175ce5", "#f0f5ff"),
}
_PDF_ACCENT_DEFAULT = ("#333a45", "#f5f6f8")


def _pdf_accent(source_id: str) -> tuple:
    return _PDF_ACCENTS.get(source_id, _PDF_ACCENT_DEFAULT)


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


# 摘要来源 → 卡片/PDF 上的标注小字（空字符串 = 不标注）
_ORIGIN_MARKS = {
    "Gemini": "要点由 AI 生成",
    "Kimi": "要点由 AI 生成",
    "客户端AI": "要点由客户端 AI 生成",
    "页面摘要": "摘要来自联合早报 · AI 辅助生成",
    "提取式": "要点摘自原文",
    "站点摘要": "摘自原文导语",
    "导语": "摘自原文导语",
}


def _origin_mark(origin: str) -> str:
    return _ORIGIN_MARKS.get(origin or "", "")


def _pick_summary_lines(article: Article, max_lines: int = 4) -> list:
    if article.summary:
        return article.summary[:max_lines]
    if article.lead:
        return [article.lead]
    return []


# ---------------------------------------------------------------- 分享卡片

def build_card_html(article: Article, hero_datauri: str, qr_datauri: str,
                    size_name: str = "square", style_name: str = "zaobao") -> str:
    size = get_size(size_name)
    style = get_style(style_name)
    w, h = size["width"], size["height"]
    css_vars = style_vars_css(style)

    has_cover = bool(hero_datauri)
    summary_lines = _pick_summary_lines(article, 4)
    title_clamp = {"square": 3, "portrait": 4, "story": 5}[size["name"]]

    cover_css = f'background-image:url("{hero_datauri}")' if has_cover else ""

    bullets = "".join(
        f'<li data-clamp="2">{_e(line)}</li>' for line in summary_lines
    )

    qr_block = f'<img class="qr" src="{qr_datauri}"/>' if qr_datauri else '<div class="qr-empty"></div>'
    origin_mark = _origin_mark(article.summary_origin)
    origin_html = f'<div class="origin">{_e(origin_mark)}</div>' if origin_mark else ""

    domain = article.url.split("/")[2] if article.url else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ width:{w}px; height:{h}px; }}
body {{ font-family:{_FONT}; {css_vars}; }}
.card {{ width:100%; height:100%; display:flex; flex-direction:column;
        background:var(--bg); color:var(--fg); overflow:hidden; position:relative; }}

/* ---- 封面区 ---- */
.cover {{ flex:none; background:var(--accent); background-size:cover; background-position:center;
         position:relative; {cover_css} }}
.size-square .cover {{ height:46%; }}
.size-portrait .cover {{ height:52%; }}
.size-story  .cover {{ height:58%; }}
.card.no-cover .cover {{ height:22%; }}
.badges {{ position:absolute; top:28px; left:32px; display:flex; gap:14px; }}
.badge {{ padding:10px 20px; font-size:24px; border-radius:8px; letter-spacing:2px; }}
.badge.source {{ background:var(--accent); color:var(--accent-fg); font-weight:700; }}
.badge.section {{ background:rgba(0,0,0,.45); color:#fff; backdrop-filter:blur(2px); }}
.no-cover .badges {{ top:auto; bottom:24px; }}

/* ---- 正文区 ---- */
.body {{ flex:1; display:flex; flex-direction:column; padding:36px 44px 32px; min-height:0; }}
.title {{ font-weight:700; line-height:1.32; }}
.size-square .title {{ font-size:46px; }}
.size-portrait .title {{ font-size:50px; }}
.size-story  .title {{ font-size:54px; }}
.summary {{ margin-top:26px; list-style:none; }}
.summary li {{ position:relative; padding-left:30px; margin-bottom:14px; font-size:27px;
              line-height:1.5; color:var(--fg); opacity:.88; overflow:hidden;
              max-height:calc(1.5em * 2); }}
.summary li::before {{ content:""; position:absolute; left:6px; top:.62em; width:12px; height:12px;
                      background:var(--accent); border-radius:2px; }}
.summary li:empty {{ display:none; }}

.foot {{ margin-top:auto; padding-top:22px; border-top:2px solid var(--divider);
        display:flex; align-items:flex-end; justify-content:space-between; gap:24px; }}
.meta .src {{ font-size:26px; font-weight:700; color:var(--fg); }}
.meta .sub {{ margin-top:10px; font-size:21px; color:var(--muted); line-height:1.5; }}
.meta .origin {{ margin-top:8px; font-size:18px; color:var(--muted); opacity:.85; letter-spacing:1px; }}
.qr {{ width:150px; height:150px; border-radius:10px; background:#fff; padding:8px; }}
.qr-empty {{ width:150px; height:150px; }}
</style></head>
<body>
<div class="card size-{size['name']} {'has-cover' if has_cover else 'no-cover'}">
  <div class="cover">
    <div class="badges">
      <span class="badge source">{_e(article.source)}</span>
      {f'<span class="badge section">{_e(article.section)}</span>' if article.section else ''}
    </div>
  </div>
  <div class="body">
    <div class="title" data-clamp="{title_clamp}">{_e(article.title)}</div>
    <ul class="summary">{bullets}</ul>
    <div class="foot">
      <div class="meta">
        <div class="src">{_e(article.source)} · {_e(article.section or '即时报道')}</div>
        <div class="sub">{_e(article.author)}{' ｜ ' if article.author else ''}{_e(article.published_display)}<br>{_e(domain)}</div>
        {origin_html}
      </div>
      {qr_block}
    </div>
  </div>
</div>
<script>
// 行数钳制 + 省略号（替代有渲染 bug 的 -webkit-line-clamp）
(function () {{
  function clampEl(el, maxLines) {{
    var lh = parseFloat(getComputedStyle(el).lineHeight);
    if (!lh || !el.textContent) return;
    var max = lh * maxLines;
    if (el.scrollHeight <= max + 1) return;
    var text = el.textContent;
    var lo = 0, hi = text.length;
    while (lo < hi) {{
      var mid = (lo + hi + 1) >> 1;
      el.textContent = text.slice(0, mid) + "…";
      if (el.scrollHeight <= max + 1) lo = mid; else hi = mid - 1;
    }}
    el.textContent = text.slice(0, lo) + "…";
  }}
  var nodes = document.querySelectorAll("[data-clamp]");
  for (var i = 0; i < nodes.length; i++) {{
    clampEl(nodes[i], parseInt(nodes[i].getAttribute("data-clamp"), 10));
  }}
  // 版面保险：内容超高时递减摘要条目，直到装下（保底 1 条）
  var bodyEl = document.querySelector(".body");
  var summaryEl = document.querySelector(".summary");
  if (bodyEl && summaryEl) {{
    var guard = 0;
    while (bodyEl.scrollHeight > bodyEl.clientHeight + 1
           && summaryEl.children.length > 1 && guard < 4) {{
      summaryEl.removeChild(summaryEl.lastElementChild);
      guard++;
    }}
  }}
}})();
</script>
</body></html>"""


# ---------------------------------------------------------------- 全文页（PDF）

def build_article_html(article: Article, hero_datauri: str,
                       inline_images: Dict[str, str], qr_datauri: str,
                       generated_at: Optional[str] = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    accent, accent_bg = _pdf_accent(article.source_id)
    summary_label = "AI 摘要" if article.source_id == "zaobao" else "摘要"

    def render_blocks() -> str:
        out = []
        for b in article.blocks:
            if b.type == "paragraph":
                out.append(f'<p class="para">{_e(b.text)}</p>')
            elif b.type == "image" and b.image:
                uri = inline_images.get(b.image.url, "")
                if not uri:
                    continue
                cap = f'<figcaption>{_e(b.image.caption)}</figcaption>' if b.image.caption else ""
                out.append(f'<figure><img src="{uri}"/>{cap}</figure>')
            elif b.type == "note":
                out.append(f'<p class="note">{_e(b.text)}</p>')
        return "\n".join(out)

    hero = ""
    if hero_datauri:
        cap = (f'<figcaption>{_e(article.hero_caption)}</figcaption>'
               if article.hero_caption else "")
        hero = f'<figure class="hero"><img src="{hero_datauri}"/>{cap}</figure>'

    summary_box = ""
    if article.summary:
        items = "".join(f"<li>{_e(s)}</li>" for s in article.summary)
        origin_mark = _origin_mark(article.summary_origin)
        origin_html = (f'<span class="sb-origin">{_e(origin_mark)}</span>'
                       if origin_mark else "")
        summary_box = (f'<div class="summary-box"><div class="sb-title">'
                       f'{summary_label}{origin_html}</div><ul>{items}</ul></div>')

    qr_foot = ""
    if qr_datauri:
        qr_foot = f"""
  <div class="endcard">
    <img class="qr" src="{qr_datauri}"/>
    <div class="endtext">
      <div class="t1">扫码阅读原文</div>
      <div class="t2">{_e(article.url)}</div>
      <div class="t2">本文档由 news2card 生成 ｜ {generated_at}</div>
    </div>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 18mm 16mm; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:{_FONT}; color:#22201d; font-size:11.5pt; line-height:1.85; }}
.kicker {{ display:flex; gap:10px; align-items:center; color:{accent}; font-size:10pt; font-weight:700; letter-spacing:1px; }}
.kicker .dot {{ width:10px; height:10px; background:{accent}; border-radius:2px; }}
h1 {{ font-size:20pt; line-height:1.4; margin:10px 0 12px; font-weight:700; }}
.meta {{ color:#8a837a; font-size:9.5pt; padding-bottom:14px; border-bottom:2px solid #e8e2d8; }}
.summary-box {{ background:{accent_bg}; border-left:4px solid {accent}; padding:12px 16px; margin:16px 0;
               font-size:10pt; color:#4a453e; }}
.summary-box .sb-title {{ font-weight:700; color:{accent}; margin-bottom:6px; }}
.summary-box .sb-origin {{ float:right; font-weight:400; font-size:8pt; color:#8a837a; }}
.summary-box ul {{ margin-left:18px; }}
.summary-box li {{ margin-bottom:4px; line-height:1.7; }}
figure {{ margin:14px 0; text-align:center; }}
figure img {{ max-width:100%; border-radius:4px; }}
figcaption {{ font-size:8.5pt; color:#8a837a; margin-top:6px; line-height:1.5; text-align:left; }}
p.para {{ text-indent:2em; text-align:justify; margin:8px 0; }}
p.note {{ color:#8a837a; font-size:9pt; border:1px dashed #d8d2c8; padding:8px 12px; margin:10px 0; }}
.endcard {{ margin-top:36px; padding-top:20px; border-top:2px solid #e8e2d8; display:flex; gap:18px; align-items:center; }}
.endcard .qr {{ width:110px; height:110px; }}
.endtext .t1 {{ font-weight:700; font-size:12pt; }}
.endtext .t2 {{ color:#8a837a; font-size:8.5pt; margin-top:6px; word-break:break-all; }}
</style></head>
<body>
  <div class="kicker"><span class="dot"></span>{_e(article.source)}{' ｜ ' + _e(article.section) if article.section else ''}</div>
  <h1>{_e(article.title)}</h1>
  <div class="meta">{_e(article.author)}{' ｜ ' if article.author else ''}{_e(article.published_display)}{' ｜ ' + _e(article.url) if article.url else ''}</div>
  {summary_box}
  {hero}
  {render_blocks()}
  {qr_foot}
</body></html>"""
