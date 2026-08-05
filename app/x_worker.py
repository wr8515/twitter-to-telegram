"""隔离执行单个 x.com 账号采集的子进程入口。

作者：xxx
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.collectors import XBrowserCollector
from app.models import AccountConfig, Tweet


def parse_arguments() -> argparse.Namespace:
    """解析父进程传入的账号和 Cookie 文件参数。

    参数:
        无
    返回:
        包含 username 和 cookie_file 的命令行参数
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--cookie-file", required=True)
    return parser.parse_args()


async def collect(username: str, cookie_file: Path) -> list[Tweet]:
    """在当前隔离进程内执行浏览器采集。

    参数:
        username: 不带 @ 的 Twitter 用户名
        cookie_file: Playwright Cookie 文件路径
    返回:
        当前页面可见的原创推文列表
    """

    account = AccountConfig(username=username, source="x")
    return await XBrowserCollector(cookie_file).collect(account)


def serialize(tweets: list[Tweet]) -> str:
    """将推文列表序列化为父进程可读取的 JSON。

    参数:
        tweets: 浏览器采集到的推文列表
    返回:
        不转义中文的 JSON 文本
    """

    payload = [
        {
            "tweet_id": tweet.tweet_id,
            "username": tweet.username,
            "text": tweet.text,
            "url": tweet.url,
            "published_at": tweet.published_at.isoformat(),
            "image_url": tweet.image_url,
        }
        for tweet in tweets
    ]
    return json.dumps(payload, ensure_ascii=False)


def main() -> None:
    """运行单账号采集并只向标准输出写入结果 JSON。

    参数:
        无
    返回:
        无，采集异常时由进程退出码通知父进程
    """

    arguments = parse_arguments()
    tweets = asyncio.run(collect(arguments.username, Path(arguments.cookie_file)))
    print(serialize(tweets))


if __name__ == "__main__":
    main()
