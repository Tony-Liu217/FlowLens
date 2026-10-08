# 第一阶段接口与数据约定

所有 `/api/` 请求使用 `Authorization: Bearer <本地临时令牌>`，令牌由启动链接的 fragment 传入并仅在本浏览器页会话中保存。静态资源来自 `frontend/`；无外部资源、旧会话 API 或 AI 接口。

## 接口

| 方法 / 路径 | 职责 |
| --- | --- |
| `GET /api/state` | 当前账本上下文、账本和批次摘要、当前批次核验快照、导入任务 |
| `POST /api/books` | 新建、打开、重命名、打开批次、移入回收站（trash）、恢复（restore）；UI 不暴露永久删除 |
| `POST /api/import` | `files: [{name, data: base64}]`，可选批次名称及跨批重复文件选项；202 后轮询 state |
| `GET /api/overview` | 整本有效输入的来源／币种金额、日期范围、缺口及尚未计算标识 |
| `GET /api/records` | 整本分页明细，默认每页 50、最多 100；支持 `q/source/status/start/end/batch_id` |
| `GET /api/detail` | 当前批次指定记录的有效字段、原始读取和原始行 |
| `GET /api/evidence` | 当前批次登记且哈希匹配的 PNG 证据；前端鉴权请求后生成 Blob |
| `POST /api/action` | 确认／修正、补录、排除、页面确认、最近一项撤销 |
| `GET /api/export` | 整本有效 JSON，未解决缺口时拒绝完整导出；`allow_partial=true` 明确请求部分数据 |

除 state 外，读取账本的接口与写接口均携带 `book_id + catalog_revision`。detail/evidence 携带当前 `batch_id + workspace_id`；action 携带批次、底稿身份、`expected_revision`、唯一 `request_id`。`action=confirm` 的 `note` 可省略或为空字符串（最多 2000 字）；其他动作仍要求非空依据。无备注确认仍保留完整事件与字段变化，不生成替代的用户理由。

导入任务可携带 `started_at`／`finished_at`（Unix 秒）与 `progress`。进度字段为 `stage`、`completed`、`total`（文件数），以及当前 `filename`、`segment`／`segments`、`page`。阶段依次可能为 `preparing/reading/standardizing/ocr/file_done/saving`；`completed` 包括已处理但失败或跳过的文件，不代表可用文件数，也不是时间百分比。任务的 `status=completed` 只在保存并登记成功后写入。读取旧任务时这些字段可能不存在，前端须兼容。

单批次修订通过 SQLite 事务保存，重复 action 请求重放原结果，过期写入返回 409。目录切换也推进目录版本，禁止旧页面跨账本或跨批次写入。导入期间禁止切换账本及核验写入。

detail 增加只读 `attention`：`fields` 为标准字段到核对原因数组的映射，`raw_columns` 为 `{index, fields, reasons}` 数组（index 从 0 开始），`unlocalized` 保留不能定位字段的行级问题。依据未解决的字段错误和已登记、哈希验证的 OCR 关键字段证据生成；不修改底稿、人工修订或放行策略。已确认／排除记录返回空标记。前端同时显示荧光标记和文字原因，不把未标记解释为准确保证；整页完整性仍由原有任务展示。

## 分析输入和显示语义

- 底稿 `ledger.sqlite3` 为不可变读取结果；`review.sqlite3` 追加修订；`books.sqlite3` 保存账本和批次目录。
- 单批读取使用 `load_effective_records()`，整本入口使用 `export_book()`，格式为 `book-effective-1`。
- 当前快照、单批有效导出及整本导出的批次信息携带 `reading_policy`。分析缓存应使用底稿身份、修订版本和读取规则版本；只看人工修订号不足以识别规则升级。
- 已有底稿不改写。新版有效读取结果中的 `policy_adjustment` 记录规则及原解析状态；自动放行的旧 OCR 任务可标为 `resolved_by_policy`，不等同于 `human_confirmed`。人工覆盖仍优先。
- `balance_reliable=false` 表示 OCR 余额仅供参考；`auxiliary_reason_codes` 保留非阻断原因。它不会阻断有效交易金额，但未来余额校验不得忽略这一标志。日期、金额、方向及整页完整性门槛仍保留。
- `book_record_id = batch_id + ':' + record_id`，不要仅用单批 record_id 关联全账本。
- 整本输入携带每批底稿身份和修订版本。多批次没有一个能替代整个版本向量的 `revision`。
- `overview.status` 区分 `empty/ready/partial`，仅表示读取覆盖；`analysis_status=not_implemented`、`personal_expense=null`、`transaction_deduplication=false`。
- 金额在后端以 Decimal 相加并以十进制字符串返回。前端只格式化，不自行净额推断。来源汇总包含各交易状态，不能解释为已结算账户现金流或个人消费。
- 部分预览保留未核验、排除、不可读批次信息。不可读批次的记录数未知，不伪装成零。
- import-task.json 通过临时文件替换持久化任务；服务重启检查 COMPLETE 和目录登记，避免把未完成导入认定为成功或重复登记。

## 变更边界

导入和核验核心包名保持不变。新增 `app/service.py` 编排本地任务，`app/server.py` 处理传输，`app/views.py` 提供整本展示模型。第一阶段不依赖 `jiaowopay_reconcile`，不支持 Demo 的 `AnalysisBundle`。

本地文件原文视为数据，前端以安全文本呈现。文件路径与证据访问必须使用现有校验入口，不能让前端提交一个任意磁盘路径后直接读取。
