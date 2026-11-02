#!/usr/bin/env python3
"""news2card — 新闻详情页 -> PNG 分享卡片 / PDF 全文（纯代码流水线，零 LLM）

用法:
    python3.13 news2card.py <url> [--format card|pdf|all]
                        [--size square|portrait|story]
                        [--style clean|zaobao|dark]
                        [--outdir 输出目录] [--no-images]

示例:
    python3.13 news2card.py "https://www.zaobao.com/news/china/story20261025-9889921"
"""

import argparse
import sys
import tempfile
from pathlib import Path

import common
from news2card import router, summarizers
from news2card.chrome_render import html_to_pdf, html_to_png
from news2card.extract_zaobao import ExtractError
from news2card.models import Article
from news2card.presets import CARD_SIZES, CARD_STYLES, SITE_DEFAULT_STYLE, get_size, get_style
from news2card.templates import build_article_html, build_card_html


def generate_card(article: Article, outdir: Path, size_name: str,
                  style_name: str, use_images: bool, seq: int) -> Path:
    size = get_size(size_name)
    hero = common.load_hero(article, use_images)
    qr = common.make_qr_datauri(article.url, style_name)
    html_text = build_card_html(article, hero, qr, size["name"], style_name)

    slug = common.slug_from_url(article.url)
    out = outdir / f"{seq:03d}-card-{size['name']}-{style_name}-{slug}.png"
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "card.html"
        hp.write_text(html_text, encoding="utf-8")
        html_to_png(hp, out, size["width"], size["height"])
    common.log(f"卡片已生成: {out} ({size['width']}x{size['height']})")
    return out


def generate_pdf(article: Article, outdir: Path, use_images: bool, seq: int) -> Path:
    hero = common.load_hero(article, use_images)
    inline = common.load_inline_images(article, use_images)
    qr = common.make_qr_datauri(article.url, "clean")
    html_text = build_article_html(article, hero, inline, qr)

    slug = common.slug_from_url(article.url)
    out = outdir / f"{seq:03d}-article-{slug}.pdf"
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "article.html"
        hp.write_text(html_text, encoding="utf-8")
        html_to_pdf(hp, out)
    common.log(f"PDF 已生成: {out}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="新闻详情页 -> PNG 分享卡片 / PDF 全文（零 LLM）")
    p.add_argument("url", help="新闻文章链接（支持: 联合早报 zaobao.com / 澎湃新闻 thepaper.cn）")
    p.add_argument("--format", choices=["card", "pdf", "all"], default="all",
                   help="输出格式: card(卡片PNG) / pdf(全文PDF) / all(两者，默认)")
    p.add_argument("--size", choices=sorted(CARD_SIZES), default="square",
                   help=f"卡片尺寸（默认 square）: {', '.join(CARD_SIZES)}")
    p.add_argument("--style", choices=sorted(CARD_STYLES), default=None,
                   help="卡片风格（默认按站点自动: 早报→zaobao, 澎湃→thepaper）: "
                        f"{', '.join(CARD_STYLES)}")
    p.add_argument("--outdir", default=None,
                   help="输出目录（默认 /home/share/news-pics，平铺 + NNN- 递增编号）")
    p.add_argument("--no-images", action="store_true", help="不下载图片（纯文字卡片/PDF，速度快）")
    p.add_argument("--summary", choices=["auto", "ai", "off"], default="auto",
                   help="摘要模式: auto(默认级联,纯代码) / ai(缺摘要时调 LLM) / off(不要摘要)")
    p.add_argument("--llm", choices=["gemini", "kimi"], default="gemini",
                   help="AI 摘要提供方: gemini(默认,Gemini 失败自动兜 Kimi) / kimi(只用 Kimi)")
    args = p.parse_args()

    common.log(f"抽取: {args.url}")
    try:
        article = router.extract(args.url)
    except ExtractError as e:
        common.log(f"抽取失败: {e}")
        return 1

    style_name = args.style or SITE_DEFAULT_STYLE.get(article.source_id, "clean")

    if args.summary == "off":
        article.summary = []
        article.summary_origin = ""
        common.log("摘要: 已关闭 (--summary off)")
    else:
        lines, src_label = summarizers.resolve(article, mode=args.summary, llm=args.llm)
        article.summary = lines
        article.summary_origin = src_label
        common.log(f"摘要来源: {src_label}（{len(lines)} 条）"
            + (f" ｜ 首条: {lines[0][:40]}…" if lines else ""))

    common.log(f"标题: {article.title}")
    common.log(f"来源: {article.source}({article.source_id}) ｜ 频道: {article.section or '-'} ｜ "
        f"作者: {article.author or '-'} ｜ 时间: {article.published_display or '-'}")
    n_p = len(article.plain_paragraphs)
    n_img = len([b for b in article.blocks if b.type == "image"])
    common.log(f"正文段落 {n_p} ｜ 正文图片 {n_img} ｜ 头图 {'有' if article.hero_url else '无'}"
        + (f" ｜ 约 {article.word_count} 字" if article.word_count else ""))

    outdir = Path(args.outdir) if args.outdir else common.DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        seq = common.next_seq(outdir)
        if args.format in ("card", "all"):
            generate_card(article, outdir, args.size, style_name, not args.no_images, seq)
            seq += 1
        if args.format in ("pdf", "all"):
            generate_pdf(article, outdir, not args.no_images, seq)
    except Exception as e:
        common.log(f"生成失败: {e}")
        return 2

    print(str(outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
