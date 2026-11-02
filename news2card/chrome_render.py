"""无头 Chrome 渲染：HTML -> PNG（截图）/ PDF（打印）。"""

import shutil
import subprocess
from pathlib import Path

_CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


class RenderError(Exception):
    """渲染失败（未找到浏览器或进程异常）"""


def find_chrome() -> str:
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RenderError("未找到 Chrome/Chromium，请安装 google-chrome 或 chromium")


def _run_chrome(args: list, timeout: int = 90) -> None:
    chrome = find_chrome()
    base = [
        chrome, "--headless", "--disable-gpu", "--disable-dev-shm-usage",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--virtual-time-budget=10000",
    ]
    try:
        proc = subprocess.run(base + args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RenderError(f"Chrome 渲染超时（{timeout}s）")
    # Chrome 退出码不稳定（有时 0 有时非 0 但产物已生成），由调用方校验产物


def html_to_png(html_path: Path, out_png: Path, width: int, height: int) -> None:
    html_path = Path(html_path).resolve()
    out_png = Path(out_png).resolve()
    _run_chrome([
        f"--screenshot={out_png}",
        f"--window-size={width},{height}",
        "--default-background-color=FFFFFFFF",
        f"file://{html_path}",
    ])
    if not out_png.exists() or out_png.stat().st_size == 0:
        raise RenderError(f"PNG 未生成: {out_png}")


def html_to_pdf(html_path: Path, out_pdf: Path) -> None:
    html_path = Path(html_path).resolve()
    out_pdf = Path(out_pdf).resolve()
    _run_chrome([
        f"--print-to-pdf={out_pdf}",
        "--no-pdf-header-footer",
        f"file://{html_path}",
    ])
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        raise RenderError(f"PDF 未生成: {out_pdf}")
