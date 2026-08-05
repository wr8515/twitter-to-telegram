"""Telegram Bot 消息投递客户端。

作者：xxx
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.models import Tweet


LOGGER = logging.getLogger(__name__)
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024


class TelegramClient:
    """通过 Telegram Bot API 向单个频道发送推文。"""

    def __init__(self, client: httpx.AsyncClient, bot_token: str, chat_id: str) -> None:
        """初始化 Telegram 投递客户端。

        参数:
            client: 用于请求 Telegram Bot API 的异步 HTTP 客户端
            bot_token: Telegram Bot Token
            chat_id: 目标频道用户名或数字 ID
        返回:
            无
        """

        self._client = client
        self._chat_id = chat_id
        self._api_base = f"https://api.telegram.org/bot{bot_token}"

    async def send(self, tweet: Tweet) -> None:
        """发送推文正文、北京时间、原文链接和第一张图片。

        参数:
            tweet: 已采集的原创推文
        返回:
            无，正文投递失败时抛出异常
        """

        message = self._format_message(tweet)
        if not tweet.image_url:
            await self._send_text(message)
            return

        # 1. 【Telegram】【短内容优先作为第一张图片说明发送】
        if _telegram_length(message) <= CAPTION_LIMIT:
            try:
                await self._post("sendPhoto", {"photo": tweet.image_url, "caption": message})
                return
            except Exception as error:
                LOGGER.error("推文 %s 图片发送失败，降级发送正文：%s", tweet.tweet_id, error)
                await self._send_text(message)
                return

        # 2. 【Telegram】【长内容先发送正文再尝试发送第一张图片】
        await self._send_text(message)
        try:
            await self._post("sendPhoto", {"photo": tweet.image_url})
        except Exception as error:
            LOGGER.error("推文 %s 正文已发送，但图片发送失败：%s", tweet.tweet_id, error)

    async def _send_text(self, message: str) -> None:
        """按 Telegram 长度限制分段发送正文。

        参数:
            message: 完整频道消息
        返回:
            无，任一分段发送失败时抛出异常
        """

        for chunk in _split_message(message, MESSAGE_LIMIT):
            await self._post(
                "sendMessage",
                {
                    "text": chunk,
                    "link_preview_options": {"is_disabled": True},
                },
            )

    async def _post(self, method: str, payload: dict) -> None:
        """调用 Telegram Bot API 并校验业务响应。

        参数:
            method: Bot API 方法名
            payload: 方法参数，不含 chat_id
        返回:
            无，请求失败或 Telegram 拒绝时抛出异常
        """

        try:
            response = await self._client.post(
                f"{self._api_base}/{method}",
                json={"chat_id": self._chat_id, **payload},
            )
        except httpx.RequestError as error:
            raise RuntimeError(f"Telegram 网络请求失败: {type(error).__name__}") from None

        try:
            result = response.json()
        except ValueError:
            result = {}
        if response.is_error:
            description = result.get("description", "未知错误")
            raise RuntimeError(f"Telegram HTTP {response.status_code}: {description}")
        if not result.get("ok"):
            raise RuntimeError(f"Telegram Bot API 调用失败: {result.get('description', '未知错误')}")

    @staticmethod
    def _format_message(tweet: Tweet) -> str:
        """生成统一的 Telegram 频道消息文本。

        参数:
            tweet: 已采集的原创推文
        返回:
            包含账号、北京时间、正文和原文链接的文本
        """

        published_at = tweet.published_at.astimezone(BEIJING_TIMEZONE)
        timestamp = _format_beijing_time(published_at)
        header = f"@{tweet.username}\n发布时间：{timestamp}（北京时间）"
        body = f"\n\n{tweet.text}" if tweet.text else ""
        return f"{header}{body}\n\n原文：{tweet.url}"


def _format_beijing_time(value: datetime) -> str:
    """把时间格式化为不带秒的北京时间文本。

    参数:
        value: 已转换到北京时间的日期时间
    返回:
        YYYY-MM-DD HH:MM 格式文本
    """

    return value.strftime("%Y-%m-%d %H:%M")


def _telegram_length(text: str) -> int:
    """按 Telegram 使用的 UTF-16 代码单元计算文本长度。

    参数:
        text: 待计算的文本
    返回:
        UTF-16 代码单元数量
    """

    return len(text.encode("utf-16-le")) // 2


def _split_message(text: str, limit: int) -> list[str]:
    """按 UTF-16 限制分割文本并优先在换行处断开。

    参数:
        text: 完整消息文本
        limit: 每段最大 UTF-16 代码单元数
    返回:
        不超过限制且顺序不变的消息分段
    """

    chunks: list[str] = []
    remaining = text
    while _telegram_length(remaining) > limit:
        used_units = 0
        split_index = 0
        last_newline = -1
        for index, character in enumerate(remaining):
            character_units = 2 if ord(character) > 0xFFFF else 1
            if used_units + character_units > limit:
                break
            used_units += character_units
            split_index = index + 1
            if character == "\n":
                last_newline = split_index
        if last_newline > 0:
            split_index = last_newline
        chunks.append(remaining[:split_index].rstrip())
        remaining = remaining[split_index:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
