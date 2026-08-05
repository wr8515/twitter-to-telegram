"""RSS 与 x.com 推文采集器。

作者：xxx
"""

import asyncio
import calendar
import json
import logging
import os
import re
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Error as PlaywrightError,
    Page,
    Request,
    Response,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.models import AccountConfig, Tweet


LOGGER = logging.getLogger(__name__)
STATUS_PATH_PATTERN = re.compile(r"/([A-Za-z0-9_]+)/status/(\d+)", re.IGNORECASE)
HANDLE_PATTERN = re.compile(r"@([A-Za-z0-9_]+)")
X_COLLECTION_TIMEOUT_SECONDS = 150


class RssCollector:
    """从 RSS 或 Atom Feed 中采集原创推文。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """初始化 RSS 采集器。

        参数:
            client: 用于请求 Feed 的异步 HTTP 客户端
        返回:
            无
        """

        self._client = client

    async def collect(self, account: AccountConfig) -> list[Tweet]:
        """采集指定账号的原创推文。

        参数:
            account: 包含用户名和 Feed 地址的账号配置
        返回:
            按推文 ID 从小到大排列的原创推文列表
        """

        if not account.feed_url:
            raise ValueError(f"账号 @{account.username} 未配置 feed_url")

        # 1. 【Twitter】【下载并解析中间源 Feed】
        try:
            response = await self._client.get(account.feed_url)
        except httpx.RequestError as error:
            raise RuntimeError(f"Feed 网络请求失败: {type(error).__name__}") from None
        if response.is_error:
            raise RuntimeError(f"Feed HTTP 状态异常: {response.status_code}")
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"Feed 解析失败: {feed.bozo_exception}")

        # 2. 【Twitter】【保守筛选能够确认属于该账号的原创推文】
        tweets: dict[str, Tweet] = {}
        for entry in feed.entries:
            tweet = self._parse_entry(entry, account)
            if tweet is not None:
                tweets[tweet.tweet_id] = tweet

        return sorted(tweets.values(), key=lambda item: int(item.tweet_id))

    def _parse_entry(self, entry: feedparser.FeedParserDict, account: AccountConfig) -> Tweet | None:
        """将单个 Feed 条目转换为原创推文。

        参数:
            entry: feedparser 解析后的条目
            account: 当前账号配置
        返回:
            能够确认是原创推文时返回 Tweet，否则返回空
        """

        link = str(entry.get("link") or entry.get("id") or "")
        status_match = STATUS_PATH_PATTERN.search(link)
        if status_match is None:
            self._log_skipped(account.username, "缺少可识别的状态链接")
            return None

        link_author, tweet_id = status_match.groups()
        if link_author.lower() != account.username.lower():
            self._log_skipped(account.username, f"状态链接作者为 @{link_author}")
            return None

        summary = str(entry.get("summary") or entry.get("description") or "")
        soup = BeautifulSoup(summary, "html.parser")
        if self._looks_like_non_original(entry, soup):
            self._log_skipped(account.username, f"推文 {tweet_id} 被识别为回复、转推或引用")
            return None

        creator = self._extract_creator(entry)
        if creator is not None and creator.lower() != account.username.lower():
            self._log_skipped(account.username, f"推文 {tweet_id} 的作者元数据不匹配")
            return None

        published_at = self._extract_published_at(entry)
        if published_at is None:
            self._log_skipped(account.username, f"推文 {tweet_id} 缺少发布时间")
            return None

        text = self._extract_text(entry, soup)
        image_url = self._extract_first_image(entry, soup, account.feed_url or "")
        canonical_url = f"https://x.com/{account.username}/status/{tweet_id}"
        return Tweet(tweet_id, account.username, text, canonical_url, published_at, image_url)

    @staticmethod
    def _extract_creator(entry: feedparser.FeedParserDict) -> str | None:
        """提取 Feed 条目中的作者用户名。

        参数:
            entry: feedparser 解析后的条目
        返回:
            规范化后的用户名，缺少作者元数据时返回空
        """

        raw_creator = str(entry.get("author") or entry.get("dc_creator") or "").strip()
        if not raw_creator:
            return None
        handles = HANDLE_PATTERN.findall(raw_creator)
        return handles[-1] if handles else raw_creator.removeprefix("@").strip()

    @staticmethod
    def _looks_like_non_original(entry: feedparser.FeedParserDict, soup: BeautifulSoup) -> bool:
        """判断 Feed 条目是否明显属于回复、转推或引用。

        参数:
            entry: feedparser 解析后的条目
            soup: 条目 HTML 内容
        返回:
            能识别为非原创内容时返回 True
        """

        title = str(entry.get("title") or "").strip().lower()
        non_original_prefixes = ("rt by @", "retweeted by @", "reposted by @", "r to @", "replying to @")
        if title.startswith(non_original_prefixes):
            return True

        selectors = (
            ".retweet-header",
            ".replying-to",
            ".quote",
            ".quote-link",
            ".quoted-tweet",
            "blockquote",
        )
        return any(soup.select_one(selector) is not None for selector in selectors)

    @staticmethod
    def _extract_published_at(entry: feedparser.FeedParserDict) -> datetime | None:
        """提取并规范化 Feed 条目的发布时间。

        参数:
            entry: feedparser 解析后的条目
        返回:
            UTC 时区的发布时间，无法解析时返回空
        """

        parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed_time is None:
            return None
        timestamp = calendar.timegm(parsed_time)
        return datetime.fromtimestamp(timestamp, tz=UTC)

    @staticmethod
    def _extract_text(entry: feedparser.FeedParserDict, soup: BeautifulSoup) -> str:
        """提取推文正文并保留换行。

        参数:
            entry: feedparser 解析后的条目
            soup: 条目 HTML 内容
        返回:
            清理后的推文正文，图片推文可能为空字符串
        """

        tweet_node = soup.select_one(".tweet-content")
        if tweet_node is not None:
            return tweet_node.get_text("\n", strip=True)
        return str(entry.get("title") or "").strip()

    @staticmethod
    def _extract_first_image(
        entry: feedparser.FeedParserDict,
        soup: BeautifulSoup,
        feed_url: str,
    ) -> str | None:
        """提取推文第一张非头像图片。

        参数:
            entry: feedparser 解析后的条目
            soup: 条目 HTML 内容
            feed_url: 用于补全相对图片地址的 Feed 地址
        返回:
            第一张图片的绝对地址，无图片时返回空
        """

        # 1. 【Twitter】【优先使用 Feed 标准媒体和附件字段】
        for media in entry.get("media_content", []) or []:
            media_url = str(media.get("url") or "")
            media_kind = str(media.get("medium") or media.get("type") or "").lower()
            if media_url and ("image" in media_kind or _looks_like_tweet_image(media_url)):
                return _as_original_image(urljoin(feed_url, media_url))
        for enclosure in entry.get("enclosures", []) or []:
            media_url = str(enclosure.get("href") or enclosure.get("url") or "")
            media_type = str(enclosure.get("type") or "")
            if media_url and media_type.startswith("image/"):
                return _as_original_image(urljoin(feed_url, media_url))

        # 2. 【Twitter】【从正文 HTML 中排除头像和表情后取第一张图片】
        for image in soup.find_all("img"):
            media_url = str(image.get("src") or "")
            classes = " ".join(image.get("class") or []).lower()
            lowered_url = media_url.lower()
            if not media_url or "avatar" in classes or "profile_images" in lowered_url or "emoji" in classes:
                continue
            if not _looks_like_tweet_image(media_url):
                continue
            return _as_original_image(urljoin(feed_url, media_url))
        return None

    @staticmethod
    def _log_skipped(username: str, reason: str) -> None:
        """用 info 级别记录被保守过滤的条目。

        参数:
            username: 当前 Twitter 用户名
            reason: 跳过条目的原因
        返回:
            无
        """

        LOGGER.info("账号 @%s 跳过无法确认的原创内容：%s", username, reason)


class XCollector:
    """通过独立子进程执行 x.com 采集并提供硬超时保护。"""

    def __init__(self, cookie_file: Path) -> None:
        """初始化带进程隔离的 x.com 采集器。

        参数:
            cookie_file: Playwright storage_state 或 Cookie 数组文件
        返回:
            无
        """

        self._cookie_file = cookie_file

    async def collect(self, account: AccountConfig, stop_ids: list[str] | None = None) -> list[Tweet]:
        """在独立进程中采集账号并限制最长执行时间。

        参数:
            account: 当前账号配置
            stop_ids: 页面出现其中任一 ID 后停止继续滚动
        返回:
            按推文 ID 从小到大排列的原创推文列表
        """

        # 1. 【Twitter】【启动独立进程并隔离 Chromium 进程组】
        command = [
            sys.executable,
            "-m",
            "app.x_worker",
            "--username",
            account.username,
            "--cookie-file",
            str(self._cookie_file),
        ]
        for stop_id in stop_ids or []:
            command.extend(("--stop-id", stop_id))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        # 2. 【Twitter】【超时后强制回收采集进程及全部浏览器子进程】
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=X_COLLECTION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise TimeoutError(
                f"账号 @{account.username} 的 x.com 采集超过 {X_COLLECTION_TIMEOUT_SECONDS} 秒"
            ) from None

        if process.returncode != 0:
            error_lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
            primary_errors = [line.strip() for line in error_lines if "x.com 未加载推文节点" in line]
            selected_lines = primary_errors[-1:] or error_lines[-6:]
            error_tail = " | ".join(line.strip() for line in selected_lines if line.strip())
            error_message = error_tail[-1_200:] if error_tail else "未知错误"
            raise RuntimeError(f"x.com 采集进程退出码 {process.returncode}: {error_message}")

        return self._decode_tweets(stdout)

    @staticmethod
    def _decode_tweets(payload: bytes) -> list[Tweet]:
        """把 worker 返回的 JSON 转换为推文模型。

        参数:
            payload: worker 标准输出中的 UTF-8 JSON
        返回:
            反序列化后的推文列表
        """

        raw_tweets = json.loads(payload.decode("utf-8"))
        if not isinstance(raw_tweets, list):
            raise ValueError("x.com 采集进程返回格式错误")
        return [
            Tweet(
                tweet_id=str(item["tweet_id"]),
                username=str(item["username"]),
                text=str(item["text"]),
                url=str(item["url"]),
                published_at=datetime.fromisoformat(str(item["published_at"])),
                image_url=str(item["image_url"]) if item.get("image_url") else None,
            )
            for item in raw_tweets
        ]


class XBrowserCollector:
    """使用带登录 Cookie 的 Chromium 从 x.com 采集原创推文。"""

    def __init__(self, cookie_file: Path) -> None:
        """初始化 x.com 采集器。

        参数:
            cookie_file: Playwright storage_state 或 Cookie 数组文件
        返回:
            无
        """

        self._cookie_file = cookie_file

    async def collect(self, account: AccountConfig, stop_ids: list[str] | None = None) -> list[Tweet]:
        """从账号主页采集当前可见的原创推文。

        参数:
            account: 当前账号配置
            stop_ids: 页面出现其中任一 ID 后停止继续滚动
        返回:
            按推文 ID 从小到大排列的原创推文列表
        """

        if not self._cookie_file.is_file():
            raise FileNotFoundError(f"X Cookie 文件不存在: {self._cookie_file}")

        # 1. 【Twitter】【创建独立浏览器上下文并隔离账号页面状态】
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel="chromium",
                headless=True,
                args=(
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--renderer-process-limit=2",
                ),
            )
            try:
                context = await self._create_context(browser)
                try:
                    return await self._collect_from_page(context, account, set(stop_ids or []))
                finally:
                    # 2. 【Twitter】【清理已退出的浏览器上下文时不覆盖主错误】
                    try:
                        await context.close()
                    except PlaywrightError:
                        pass
            finally:
                # 3. 【Twitter】【清理已退出的 Chromium 时不覆盖主错误】
                try:
                    await browser.close()
                except PlaywrightError:
                    pass

    async def _create_context(self, browser: Browser) -> BrowserContext:
        """根据 Cookie 文件创建英文界面的浏览器上下文。

        参数:
            browser: 已启动的 Chromium 浏览器
        返回:
            已加载登录 Cookie 的浏览器上下文
        """

        cookie_data = json.loads(self._cookie_file.read_text(encoding="utf-8"))
        context_options = {
            "locale": "en-US",
            "viewport": {"width": 900, "height": 700},
            "device_scale_factor": 1,
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
            ),
        }
        if isinstance(cookie_data, dict) and isinstance(cookie_data.get("cookies"), list):
            context = await browser.new_context(storage_state=cookie_data, **context_options)
        elif isinstance(cookie_data, list):
            context = await browser.new_context(**context_options)
            cookies = [self._normalize_cookie(cookie) for cookie in cookie_data]
            await context.add_cookies(cookies)
        else:
            raise ValueError("X Cookie 文件必须是 Playwright storage_state 对象或 Cookie 数组")

        await context.route("**/*", _filter_browser_request)
        return context

    @staticmethod
    def _normalize_cookie(raw_cookie: dict) -> dict:
        """把常见浏览器导出 Cookie 转换为 Playwright 格式。

        参数:
            raw_cookie: 浏览器扩展或自动化工具导出的 Cookie 字典
        返回:
            Playwright 可接受的 Cookie 字典
        """

        cookie = {
            key: raw_cookie[key]
            for key in ("name", "value", "url", "domain", "path", "expires", "httpOnly", "secure")
            if key in raw_cookie
        }
        if "expires" not in cookie and "expirationDate" in raw_cookie:
            cookie["expires"] = raw_cookie["expirationDate"]
        same_site = str(raw_cookie.get("sameSite") or "").lower()
        same_site_mapping = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
        if same_site in same_site_mapping:
            cookie["sameSite"] = same_site_mapping[same_site]
        return cookie

    async def _collect_from_page(
        self,
        context: BrowserContext,
        account: AccountConfig,
        stop_ids: set[str],
    ) -> list[Tweet]:
        """打开账号主页并在有限滚动范围内提取原创推文。

        参数:
            context: 已登录的浏览器上下文
            account: 当前账号配置
            stop_ids: 页面出现其中任一 ID 后停止继续滚动
        返回:
            按推文 ID 从小到大排列的原创推文列表
        """

        page = await context.new_page()
        api_responses: list[Response] = []
        browser_errors: list[str] = []
        page.on("response", lambda response: self._record_api_response(api_responses, response))
        page.on("requestfailed", lambda request: self._record_request_failure(browser_errors, request))
        page.on("console", lambda message: self._record_console_error(browser_errors, message))
        page.on("pageerror", lambda error: self._append_diagnostic(browser_errors, f"pageerror={error}"))
        try:
            await page.goto(
                f"https://x.com/{account.username}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            try:
                # 1. 【Twitter】【只等待推文节点挂载而不依赖无头浏览器可见性计算】
                await page.wait_for_selector(
                    'article[data-testid="tweet"]',
                    state="attached",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                page_state = await self._describe_page_failure(page, api_responses, browser_errors)
                raise RuntimeError(f"x.com 未加载推文节点：{page_state}") from None

            # 2. 【Twitter】【分段读取虚拟列表并保留已离开页面的推文】
            tweets: dict[str, Tweet] = {}
            for _ in range(6):
                articles = page.locator('article[data-testid="tweet"]')
                for index in range(await articles.count()):
                    try:
                        tweet = await self._parse_article(articles.nth(index), account)
                    except PlaywrightTimeoutError:
                        LOGGER.info("账号 @%s 的一个动态推文节点已失效，继续采集", account.username)
                        continue
                    if tweet is not None:
                        tweets[tweet.tweet_id] = tweet

                # 3. 【Twitter】【发现状态锚点或取得首次三条后立即停止滚动】
                found_stop_id = bool(stop_ids.intersection(tweets))
                enough_for_initialization = not stop_ids and len(tweets) >= 3
                if found_stop_id or enough_for_initialization or len(tweets) >= 25:
                    break
                await page.mouse.wheel(0, 2_400)
                await page.wait_for_timeout(1_500)

            return sorted(tweets.values(), key=lambda item: int(item.tweet_id))
        finally:
            # 4. 【Twitter】【页面已关闭时保留原始采集异常】
            try:
                await page.close()
            except PlaywrightError:
                pass

    @staticmethod
    def _record_api_response(responses: list[Response], response: Response) -> None:
        """保留最近的 X API 响应以便失败时诊断。

        参数:
            responses: 当前页面已记录的 API 响应
            response: Playwright 新收到的响应
        返回:
            无
        """

        if "/graphql/" not in response.url and "/i/api/" not in response.url:
            return
        responses.append(response)
        del responses[:-8]

    @staticmethod
    def _record_request_failure(errors: list[str], request: Request) -> None:
        """记录 X API、页面或脚本的网络失败。

        参数:
            errors: 当前页面已记录的浏览器错误
            request: Playwright 报告失败的请求
        返回:
            无
        """

        is_api = "/graphql/" in request.url or "/i/api/" in request.url
        if not is_api and request.resource_type not in {"document", "script"}:
            return
        path = urlparse(request.url).path.rsplit("/", 1)[-1][:80]
        XBrowserCollector._append_diagnostic(
            errors,
            f"requestfailed={request.resource_type}:{path}:{request.failure or '未知原因'}",
        )

    @staticmethod
    def _record_console_error(errors: list[str], message: ConsoleMessage) -> None:
        """记录页面控制台的 error 级别摘要。

        参数:
            errors: 当前页面已记录的浏览器错误
            message: Playwright 页面控制台消息
        返回:
            无
        """

        if message.type == "error" and "Failed to load resource" not in message.text:
            XBrowserCollector._append_diagnostic(errors, f"console={message.text[:180]}")

    @staticmethod
    def _append_diagnostic(items: list[str], item: str) -> None:
        """追加一条定长浏览器诊断信息。

        参数:
            items: 诊断信息列表
            item: 本次追加的诊断文本
        返回:
            无
        """

        items.append(re.sub(r"\s+", " ", item).strip())
        del items[:-6]

    @staticmethod
    async def _summarize_api_response(response: Response) -> str:
        """提取单个 X API 响应的操作名、状态和错误摘要。

        参数:
            response: 待摘要的 Playwright API 响应
        返回:
            不包含请求头和 Cookie 的单行诊断文本
        """

        operation = urlparse(response.url).path.rsplit("/", 1)[-1][:80] or "unknown"
        result = f"{operation}={response.status}"
        try:
            body = await asyncio.wait_for(response.text(), timeout=1)
            payload = json.loads(body)
        except Exception:
            return result
        if isinstance(payload, dict) and payload.get("errors"):
            error_text = json.dumps(payload["errors"], ensure_ascii=False, separators=(",", ":"))
            return f"{result}:{error_text[:220]}"
        if isinstance(payload, dict) and payload.get("data") is None:
            return f"{result}:data=null"
        return f"{result}:data=ok"

    @staticmethod
    async def _describe_page_failure(
        page: Page,
        api_responses: list[Response],
        browser_errors: list[str],
    ) -> str:
        """提取 x.com 页面的有限诊断信息。

        参数:
            page: 未加载出推文节点的 Playwright 页面
            api_responses: 最近的 X API 响应
            browser_errors: 最近的页面脚本和请求错误
        返回:
            包含 URL、标题、页面提示和 API 状态的诊断文本
        """

        # 1. 【Twitter】【限制诊断文本长度以避免异常日志过大】
        try:
            title = await page.title()
        except Exception:
            title = "无法读取"
        try:
            body_text = await page.locator("body").inner_text(timeout=2_000)
            body_summary = re.sub(r"\s+", " ", body_text).strip()[:300]
        except Exception:
            body_summary = "无法读取"
        api_summaries = await asyncio.gather(
            *(XBrowserCollector._summarize_api_response(response) for response in api_responses[-6:])
        )
        api_summary = " | ".join(api_summaries) or "未捕获到 X API 响应"
        error_summary = " | ".join(browser_errors[-4:]) or "未捕获到浏览器错误"
        return (
            f"url={page.url}，title={title!r}，page={body_summary!r}，"
            f"api={api_summary}，browser={error_summary}"
        )

    @staticmethod
    async def _parse_article(article, account: AccountConfig) -> Tweet | None:
        """解析 x.com 页面中的单个推文节点。

        参数:
            article: Playwright 推文节点定位器
            account: 当前账号配置
        返回:
            能够确认是原创推文时返回 Tweet，否则返回空
        """

        payload = await article.evaluate(
            r"""
            (node, username) => {
                const pattern = /\/([A-Za-z0-9_]+)\/status\/(\d+)/i;
                const statusLinks = Array.from(node.querySelectorAll('a[href*="/status/"]'))
                    .map((link) => link.getAttribute('href') || '')
                    .map((href) => ({href, match: href.match(pattern)}))
                    .filter((item) => item.match);
                const ownLink = statusLinks.find(
                    (item) => item.match[1].toLowerCase() === username.toLowerCase()
                );
                const distinctIds = new Set(statusLinks.map((item) => item.match[2]));
                const textNode = node.querySelector('[data-testid="tweetText"]');
                const timeNode = node.querySelector('time');
                const imageNode = node.querySelector('[data-testid="tweetPhoto"] img');
                const cardImageNode = node.querySelector('[data-testid="card.wrapper"] img');
                return {
                    tweetId: ownLink ? ownLink.match[2] : null,
                    text: textNode ? textNode.innerText : '',
                    publishedAt: timeNode ? timeNode.getAttribute('datetime') : null,
                    imageUrl: imageNode
                        ? imageNode.getAttribute('src')
                        : cardImageNode?.getAttribute('src') || null,
                    isReply: node.innerText.includes('Replying to'),
                    hasQuotedStatus: distinctIds.size > 1,
                };
            }
            """,
            account.username,
            timeout=2_000,
        )
        if not payload["tweetId"] or payload["isReply"] or payload["hasQuotedStatus"]:
            return None
        if not payload["publishedAt"]:
            LOGGER.info("账号 @%s 跳过缺少发布时间的推文 %s", account.username, payload["tweetId"])
            return None

        published_at = datetime.fromisoformat(str(payload["publishedAt"]).replace("Z", "+00:00"))
        tweet_id = str(payload["tweetId"])
        canonical_url = f"https://x.com/{account.username}/status/{tweet_id}"
        image_url = _as_original_image(str(payload["imageUrl"])) if payload["imageUrl"] else None
        return Tweet(tweet_id, account.username, str(payload["text"]), canonical_url, published_at, image_url)


def _as_original_image(image_url: str) -> str:
    """把 Twitter 图片地址调整为原图尺寸。

    参数:
        image_url: RSS 或 x.com 返回的图片地址
    返回:
        对 pbs.twimg.com 请求原图后的地址，其他地址保持不变
    """

    parsed = urlparse(image_url)
    if parsed.hostname != "pbs.twimg.com":
        return image_url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "orig"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _looks_like_tweet_image(image_url: str) -> bool:
    """判断图片地址是否明确指向 Twitter 推文媒体。

    参数:
        image_url: Feed HTML 或媒体字段中的图片地址
    返回:
        能通过路径确认是推文媒体图片时返回 True
    """

    normalized_url = unquote(image_url).lower()
    indicators = ("pbs.twimg.com/media", "/pic/media", "/media/")
    return "card_img" not in normalized_url and any(item in normalized_url for item in indicators)


async def _filter_browser_request(route: Route) -> None:
    """阻止不影响 DOM 媒体地址提取的高开销浏览器资源。

    参数:
        route: Playwright 当前拦截到的网络请求
    返回:
        无
    """

    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
        return
    await route.continue_()
