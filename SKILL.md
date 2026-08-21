---
name: appstore-review-scraper
description: 抓取 App Store 应用在不同国家/地区的评分分布和公开可见文字评论，并基于评论进行单产品洞察或竞品分析。适用于 App Store 评论抓取、地区选择、用户需求研究和竞品比较；不适用于 Google Play 或 App Store Connect 私有数据。
---

# App Store 评论抓取与竞品分析

先判断地区选择方式：用户已经指定抓取地区时，校验这些地区后直接抓取；用户未指定时，先让其选择“直接指定地区”或“扫描全地区后再选”。只有用户希望发现重点市场时才扫描全地区评分分布。评论抓取完成后不要自动开始分析。

## 先收集输入

必须取得：

- 一个目标 App 的 App Store 产品页 URL 或数字 Apple ID。
- 如需竞品分析，再取得零到多个竞品的 URL 或数字 Apple ID。

同时询问或识别用户是否已经指定抓取国家/地区，以及可选的评论起始日期。用户明确给出地区时视为已经完成选区，不再要求先扫描全球分布或再次确认；用户未给地区时，说明可直接指定，也可扫描全地区评分分布后再决定，不要静默启动约 9 分钟的全球扫描。App Store 链接中的 `/us/`、`/cn/` 等仅表示该链接的 storefront，不能单独视为用户选择了该地区。

告诉用户获取方式：在 App Store 打开 App，选择“分享 → 拷贝链接”；也可直接提供链接中 `id` 后面的数字，例如 `https://apps.apple.com/us/app/example/id123456789` 对应 `123456789`。不要索取 App 名称、Bundle ID、Apple 账号、密码或 App Store Connect 凭证；用户只给名称时，应请其补充 URL 或数字 ID，避免同名误判。

## 1. 确定抓取地区并初始化任务

### 用户已指定地区

只查询用户指定的 storefront，用于校验 App、初始化运行目录和记录这些地区的评分元数据；不要扫描其余地区：

```bash
python3 scripts/scrape.py regions \
  --target <URL|ID> \
  --competitor <URL|ID> \
  --output-dir <run-dir> \
  --countries us,cn
```

`--competitor` 可重复。向用户回显 App 名称、开发者、ID，以及指定地区的可用/未上架/查询失败状态。地区已经由用户决定，因此校验完成后直接进入评论抓取，不要再次询问选区。

### 用户希望发现地区

只有用户主动选择了解全球分布和推荐市场时，才运行全地区扫描：

运行：

```bash
python3 scripts/scrape.py regions \
  --target <URL|ID> \
  --competitor <URL|ID> \
  --output-dir <run-dir> \
  --countries all
```

默认 `--countries all`。先用 Lookup 结果校验每个 ID 为软件 App，并向用户回显 App 名称、开发者和 ID。未上架与请求失败是不同状态，不得当作评分数为零。全地区扫描约发出 175 次请求；按约 3.2 秒的请求间隔，通常至少需要约 9 分钟。

地区结果中的 `userRatingCount` 是评分人数，包含只打星但没有文字评论的用户；`averageUserRating` 是该 storefront 的平均评分。公开接口不提供竞品的官方文字评论总数，也不得用评分人数减去已抓评论数推算“只打星人数”。

全地区扫描完成后展示：

- 用户指定的重点市场；
- 所有待比较 App 均可用且合计评分人数最高的前 5 个地区；
- 上述重点市场和前 5 个地区的状态、评分人数和平均评分。

完整地区明细保存在 `storefront_ratings.json` 和 `storefront_ratings.csv`，不必在对话中铺开全部 175 个地区。

然后停止并请用户选择要抓取文字评论的地区。

## 2. 抓取已确定地区的评论

地区由用户直接指定或在全地区扫描后确认，随后运行：

```bash
python3 scripts/scrape.py reviews \
  --run-dir <run-dir> \
  --countries us,cn \
  [--since YYYY-MM-DD] \
  [--resume] \
  [--max-pages N]
```

评论网页端点是非公开、best-effort 数据源，不能承诺绝对全量。只有实际抓回并去重的数量可称为“本次 API 可见文字评论数”。若主端点持续失效，只在用户同意后降级为 RSS 最近最多 500 条，并在数据集中标记 `truncated=true`。

抓取完成后汇报每个 App × 地区的成功/失败状态、本次 API 可见文字评论数、时间范围、有效文本字符数、是否截断以及输出目录。数据集中的 `status=complete` 只表示本次 best-effort 抓取流程正常结束，不表示获得 Apple 官方评论全集。默认在此停止，并询问用户是否继续分析；即使用户最初已经提出“做竞品分析”，也要先披露实际数据范围、规模与预计分析成本，再次取得确认。

## 3. 仅在用户同意后分析

先对本次抓回的全部 API 可见评论生成数据概览：

```bash
python3 scripts/prepare_analysis.py profile --run-dir <run-dir>
```

`profile` 会生成携带内容哈希的 `analysis/population.jsonl`。单 App 时它包含全部非空正文；多 App 时只保留同一地区中所有 App 都有数据、且日期落在该地区共同交集窗口内的评论。无法对齐的地区仍在 `profile.json` 中披露，但不进入竞品提及率；若对齐后的总体为空，不得输出强弱结论。全量标注应以 `population.jsonl` 为输入。若对齐后的有效评论超过 2,000 条或正文合计超过 1,000,000 字符，先说明全量分批标注会增加耗时和模型成本，再让用户选择：

- 全量分批标注；或
- 固定随机种子的分层抽样分析。

抽样可运行（标注时逐行保留 `content_hash`）：

```bash
python3 scripts/prepare_analysis.py sample \
  --run-dir <run-dir> --per-app 2000 --seed 42
```

开始分析前必须阅读 [references/竞品分析.md](references/竞品分析.md)，按其中的数据对齐、标注、聚合、证据和报告规范执行。模型不得凭印象估算主题数量；完成标注后用脚本聚合：

```bash
python3 scripts/prepare_analysis.py aggregate \
  --run-dir <run-dir> --annotations <annotations.jsonl>
```

## 数据口径与权威参考

- Apple 将星级评分与可选文字评论区分开：[Ratings and reviews](https://developer.apple.com/app-store/ratings-and-reviews/)。
- Lookup API 的请求格式和调用限制见 [iTunes Search API](https://performance-partners.apple.com/search-api)。
- storefront 清单以 [App Store localizations](https://developer.apple.com/help/app-store-connect/reference/app-information/app-store-localizations) 为依据。
- App Store Connect Customer Reviews API 只适合有权限的自有 App，不用于无凭证竞品抓取：[Customer Reviews API](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-apps-_id_-customerreviews)。
