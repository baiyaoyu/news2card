"""卡片尺寸 × 风格预设。

尺寸与风格自由组合：
- 尺寸控制画布与版式（方形 / 竖版矩形 / 竖长故事版）
- 风格控制配色变量（简约白 / 早报红 / 暗夜黑）
"""

# 画布尺寸（px，CSS 像素，force-device-scale-factor=1）
CARD_SIZES = {
    "square":   {"label": "方形", "width": 1080, "height": 1080},
    "portrait": {"label": "竖版矩形", "width": 1080, "height": 1440},
    "story":    {"label": "竖长全屏", "width": 1080, "height": 1920},
}

# 风格 = CSS 变量集合；layout 由 size-* 类控制，配色由这里注入
CARD_STYLES = {
    "clean": {
        "label": "简约白",
        "vars": {
            "--bg": "#ffffff", "--fg": "#1a1a1a", "--muted": "#8a8a8a",
            "--accent": "#0f4c81", "--accent-fg": "#ffffff",
            "--divider": "#e6e6e6", "--card-shadow": "none",
        },
    },
    "zaobao": {
        "label": "早报红",
        "vars": {
            "--bg": "#fdfbf7", "--fg": "#22201d", "--muted": "#9a938a",
            "--accent": "#b4001e", "--accent-fg": "#ffffff",
            "--divider": "#e8e2d8", "--card-shadow": "none",
        },
    },
    "thepaper": {
        "label": "澎湃蓝",
        "vars": {
            "--bg": "#ffffff", "--fg": "#16181d", "--muted": "#8a93a0",
            "--accent": "#175ce5", "--accent-fg": "#ffffff",
            "--divider": "#e8ebef", "--card-shadow": "none",
        },
    },
    "wechat": {
        "label": "微信绿",
        "vars": {
            "--bg": "#ffffff", "--fg": "#1f2329", "--muted": "#8a8f99",
            "--accent": "#07c160", "--accent-fg": "#ffffff",
            "--divider": "#e8eaee", "--card-shadow": "none",
        },
    },
    "dark": {
        "label": "暗夜黑",
        "vars": {
            "--bg": "#14161a", "--fg": "#f2f2f2", "--muted": "#9aa0a8",
            "--accent": "#d4a24e", "--accent-fg": "#1a1a1a",
            "--divider": "#2c3038", "--card-shadow": "none",
        },
    },
}


# 站点默认风格: source_id → 风格名（拟态各官网品牌色；CLI 未指定 --style 时使用）
SITE_DEFAULT_STYLE = {
    "zaobao": "zaobao",
    "thepaper": "thepaper",
    "wechat": "wechat",
}

DEFAULT_STYLE = "clean"


def get_size(name: str) -> dict:
    key = (name or "square").lower()
    if key not in CARD_SIZES:
        raise KeyError(f"未知尺寸 '{name}'，可选: {', '.join(CARD_SIZES)}")
    return {"name": key, **CARD_SIZES[key]}


def get_style(name: str) -> dict:
    key = (name or "zaobao").lower()
    if key not in CARD_STYLES:
        raise KeyError(f"未知风格 '{name}'，可选: {', '.join(CARD_STYLES)}")
    return {"name": key, **CARD_STYLES[key]}


def style_vars_css(style: dict) -> str:
    return "; ".join(f"{k}: {v}" for k, v in style["vars"].items())
