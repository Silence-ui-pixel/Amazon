"""共用浏览器启动策略，保持商品读取与搜索核验的运行环境一致。"""

import logging
import os
import sys

from playwright.sync_api import Browser, Error, Playwright

logger = logging.getLogger(__name__)


def launch_browser(playwright: Playwright, *, headless: bool) -> Browser:
    """尊重显式渠道；Windows 默认浏览器缺失时使用已安装的 Edge。

    只处理可执行文件缺失，不对崩溃、网页超时等异常重复启动浏览器。
    """
    channel = os.getenv("KEYWORD_BROWSER_CHANNEL") or None
    try:
        return playwright.chromium.launch(headless=headless, channel=channel)
    except Error as error:
        if channel or sys.platform != "win32" or "Executable doesn't exist" not in str(error):
            raise
        logger.warning("Playwright bundled browser missing; using installed Microsoft Edge")
        return playwright.chromium.launch(headless=headless, channel="msedge")
