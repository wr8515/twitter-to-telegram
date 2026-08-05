"""Twitter 采集与 Telegram 投递主服务。

作者：xxx
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from time import monotonic
from typing import cast

import httpx
import yaml

from app.collectors import RssCollector, XCollector
from app.models import AccountConfig, SourceType, Tweet
from app.state import StateStore
from app.telegram import TelegramClient


LOGGER = logging.getLogger(__name__)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MAX_TWEETS_PER_ACCOUNT = 20
INITIAL_TWEETS_TO_SEND = 3
SEND_THROTTLE_SECONDS = 1


class CollectionService:
    """协调多账号采集、增量定位和 Telegram 投递。"""

    def __init__(
        self,
        accounts: list[AccountConfig],
        rss_collector: RssCollector,
        x_collector: XCollector,
        telegram: TelegramClient,
        state: StateStore,
        poll_interval_seconds: int,
    ) -> None:
        """初始化采集服务。

        参数:
            accounts: 需要轮询的 Twitter 账号配置
            rss_collector: RSS 中间源采集器
            x_collector: x.com 网页采集器
            telegram: Telegram 频道投递客户端
            state: 最近位置和失败次数状态存储
            poll_interval_seconds: 两轮采集开始时间之间的秒数
        返回:
            无
        """

        self._accounts = accounts
        self._rss_collector = rss_collector
        self._x_collector = x_collector
        self._telegram = telegram
        self._state = state
        self._poll_interval_seconds = poll_interval_seconds

    async def run_forever(self) -> None:
        """启动立即采集，并按配置间隔持续执行。

        参数:
            无
        返回:
            无，任务被取消时结束
        """

        while True:
            cycle_started = monotonic()
            await self._run_cycle()
            elapsed = monotonic() - cycle_started
            await asyncio.sleep(max(0, self._poll_interval_seconds - elapsed))

    async def _run_cycle(self) -> None:
        """顺序处理一轮所有账号，隔离单账号异常。

        参数:
            无
        返回:
            无
        """

        LOGGER.info("开始新一轮采集，共 %d 个账号", len(self._accounts))
        for account in self._accounts:
            try:
                await self._process_account(account)
            except Exception:
                LOGGER.exception("账号 @%s 本轮处理失败", account.username)
        LOGGER.info("本轮采集结束")

    async def _process_account(self, account: AccountConfig) -> None:
        """采集并投递单个账号本轮最多 20 条推文。

        参数:
            account: 当前 Twitter 账号配置
        返回:
            无
        """

        # 1. 【Twitter】【按账号策略采集原创推文】
        tweets = await self._collect(account)
        LOGGER.info("账号 @%s 采集到 %d 条可确认的原创推文", account.username, len(tweets))

        # 2. 【采集服务】【首次启用建立基线并选择最新三条推文】
        if not self._state.is_initialized(account.username):
            baseline_tweets = tweets[:-INITIAL_TWEETS_TO_SEND]
            candidates = tweets[-INITIAL_TWEETS_TO_SEND:]
            self._state.initialize(account.username, baseline_tweets)
            LOGGER.info(
                "账号 @%s 已建立首次基线，准备发送最新 %d 条推文",
                account.username,
                len(candidates),
            )
        else:
            # 3. 【采集服务】【按发布时间选取增量和待重试推文】
            candidates = self._state.candidates(account.username, tweets, MAX_TWEETS_PER_ACCOUNT)
        if not candidates:
            LOGGER.info("账号 @%s 本轮没有待发送推文", account.username)
            return

        # 4. 【Telegram】【逐条投递并持久记录结果】
        for tweet in candidates:
            await self._deliver(account, tweet)
            await asyncio.sleep(SEND_THROTTLE_SECONDS)

    async def _collect(self, account: AccountConfig) -> list[Tweet]:
        """根据账号策略选择 RSS、x.com 或自动回退采集。

        参数:
            account: 当前 Twitter 账号配置
        返回:
            当前来源可见范围内的原创推文
        """

        if account.source == "rss":
            return await self._rss_collector.collect(account)
        if account.source == "x":
            return await self._x_collector.collect(account)

        # 1. 【Twitter】【auto 策略优先使用中间源】
        try:
            return await self._rss_collector.collect(account)
        except Exception as error:
            LOGGER.error("账号 @%s 的 RSS 采集失败，改用 x.com：%s", account.username, error)

        # 2. 【Twitter】【中间源失败后使用 x.com Cookie 兜底】
        return await self._x_collector.collect(account)

    async def _deliver(self, account: AccountConfig, tweet: Tweet) -> None:
        """投递单条推文，并更新成功位置或失败次数。

        参数:
            account: 当前 Twitter 账号配置
            tweet: 待投递推文
        返回:
            无
        """

        try:
            await self._telegram.send(tweet)
        except Exception as error:
            attempt, abandoned = self._state.mark_failed(account.username, tweet.tweet_id)
            if abandoned:
                LOGGER.error(
                    "推文 %s 第 %d 次发送失败，已放弃并推进位置：%s",
                    tweet.tweet_id,
                    attempt,
                    error,
                )
            else:
                LOGGER.error(
                    "推文 %s 第 %d 次发送失败，将在下轮重试：%s",
                    tweet.tweet_id,
                    attempt,
                    error,
                )
            return

        self._state.mark_processed(account.username, tweet.tweet_id)
        LOGGER.info("推文 %s 已发送到 Telegram 频道", tweet.tweet_id)


async def run_service() -> None:
    """读取配置和 Secret 后启动长期采集服务。

    参数:
        无
    返回:
        无，服务停止或启动失败时结束
    """

    # 1. 【采集服务】【从固定挂载位置加载配置与最小状态】
    accounts, poll_interval_minutes = load_config(Path("/app/config.yaml"))
    state = StateStore(Path("/app/data/state.json"))
    bot_token = read_secret(Path(_required_environment("TELEGRAM_BOT_TOKEN_FILE")))
    chat_id = _required_environment("TELEGRAM_CHAT_ID")
    cookie_file = Path(_required_environment("X_COOKIE_FILE"))

    # 2. 【采集服务】【复用 HTTP 连接并启动长期轮询】
    timeout = httpx.Timeout(45, connect=20)
    headers = {"User-Agent": "twitter-to-telegram/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        service = CollectionService(
            accounts=accounts,
            rss_collector=RssCollector(client),
            x_collector=XCollector(cookie_file),
            telegram=TelegramClient(client, bot_token, chat_id),
            state=state,
            poll_interval_seconds=poll_interval_minutes * 60,
        )
        LOGGER.info("采集服务启动，轮询间隔为 %d 分钟", poll_interval_minutes)
        await service.run_forever()


def load_config(config_file: Path) -> tuple[list[AccountConfig], int]:
    """读取并校验 YAML 轮询间隔和账号列表。

    参数:
        config_file: YAML 配置文件路径
    返回:
        去重且保持顺序的账号列表，以及轮询间隔分钟数
    """

    data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml 顶层必须是对象")

    poll_interval_minutes = data.get("poll_interval_minutes")
    if type(poll_interval_minutes) is not int or poll_interval_minutes <= 0:
        raise ValueError("poll_interval_minutes 必须是大于 0 的整数")

    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("config.yaml 必须包含非空 accounts 列表")

    # 1. 【采集服务】【逐项校验用户名、来源策略和 Feed 地址】
    accounts: list[AccountConfig] = []
    seen_usernames: set[str] = set()
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise ValueError("accounts 中每一项都必须是对象")
        username = str(raw_account.get("username") or "").strip().removeprefix("@")
        source = str(raw_account.get("source") or "").strip().lower()
        feed_url = str(raw_account.get("feed_url") or "").strip() or None

        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError(f"Twitter 用户名格式错误: {username!r}")
        if source not in ("rss", "x", "auto"):
            raise ValueError(f"账号 @{username} 的 source 必须是 rss、x 或 auto")
        if source in ("rss", "auto") and not feed_url:
            raise ValueError(f"账号 @{username} 使用 {source} 策略时必须配置 feed_url")
        normalized_username = username.lower()
        if normalized_username in seen_usernames:
            raise ValueError(f"Twitter 账号重复配置: @{username}")

        seen_usernames.add(normalized_username)
        source_type = cast(SourceType, source)
        accounts.append(AccountConfig(username, source_type, feed_url))
    return accounts, poll_interval_minutes


def read_secret(secret_file: Path) -> str:
    """读取 Docker Secret 并拒绝空内容。

    参数:
        secret_file: Secret 挂载文件路径
    返回:
        去除首尾空白后的 Secret 文本
    """

    value = secret_file.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret 文件为空: {secret_file}")
    return value


def configure_logging() -> None:
    """配置仅包含 info 和 error 的标准输出日志。

    参数:
        无
    返回:
        无
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    # 1. 【安全】【禁止 HTTP 客户端在 info 日志中输出包含 Bot Token 的请求地址】
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)


def _required_environment(name: str) -> str:
    """读取必填环境变量。

    参数:
        name: 环境变量名
    返回:
        非空环境变量值
    """

    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量: {name}")
    return value
