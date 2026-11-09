"""URL 路由：域名 → 抽取器。新增站点时在此注册。"""

from typing import List, Tuple

from . import extract_zaobao, extract_thepaper, extract_wechat
from .extract_zaobao import ExtractError

# 按优先级匹配：域名子串 → 抽取模块
_EXTRACTORS: List[Tuple[str, object]] = [
    ("zaobao.com", extract_zaobao),
    ("thepaper.cn", extract_thepaper),
    ("mp.weixin.qq.com", extract_wechat),
]


def extract(url: str):
    for domain, module in _EXTRACTORS:
        if domain in url:
            return module.extract(url)
    supported = ", ".join(d for d, _ in _EXTRACTORS)
    raise ExtractError(f"暂不支持该链接，当前支持: {supported}")
