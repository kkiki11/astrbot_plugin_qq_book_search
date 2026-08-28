# QQ 群找书

这是一个独立的 AstrBot 插件，用于在 QQ 群中搜索公开书籍服务并回传 TXT 文件。内部插件 ID 为 `astrbot_plugin_qq_book_search`，可以和已有的 `astrbot_plugin_qq_to_telegram_forwarder` 同时安装，不会覆盖或接管原有转发能力。

插件面向 QQ 群的所有输出文案均不暴露底层服务名称，群友只会看到「未找到相关书籍」「服务繁忙，请稍后再试」这类中性提示。

## 安装

在 AstrBot 管理面板中直接上传并安装本插件压缩包，或在插件市场按仓库名搜索更新：

```text
kkiki11/astrbot_plugin_qq_book_search
```

如果提示目录已存在，说明此独立插件之前已经安装过；请使用面板的更新/重载功能，或停止 AstrBot 后删除 `data/plugins/astrbot_plugin_qq_book_search` 再重新上传。不要删除原转发插件目录，也不要删除其 `plugin_data` 数据。

## 配置

| 配置项 | 内容 |
|---|---|
| `book_source_group_ids` | 允许使用找书功能的 QQ 群号，例如 `428568485`。 |
| `api_id` / `api_hash` | 与原转发插件相同的第三方服务应用凭据。 |
| `telegram_session` | 通常可留空；插件启动时会自动复用原转发插件已有的授权文件，也可以单独上传 `.session` 文件。 |
| `book_search_bot` | 默认 `@sobook`，无需修改。 |
| `prefer_latest_sort` | 默认开启。搜索后会自动点击“最新”按钮，并读取排序后的列表。 |
| `txt_only` | 默认开启。仅保留 TXT 结果，过滤 EPUB / MOBI / AZW3 / PDF。 |
| `adult_filter_enabled` | 默认开启。控制关键词、缩写和组合评分过滤；`🔞` 明确分级标记始终过滤。 |
| `adult_filter_keywords` | 成人内容关键词列表；可追加自定义词。内置关键词始终与此项合并；单字词仅作为独立标签匹配，避免误伤普通书名。 |
| `adult_filter_threshold` | 风险评分阈值，范围 1–12，默认 3。数值越高越宽松；`🔞` 不受阈值影响。 |
| `daily_download_limit` | 每日下载上限，默认 50 本，跨自然日自动重置。 |
| `auto_recall_messages` | 默认开启。文件回传成功后撤回过程消息。 |
| `book_search_timeout_seconds` | 等待搜索结果或文件的超时时间，默认 45 秒。服务明确返回空结果时会立即结束，无需等满。 |
| `max_book_file_size_mb` | 单文件回传上限，默认 50 MB，范围 1–512。 |

独立插件会从以下位置复用原转发插件的已授权文件：

```text
data/plugin_data/astrbot_plugin_qq_to_telegram_forwarder/user_session.session
data/plugin_data/astrbot_plugin_qq_to_telegram_forwarder/telegram_user.session
```

如果原转发插件已经能正常工作，通常无需再次登录。不要向 QQ 群或聊天发送 API Hash、验证码、两步验证密码或 `.session` 文件。

## 使用

在配置的 QQ 群中发送：

```text
/找书 书名
```

首次使用前，先在目标 QQ 群发送：

```text
/找书状态
```

该命令会显示当前群是否命中白名单、凭据与授权文件是否就绪，以及今日剩余下载额度；不会显示凭据原文或授权文件内容。

### 搜索结果

插件会自动点击“最新”排序并解析编号列表。QQ 返回的每一项会展示可识别的**格式、文件大小和字数**，例如 `TXT · 5MB · 136.6万字`。

- 结果已按可见顺序重排为**连续序号**，直接发送对应数字即可。
- 只保留 TXT 格式；无法从详情判断格式的条目会保留，实际下载时若不是 TXT 会提示换一个序号。
- `🔞` 分级标记属于硬过滤：无论开关或阈值如何设置，带该标记的条目都不会显示或被选择。
- 服务明确返回空结果时，会立即回复「未找到相关书籍，请换个关键词试试」，不会让群友干等。

### 下载与撤回

发送序号后，插件会执行两阶段操作：先选择书籍，再触发详情卡片的下载按钮。序号绝不会作为普通聊天文本发送出去。

收到文件后，插件通过 QQ 文件消息回传，**成功后自动撤回本次交互中的全部文字消息**——包括你发送的 `/找书 书名`、`正在搜索…`、结果列表和 `正在下载…`，群里只留下文件本身。

> 撤回依赖机器人权限：机器人撤回**自己**的消息通常没有问题；撤回**群友发送的命令消息**需要机器人是管理员。若撤回失败，插件只记录调试日志，不影响文件已送达。可在配置中关闭 `auto_recall_messages`。

### 每日额度

默认每天最多下载 50 本，按自然日统计并在文件成功回传后扣除。额度用尽时提示「今日下载额度已用完，请明天再试」。发送 `/找书状态` 可查看剩余额度。

### 并发说明

书籍服务是对话式的，多人同时查询会互相干扰，因此插件会把与服务的交互串行化。群里同时发起多次搜索时，后一次会排队等待前一次完成，结果不会串号。

## 存储位置

```text
data/plugin_data/astrbot_plugin_qq_book_search/download_quota.json   每日下载计数
data/plugin_data/astrbot_plugin_qq_book_search/book_downloads/       临时文件（延迟 180 秒清理）
data/plugin_data/astrbot_plugin_qq_book_search/user_session.session  账号授权文件
```

## 说明

本插件只自动化您有权获取或转发的公开免费内容，不绕过付费、登录限制或版权保护。
