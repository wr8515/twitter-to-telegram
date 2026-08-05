"""服务使用的数据模型。

作者：xxx
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


SourceType = Literal["rss", "x", "auto"]


@dataclass(frozen=True)
class AccountConfig:
    """描述单个 Twitter 账号的采集配置。

    参数:
        username: 不带 @ 的 Twitter 用户名
        source: 采集策略，支持 rss、x、auto
        feed_url: RSS 或 Atom 地址，x 策略可为空
    """

    username: str
    source: SourceType
    feed_url: str | None = None


@dataclass(frozen=True)
class Tweet:
    """描述一条待投递的原创推文。

    参数:
        tweet_id: Twitter 推文 ID
        username: 推文作者用户名
        text: 推文正文
        url: x.com 原文地址
        published_at: 带时区的发布时间
        image_url: 第一张原图地址，无图片时为空
    """

    tweet_id: str
    username: str
    text: str
    url: str
    published_at: datetime
    image_url: str | None = None
