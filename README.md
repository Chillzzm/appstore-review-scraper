# App Store Review Scraper

一个面向 Codex / WorkBuddy / Claude Code 等Agent 的 App Store 评论研究 Skill。它可以抓取一个目标 App 与多个竞品在指定国家或地区的公开评分与文字评论，并把结果整理成可审计的数据集，进一步支持 JTBD、痛点、未满足需求和产品机会分析。

## 能做什么

- 接受 App Store 产品页链接或数字 Apple ID，无需 Apple 账号或 App Store Connect 凭证。
- 用户可以直接指定要抓取的地区，也可以先扫描 Apple 的 175 个 storefront，再依据评分分布选择重点市场。
- 同时处理一个目标 App 和多个竞品。
- 保存原始 JSONL、规范化 JSON、适合 Excel 打开的 UTF-8 BOM CSV，以及数据集 manifest。
- 对评论执行确定性统计、共同时间窗口对齐、稳定分层抽样和主题聚合。
- 按“观察 / 推断 / 待验证假设”输出有评论 ID 可追溯的竞品分析，避免把模型印象当成统计事实。

## 重要数据口径

- `rating_count` / Apple 的 `userRatingCount` 是评分人数，包含只打星但没有写评论的用户，**不是文字评论数**。
- 公开接口没有竞品的官方文字评论总数。本项目只把本次实际抓回并去重的记录称为“API 可见文字评论”。
- 文字评论来自 Apple 的非公开网页端点，因此属于 best-effort 抓取，可能受接口变化、限流、删除或可见范围影响，不能承诺绝对全量。
- 多 App 的强弱比较只使用同一地区、共同时间窗口内的数据；不同 storefront 的总量不直接混合排名。

## 安装为 Skill

将 github 链接直接发给你的 agent 给你安装或者将仓库克隆到你使用的 Skill 目录。例如：

```bash
# Codex
git clone https://github.com/Chillzzm/appstore-review-scraper.git \
  ~/.codex/skills/appstore-review-scraper

# WorkBuddy
git clone https://github.com/Chillzzm/appstore-review-scraper.git \
  ~/.workbuddy/skills/appstore-review-scraper

# Claude Code
git clone https://github.com/Chillzzm/appstore-review-scraper.git \
  ~/.workbuddy/skills/appstore-review-scraper
```

新建一个任务或重启客户端，使 Skill 被重新发现。项目只依赖 Python 标准库，建议使用 Python 3.9 或更高版本。

## 在对话中使用

你只需要告诉 Skill：

- 目标 App 的 App Store 链接或数字 Apple ID；
- 零到多个竞品链接或 ID；
- 要抓取的地区（可选）；
- 评论起始日期（可选）。

App 链接的获取方式：在 App Store 打开产品页，选择“分享 → 拷贝链接”。Apple ID 是链接中 `id` 后的数字。例如，`.../id123456789` 的 Apple ID 是 `123456789`。

直接指定地区的示例：

```text
$appstore-review-scraper

目标 App：https://apps.apple.com/us/app/example/id123456789
竞品：987654321、https://apps.apple.com/jp/app/example/id1122334455
抓取地区：美国、中国、日本
只保留 2025-01-01 之后的评论
抓取完成后先告诉我数据量，我再决定是否分析
```

如果还不知道该选哪些市场：

```text
$appstore-review-scraper

目标 App：123456789
竞品：987654321
先扫描全地区评分分布，并推荐双方都有数据的重点市场；暂时不要抓文字评论。
```

Skill 会在评论抓取后先汇报数据范围、有效评论数、文本规模和截断状态。只有你确认后，它才会继续做分析。

## 直接运行脚本

### 路径 A：用户已经决定地区

只校验并初始化这些地区，不扫描全球：

```bash
python3 scripts/scrape.py regions \
  --target 123456789 \
  --competitor 987654321 \
  --output-dir runs/demo \
  --countries us,cn,jp

python3 scripts/scrape.py reviews \
  --run-dir runs/demo \
  --countries us,cn,jp \
  --since 2025-01-01
```

`--competitor` 可以重复。国家和地区使用 App Store storefront 的小写 alpha-2 代码，例如 `us`、`cn`、`jp`、`gb` 和 `xk`。

### 路径 B：先发现重点市场

```bash
python3 scripts/scrape.py regions \
  --target 123456789 \
  --competitor 987654321 \
  --output-dir runs/demo \
  --countries all
```

全地区扫描约有 175 次 Lookup 请求，并按 Apple 的调用限制串行执行，通常至少需要约 9 分钟。查看 `storefront_ratings.csv` 后，再为选定地区运行 `reviews` 命令。

### 断点恢复与抓取上限

```bash
python3 scripts/scrape.py reviews \
  --run-dir runs/demo \
  --countries us,cn \
  --resume \
  --max-pages 100
```

主端点持续失败时，可在理解“RSS 每地区最多返回最近约 500 条、结果会标记为截断”的前提下显式添加 `--rss-fallback`。

## 准备竞品分析

先生成全量数据概览和对齐后的分析总体：

```bash
python3 scripts/prepare_analysis.py profile --run-dir runs/demo
```

数据较大时，可生成固定种子的分层样本：

```bash
python3 scripts/prepare_analysis.py sample \
  --run-dir runs/demo \
  --per-app 2000 \
  --seed 42
```

模型按 [`references/竞品分析.md`](references/竞品分析.md) 生成逐条 `annotations.jsonl` 后，用脚本做最终聚合：

```bash
python3 scripts/prepare_analysis.py aggregate \
  --run-dir runs/demo \
  --annotations annotations.jsonl
```

`aggregate` 会校验标注是否完整、是否混入样本外评论，以及评论内容哈希是否发生变化；主题数量和提及率由脚本计算，不由模型估算。

## 输出目录

```text
<run-dir>/
├── manifest.json
├── storefront_ratings.json
├── storefront_ratings.csv
├── apps/<app-id>/reviews/<country>/
│   ├── reviews.raw.jsonl
│   ├── reviews.json
│   ├── reviews.csv
│   └── dataset.json
└── analysis/
    ├── profile.json
    ├── population.jsonl
    ├── sample.jsonl
    └── aggregation.json
```

具体文件会依据你实际执行的阶段生成。

## 开发与测试

项目不依赖第三方 Python 包：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/scrape.py scripts/prepare_analysis.py
```

测试代码保留在 GitHub 仓库中，用于验证分页、限流、断点、数据口径、抽样和聚合逻辑；安装后的 Skill 运行时并不依赖 `tests/`。

## 参考资料

- [Apple Ratings and Reviews](https://developer.apple.com/app-store/ratings-and-reviews/)
- [iTunes Search API](https://performance-partners.apple.com/search-api)
- [App Store localizations](https://developer.apple.com/help/app-store-connect/reference/app-information/app-store-localizations)
- [App Store Connect Customer Reviews API](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-apps-_id_-customerreviews)

请在符合 Apple 条款、适用法律和你的研究授权范围内使用本项目。

## License

[MIT](LICENSE)
