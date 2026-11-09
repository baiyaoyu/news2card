"""news-exporter MCP Server（FastMCP，协议 2026-07-28）。

设计要点（均经实测验证）：
- 单工具 generate_news，按 out_format 出卡片/全文PDF/两者
- MRTR：页面无原生摘要时，工具体返回内嵌 CreateMessageRequest（sampling，
  由客户端 LLM 就地生成）的 InputRequiredResult；文章无可用封面时，返回内嵌
  ElicitRequest 表单（由用户填写封面 URL）的 InputRequiredResult。客户端履行后
  自动重试，工具体带 ctx.input_responses 重新执行（SDK 原生的 Elicit/Resolve
  resolver 模式在 FastMCP 下不生效，需用此协议结构）
- 服务端零 LLM：摘要来自页面或客户端 LLM（sampling）；封面来自页面或用户（表单）
- 零本地日志：进度经 ctx.info / ctx.report_progress 流式推送客户端
- SQLite 阶段缓存（news2card/store.py）：恢复轮/重复调用命中缓存，
  不重复抓取、不重复下载图片、不重复渲染

运行：.venv/bin/python server.py（http://127.0.0.1:8001）
"""

import asyncio
import base64
import hashlib
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Literal, Optional

from mcp_types import (CreateMessageRequest, CreateMessageRequestParams,
                       CreateMessageResult, ElicitRequest, ElicitRequestFormParams,
                       ImageContent, InputRequiredResult, SamplingMessage, TaskMetadata,
                       TextContent)
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

import common
from news2card import router
from news2card.chrome_render import html_to_pdf, html_to_png
from news2card.extract_zaobao import ExtractError
from news2card.presets import SITE_DEFAULT_STYLE, get_size
from news2card.store import StageCache
from news2card.templates import build_article_html, build_card_html

mcp = FastMCP("news-exporter-mcp")
cache = StageCache()

FILE_ROOT = common.DEFAULT_OUTDIR.resolve()


@mcp.custom_route("/files/{filename}", methods=["GET"])
async def download_file(request: Request):
    """产物二进制下载（真流式，非 SSE）。文件名白名单校验防目录穿越。"""
    name = request.path_params.get("filename", "")
    if not re.fullmatch(r"[\w][\w.-]*", name):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = (FILE_ROOT / name).resolve()
    if not path.is_file() or not str(path).startswith(str(FILE_ROOT) + "/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    media = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=name)


SUMMARY_SYSTEM_PROMPT = (
    "阅读用户给出的新闻正文，请MCP Client侧的大模型输出 1~4 条中文要点。"
    "要求：每条一行；不要编号、项目符号、小标题或任何额外解释,多条要点需要根据用\n返回；"
    "忠实原意、不臆造细节；正文短时不足 4 条不硬凑。只输出要点文本。"
    "此机制基于最新的MRTR机制实现的MCP Server，需要重新调其MCP工具调用，携带request_state，除添加摘要之外，其他调用参数原样返回处理。")

SUMMARY_BODY_MAX = 8000  # 嵌入采样消息的正文字符上限（防超长正文撑爆请求）

ASK_COVER = (
    "该文章没有可用封面（页面未提供头图或头图下载失败）。"
    "请在 cover_url 字段填写一个封面图片链接；留空则直接生成无封面卡片。")


class CoverInput(BaseModel):
    """用户补充的封面（Elicit 结构化问答的填写模型）"""
    cover_url: str = Field(default="", description="封面图片 URL，留空表示不使用封面")


# ---------------------------------------------------------------- 阶段函数（全部幂等）

async def _stage_article(url: str, ctx: Context):
    hit = cache.get_article(url)
    if hit is not None:
        await ctx.info("命中缓存：文章已解析")
        return hit
    await ctx.info(f"正在解析：{url}")
    article = await asyncio.to_thread(router.extract, url)
    cache.save_article(url, article)
    await ctx.report_progress(0.25, 1.0)
    await ctx.info(f"解析完成：{article.title[:40]}（{article.source}｜{article.section or '-'}）")
    return article


async def _stage_media(article, ctx: Context) -> dict:
    hit = cache.get_media(article.url)
    if hit is not None:
        await ctx.info("命中缓存：图片已下载")
        return hit
    await ctx.info("正在下载图片…")
    hero = await asyncio.to_thread(common.load_hero, article, True)
    inline = await asyncio.to_thread(common.load_inline_images, article, True)
    media = {"hero": hero, "inline": inline}
    cache.save_media(article.url, media)
    await ctx.report_progress(0.5, 1.0)
    await ctx.info("图片就绪")
    return media


def _sampling_text(content) -> str:
    """从 CreateMessageResult.content（单块或块列表）提取文本。"""
    if isinstance(content, list):
        return "".join(_sampling_text(c) for c in content)
    return getattr(content, "text", "") or ""


def _clean_bullet(line: str) -> str:
    """去掉模型可能加上的行首编号/项目符号。"""
    line = line.strip()
    line = re.sub(r"^[-*•·]+[\s　]*", "", line)
    return re.sub(r"^\d{1,2}[.、)]\s*", "", line).strip()


def _summary_sampling_req(article) -> CreateMessageRequest:
    """构造摘要采样请求：正文已缓存，直接嵌入提示词，不让客户端再调工具。"""
    body = "\n\n".join(article.plain_paragraphs)[:SUMMARY_BODY_MAX]
    prompt = (f"标题：{article.title}\n来源：{article.source}"
              + (f"\n导语：{article.lead}" if article.lead else "")
              + f"\n\n正文：\n{body}")
    return CreateMessageRequest(params=CreateMessageRequestParams(
        messages=[SamplingMessage(role="user",
                                  content=TextContent(type="text", text=prompt))],
        system_prompt=SUMMARY_SYSTEM_PROMPT,
        max_tokens=2000,
        task=TaskMetadata(name="news-summary")))


def _native_summary_usable(article) -> bool:
    """页面自带摘要是否可直接用（与 summarizers.py 的"像样"标准一致）。

    澎湃的 summary 实为 80 字截断导语，不算要点，交给客户端 LLM 重新生成。"""
    if not article.summary:
        return False
    if article.source_id == "zaobao":
        return len(article.summary) >= 2
    return sum(len(s) for s in article.summary) > 80


async def _stage_summary(article, ctx: Context):
    """返回 True=摘要就绪继续渲染；InputRequiredResult=需客户端 LLM 生成。"""
    if article.summary_origin == "客户端AI" or _native_summary_usable(article):
        return True

    answers = ctx.input_responses  # 恢复轮：客户端 LLM 的生成结果
    if answers:
        result = answers.get("summary")
        if not isinstance(result, CreateMessageResult):
            return {"ok": False, "error": "客户端未履行摘要生成请求（非 sampling 结果）",
                    "files": []}
        lines = [_clean_bullet(ln) for ln in _sampling_text(result.content).splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            article.summary = lines[:4]
            article.summary_origin = "客户端AI"
            cache.save_article(article.url, article)
            await ctx.info(f"已采用客户端 LLM 摘要（{result.model}）")
            return True
        await ctx.info("客户端 LLM 未返回有效摘要，转为无摘要渲染")
        return True

    await ctx.info("页面无原生摘要，请客户端 LLM 生成（MRTR sampling）")
    return InputRequiredResult(
        input_requests={"summary": _summary_sampling_req(article)},
        request_state=f"news2card:v1:{article.url}")


async def _stage_cover(article, media: dict, ctx: Context):
    """返回 True=封面就绪（或无封面继续渲染）；InputRequiredResult=请用户补充。

    恢复轮接受用户封面 URL：下载成功则回写文章与媒体缓存，失败按无封面继续。"""
    if media["hero"]:
        return True

    answers = ctx.input_responses  # 恢复轮：用户的填写结果
    if answers and "cover" in answers:
        # 本轮应答可能是上一阶段的（如摘要），无 cover 键时继续索要
        result = answers["cover"]
        action = getattr(result, "action", None)
        if action != "accept":
            await ctx.info("用户未提供封面，按无封面渲染")
            return True
        url = str((result.content or {}).get("cover_url") or "").strip()
        if not url:
            await ctx.info("封面 URL 为空，按无封面渲染")
            return True
        article.hero_url = url
        media["hero"] = await asyncio.to_thread(common.load_hero, article, True)
        if media["hero"]:
            cache.save_article(article.url, article)
            cache.save_media(article.url, media)
            await ctx.info("已采用用户提供的封面")
        else:
            await ctx.info("用户封面下载失败，按无封面渲染")
        return True

    await ctx.info("文章无可用封面，请用户补充封面图片 URL（MRTR 问答）")
    req = ElicitRequest(params=ElicitRequestFormParams(
        mode="form", message=ASK_COVER,
        requested_schema=CoverInput.model_json_schema(),
        task=TaskMetadata(name="news-cover")))
    return InputRequiredResult(
        input_requests={"cover": req},
        request_state=f"news2card:v1:{article.url}")


def _render(article, media: dict, out_format: str, size_name: str,
            style_name: str) -> list:
    """同步渲染管线（在线程中执行）。返回产物文件路径列表。"""
    outdir = common.DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    seq = common.next_seq(outdir)
    slug = common.slug_from_url(article.url)
    files = []

    if out_format in ("card", "all"):
        size = get_size(size_name)
        qr = common.make_qr_datauri(article.url, style_name)
        html_text = build_card_html(article, media["hero"], qr,
                                    size["name"], style_name)
        out = outdir / f"{seq:03d}-card-{size['name']}-{style_name}-{slug}.png"
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "card.html"
            hp.write_text(html_text, encoding="utf-8")
            html_to_png(hp, out, size["width"], size["height"])
        files.append(str(out))
        seq += 1

    if out_format in ("pdf", "all"):
        qr = common.make_qr_datauri(article.url, "clean")
        html_text = build_article_html(article, media["hero"],
                                       media["inline"], qr)
        out = outdir / f"{seq:03d}-article-{slug}.pdf"
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "article.html"
            hp.write_text(html_text, encoding="utf-8")
            html_to_pdf(hp, out)
        files.append(str(out))

    return files


# ---------------------------------------------------------------- MCP 工具

@mcp.tool
async def generate_news(
    url: str = Field(description="新闻文章链接（支持联合早报/澎湃新闻）"),
    out_format: Literal["card", "pdf", "all"] = Field(
        default="all", description="card=分享卡片PNG / pdf=全文PDF / all=两者"),
    size: Literal["square", "portrait", "story"] = Field(
        default="square", description="卡片尺寸：方形1080² / 竖版3:4 / 竖长9:16"),
    style: Optional[Literal["clean", "zaobao", "thepaper", "wechat", "dark"]] = Field(
        default=None, description="卡片风格（缺省按站点自动匹配）"),
    ctx: Context = None,
) -> dict:
    try:
        article = await _stage_article(url, ctx)
    except ExtractError as e:
        return {"ok": False, "error": str(e), "files": []}

    summary_state = await _stage_summary(article, ctx)
    if isinstance(summary_state, InputRequiredResult):
        return summary_state          # MRTR：暂停，等客户端生成摘要
    if isinstance(summary_state, dict):
        return summary_state          # 客户端拒绝等失败
    # summary_state is True → 继续

    media = await _stage_media(article, ctx)

    cover_state = await _stage_cover(article, media, ctx)
    if isinstance(cover_state, InputRequiredResult):
        return cover_state            # MRTR：暂停，等用户补充封面
    # cover_state is True → 继续

    style_name = style or SITE_DEFAULT_STYLE.get(article.source_id, "clean")

    # 渲染缓存键含摘要/封面指纹：换摘要或封面（用户重补）必须重渲染
    summary_fp = hashlib.md5(
        ("\n".join(article.summary) + "|" + article.summary_origin).encode("utf-8")
    ).hexdigest()[:8]
    hero_fp = (hashlib.md5(media["hero"].encode("utf-8")).hexdigest()[:8]
               if media["hero"] else "nocover")
    render_key = (f"{article.url}|{out_format}|{size}|{style_name}"
                  f"|{summary_fp}|{hero_fp}")
    files = cache.get_render(render_key)
    if files:
        await ctx.info("命中缓存：产物已存在")
    else:
        await ctx.info(f"正在渲染（{out_format} / {size} / {style_name}）…")
        files = await asyncio.to_thread(_render, article, media,
                                        out_format, size, style_name)
        cache.save_render(render_key, files)
        await ctx.info("渲染完成")

    await ctx.report_progress(1.0, 1.0)
    structured = {"ok": True,
                  "title": article.title,
                  "summary_origin": article.summary_origin or "页面摘要" if article.summary else "",
                  "files": files,
                  "downloads": [{"name": Path(f).name,
                                 "url": f"/files/{Path(f).name}"} for f in files]}
    # 卡片 PNG 内嵌为 ImageContent（客户端 LLM 可直接"看到"卡片）；PDF 走下载流
    images = []
    for f in files:
        if f.endswith(".png"):
            b64 = base64.b64encode(Path(f).read_bytes()).decode("ascii")
            images.append(ImageContent(type="image", data=b64, mime_type="image/png"))
    if images:
        return ToolResult(content=images, structured_content=structured)
    return structured


@mcp.tool
async def get_news_content(
    url: str = Field(description="新闻文章链接（联合早报 / 澎湃新闻 / 微信公众号）"),
    ctx: Context = None,
) -> dict:
    """获取新闻详情：元信息 + 全文内容（按原文块序），不生成摘要。"""
    try:
        article = await _stage_article(url, ctx)
    except ExtractError as e:
        return {"ok": False, "error": str(e)}

    blocks = []
    for b in article.blocks:
        if b.type == "paragraph":
            blocks.append({"type": "paragraph", "text": b.text})
        elif b.type == "image" and b.image:
            blocks.append({"type": "image", "url": b.image.url,
                           "caption": b.image.caption})
        else:
            blocks.append({"type": "note", "text": b.text})

    await ctx.report_progress(1.0, 1.0)
    return {"ok": True,
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "source_id": article.source_id,
            "author": article.author,
            "section": article.section,
            "published": article.published_display,
            "word_count": article.word_count,
            "hero_url": article.hero_url,
            "hero_caption": article.hero_caption,
            "has_native_summary": bool(article.summary),
            "summary": article.summary,
            "lead": article.lead,
            "content": blocks}


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8083,
        stateless_http=False,
    )
