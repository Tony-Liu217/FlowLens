from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .pipeline import import_files
from .storage import save_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description="交我Pay 本地导入标准化：不去重、不配对、不调用外部 AI")
    parser.add_argument("files", nargs="+", help="明确指定账单路径，不会扫描无关问卷或报表")
    parser.add_argument("--output", help="新数据包目录；已存在时拒绝覆盖")
    parser.add_argument("--mapping", help="用户确认的映射 JSON 配置")
    parser.add_argument("--allow-duplicate-files", action="store_true", help="显式保留重复文件的独立导入实例")
    parser.add_argument("--no-excel", action="store_true", help="仅保存 SQLite 和 JSON 数据")
    parser.add_argument('--no-ocr',action='store_true',help='禁用扫描页/图片本地 OCR')
    parser.add_argument('--ocr-python',help='明确指定已安装本地 OCR 依赖的 Python 路径')
    args = parser.parse_args(argv)
    try:
        mappings = json.loads(Path(args.mapping).read_text(encoding="utf-8-sig")) if args.mapping else None
        if args.output and Path(args.output).exists():
            raise ValueError('输出目录已存在，拒绝覆盖。')
        dataset = import_files(args.files, mappings=mappings, allow_duplicate_files=args.allow_duplicate_files,
                               ocr=not args.no_ocr,ocr_python=args.ocr_python)
        output = args.output or str(Path(__file__).resolve().parents[1]/".local-data"/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+dataset["batch_id"][:8]))
        target = save_bundle(dataset, output, excel=not args.no_excel)
    except (OSError, ValueError) as exc:
        print(f"未完成：{exc}")
        return 1
    summary = dataset["summary"]
    print(f"已保存：{target.resolve()}")
    print(f"文件 {summary['file_count']}，候选交易 {summary['record_count']}，已识别 {summary['ready_count']}，待确认 {summary['needs_review_count']}。")
    print("未做交易去重、配对、分类推断或网络调用。")
    if summary.get('review_item_count'):
        print(f"已保存 {summary['review_item_count']} 项 OCR 核验任务及截图；可打开数据包中的 review.html 只读查看。")
    if dataset["status"] != "completed":
        print("部分文件或记录需要处理，请查看“异常与待确认”及 manifest.json。退出码 2 表示数据已保存但尚未全部通过。")
        return 2
    return 0
