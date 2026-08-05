"""服务命令行入口。

作者：xxx
"""

import asyncio

from app.service import configure_logging, run_service


def main() -> None:
    """初始化日志并运行异步采集服务。

    参数:
        无
    返回:
        无
    """

    configure_logging()
    asyncio.run(run_service())


if __name__ == "__main__":
    main()
