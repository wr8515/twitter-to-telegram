"""轻量 JSON 采集位置状态。

作者：xxx
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.models import Tweet


class StateStore:
    """管理每个账号最近 5 个推文 ID 和临时失败次数。"""

    def __init__(self, state_file: Path) -> None:
        """加载或创建状态存储。

        参数:
            state_file: 持久化 JSON 状态文件路径
        返回:
            无
        """

        self._state_file = state_file
        self._data = self._load()

    def is_initialized(self, username: str) -> bool:
        """判断账号是否已经建立首次采集基线。

        参数:
            username: Twitter 用户名
        返回:
            已建立基线时返回 True
        """

        account_state = self._data["accounts"].get(username.lower())
        return bool(account_state and account_state.get("initialized_at"))

    def initialize(self, username: str, tweets: list[Tweet]) -> None:
        """首次加入账号时记录当前位置而不投递历史推文。

        参数:
            username: Twitter 用户名
            tweets: 首次成功采集到的原创推文列表
        返回:
            无
        """

        recent_ids = sorted({tweet.tweet_id for tweet in tweets}, key=int)[-5:]
        self._data["accounts"][username.lower()] = {
            "initialized_at": datetime.now(UTC).isoformat(),
            "recent_ids": recent_ids,
            "failures": {},
        }
        self._save()

    def candidates(self, username: str, tweets: list[Tweet], limit: int) -> list[Tweet]:
        """选出本轮需要首次投递或继续重试的推文。

        参数:
            username: Twitter 用户名
            tweets: 本轮采集到的原创推文
            limit: 本轮最多返回的推文数量
        返回:
            按发布时间从旧到新排列的候选推文
        """

        account_state = self._account_state(username)
        recent_ids = set(account_state["recent_ids"])
        failed_ids = set(account_state["failures"])
        high_watermark = max((int(tweet_id) for tweet_id in recent_ids), default=None)
        initialized_at = datetime.fromisoformat(account_state["initialized_at"])

        # 1. 【采集服务】【合并待重试推文与 ID 位置之后的新推文】
        selected: list[Tweet] = []
        for tweet in tweets:
            if tweet.tweet_id in recent_ids:
                continue
            is_pending_failure = tweet.tweet_id in failed_ids
            is_after_id = high_watermark is not None and int(tweet.tweet_id) > high_watermark
            is_after_initialization = high_watermark is None and tweet.published_at > initialized_at
            if is_pending_failure or is_after_id or is_after_initialization:
                selected.append(tweet)

        selected.sort(key=lambda item: (item.published_at, int(item.tweet_id)))
        return selected[:limit]

    def mark_processed(self, username: str, tweet_id: str) -> None:
        """将成功发送或主动放弃的推文记录到最近位置。

        参数:
            username: Twitter 用户名
            tweet_id: 已完成处理的推文 ID
        返回:
            无
        """

        account_state = self._account_state(username)
        ids = set(account_state["recent_ids"])
        ids.add(tweet_id)
        account_state["recent_ids"] = sorted(ids, key=int)[-5:]
        account_state["failures"].pop(tweet_id, None)
        self._save()

    def mark_failed(self, username: str, tweet_id: str) -> tuple[int, bool]:
        """累计 Telegram 正文投递失败次数并在第 5 次后放弃。

        参数:
            username: Twitter 用户名
            tweet_id: 投递失败的推文 ID
        返回:
            当前失败次数，以及是否已经达到放弃条件
        """

        account_state = self._account_state(username)
        failures = account_state["failures"]
        attempt = int(failures.get(tweet_id, 0)) + 1
        failures[tweet_id] = attempt
        abandoned = attempt >= 5
        if abandoned:
            ids = set(account_state["recent_ids"])
            ids.add(tweet_id)
            account_state["recent_ids"] = sorted(ids, key=int)[-5:]
            failures.pop(tweet_id, None)
        self._save()
        return attempt, abandoned

    def _account_state(self, username: str) -> dict:
        """读取已初始化账号的内部状态。

        参数:
            username: Twitter 用户名
        返回:
            可修改的账号状态字典
        """

        account_state = self._data["accounts"].get(username.lower())
        if not account_state or not account_state.get("initialized_at"):
            raise RuntimeError(f"账号 @{username} 尚未初始化状态")
        return account_state

    def _load(self) -> dict:
        """从磁盘加载状态文件。

        参数:
            无
        返回:
            包含 accounts 字段的状态字典
        """

        if not self._state_file.exists():
            return {"accounts": {}}
        data = json.loads(self._state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise ValueError("状态文件格式错误，必须包含 accounts 对象")
        return data

    def _save(self) -> None:
        """原子覆盖状态文件，避免容器中断产生半写文件。

        参数:
            无
        返回:
            无
        """

        # 1. 【采集服务】【写入同目录临时文件后原子替换状态文件】
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        temporary_file.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_file, self._state_file)
