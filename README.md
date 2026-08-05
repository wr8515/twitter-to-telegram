<!-- 作者：xxx -->

# Twitter to Telegram

定期采集多个 Twitter 账号的原创推文，并统一发送到一个 Telegram 频道。每个账号可独立选择 RSS 中间源、`x.com` 网页采集或自动回退策略。

## 已实现规则

- 容器启动后立即采集，之后每 30 分钟执行一轮
- 每账号配置 `rss`、`x` 或 `auto` 策略
- `auto` 先请求 RSS，中间源请求或解析失败时使用 `x.com`
- 只发送能够确认的原创推文，跳过回复、转推和引用推文
- 发送账号、北京时间、原文正文、`x.com` 链接和第一张图片；无原生图片时使用外链卡片封面
- 图片失败时发送正文和链接，并将推文标记为已处理
- 每账号每轮最多补发 20 条，按发布时间从旧到新发送
- Telegram 正文发送失败后每轮重试一次，第 5 次失败后放弃并推进位置
- 不使用数据库，只在 `data/state.json` 保存初始化时间、最近 5 个已处理 ID 和临时失败次数
- Docker 日志最多保留 `10 MB × 3` 个文件，使用 `info` 和 `error` 级别

## 配置

复制示例文件并创建运行目录：

```bash
cp config.example.yaml config.yaml
cp .env.example .env
mkdir -p data secrets
```

编辑 `config.yaml`：

```yaml
accounts:
  - username: zaobaosg
    source: auto
    feed_url: https://your-rss-instance.example/zaobaosg/rss

  - username: nytchinese
    source: rss
    feed_url: https://your-rss-instance.example/nytchinese/rss

  - username: example_account
    source: x
```

来源策略说明：

- `rss`：只使用当前账号的 `feed_url`
- `x`：只使用带登录 Cookie 的 `x.com` 页面
- `auto`：优先使用 `feed_url`，请求或解析报错时回退到 `x.com`

修改账号配置后执行以下命令重启容器，新的账号首次成功采集只建立基线，不发送历史内容：

```bash
docker compose restart
```

## Telegram Secret

1. 使用 BotFather 创建 Bot。
2. 将 Bot 加入目标频道并授予发布消息权限。
3. 把 Bot Token 写入 `secrets/telegram_bot_token.txt`，文件中只放 Token。
4. 在 `.env` 中填写频道用户名或数字 ID：

```dotenv
TELEGRAM_CHAT_ID=@your_channel
```

频道消息格式如下：

```text
@nytchinese
发布时间：2026-08-05 10:30（北京时间）

推文原文内容

原文：https://x.com/nytchinese/status/1234567890
```

## X Cookie

使用专用 X 账号登录后导出 Cookie，写入 `secrets/x-cookies.json`。不要使用主账号 Cookie，也不要把 Secret 文件提交到版本库。

支持 Playwright `storage_state` 格式：

```json
{
  "cookies": [
    {
      "name": "auth_token",
      "value": "replace-with-your-cookie",
      "domain": ".x.com",
      "path": "/",
      "httpOnly": true,
      "secure": true,
      "sameSite": "None"
    }
  ],
  "origins": []
}
```

也支持浏览器扩展直接导出的 Cookie 数组。Cookie 至少应包含当前登录会话需要的 `auth_token`、`ct0` 等字段。Cookie 过期后需要重新导出并重启容器。

即使当前只使用 `rss`，Compose 启动时也要求 Secret 文件存在，可以暂时写入以下内容：

```json
{"cookies": [], "origins": []}
```

## 启动与日志

构建并后台启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f twitter-to-telegram
```

停止服务：

```bash
docker compose down
```

`data/state.json` 必须保留，删除它会使所有账号在下一次成功采集时重新建立基线。

## 边界说明

- 网页采集和第三方 RSS 都不是稳定接口，页面改版、登录验证或中间源格式变化可能导致漏采。
- RSS 元数据不足时会保守跳过条目，避免把回复、转推或引用推文发送到频道。
- 最近 5 个 ID 可以容忍作者删除少量锚点推文，但连续删除全部锚点、停机时间过长或来源只提供有限历史时，无法保证完整补发。
- 状态文件不保存推文正文或图片；待重试推文若已从采集来源消失，将无法继续发送。
- Telegram 请求超时发生在服务端实际接收消息之后时，下一轮重试可能产生重复消息，网页采集方案无法提供严格的恰好一次投递。
