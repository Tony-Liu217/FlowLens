# 开发与验证

## 本地运行

按根目录 README 创建 Python 3.12 虚拟环境、安装 `requirements.txt` 并准备 OCR 模型。前端无需构建，后端使用本机 HTTP 服务和 SQLite。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s backend\tests -q
.\.venv\Scripts\python.exe -B -X utf8 scripts\check_format_variants.py
```

后端测试使用临时合成数据，不要求提供真实账单。格式变化脚本包含已知语义盲点的反例；脚本通过不代表该盲点已修复。

## 浏览器验收（开发者可选）

浏览器测试额外需要 Node.js、Playwright 和已安装的 Microsoft Edge。应用用户不需要 Node.js。

```powershell
npm install --no-save --package-lock=false playwright
node scripts/browser_acceptance.cjs
node scripts/browser_continuous_review.cjs
node scripts/browser_book_management.cjs
```

测试默认按 `runtime/python.exe`、`.venv/Scripts/python.exe`、`.venv/bin/python`、系统 `python` 查找环境。可显式指定：

```powershell
$env:FLOWLENS_TEST_PYTHON = (Resolve-Path '.\.venv\Scripts\python.exe').Path
node scripts/browser_acceptance.cjs
```

每次测试新建 `.local-data/` 下的隔离账本，使用单独的无个人配置浏览器会话。截图、连接凭证和测试结果只保存在私有目录。

`browser_progress_acceptance.cjs` 需要显式提供一个本地账单图片，运行真实 OCR；`verify_real_sources.py` 需要显式传入文件路径。两者不属于无样本的默认验收，不应将测试输入提交到仓库。

## 当前验证范围

已有 Windows 本机验证覆盖：合成读取／核验／账本回归、连续核验、双页面过期写入保护、追加、重启、导出、补录、页面笔数核验、删除恢复和窄屏布局；实际来源样本也做过隔离验证。

这些证据不等于跨银行独立逐字段准确率，也不等于已完成 macOS、Linux 或任意新电脑上的全新安装验证。OCR 模型初始化成功与依赖检查通过，应与实际文件导入成功分别报告。

## 维护约束

- 原始底稿保持只读；人工修订追加保存，支持撤销和版本检查。
- 以 `bookstore.export_book()` 等有效记录入口作为后续整本分析输入，不跳过人工修订。
- 金额在后端精确计算；缺失值、未实现结果和计算为零分别表达。
- 读取缺口保留，不将失败文件和漏页解释为空数据。
- 私有账单、`.local-data/`、模型、运行环境、密钥及临时访问链接不提交。
- 修改读取或 OCR 政策时补充正反例，说明人工核验量和错误风险的权衡。
- 发布前检查实际文件清单；本地历史验收报告可能含私人样本信息，不作为公开用户文档。

接口及有效数据版本约定见 [API_STAGE1.md](API_STAGE1.md)，产品方向见 [ROADMAP.md](ROADMAP.md)。
