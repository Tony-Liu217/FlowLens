from __future__ import annotations

import copy
import hashlib
import re
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .mapping import Mapping, explicit_mapping, find_header, map_header, norm, text, document_profile
from .model import ReadFailure, digest, issue, raw_value
from .normalize import normalize, parse_date
from .readers import MAX_BYTES, read_document


def _non_transaction(values: list) -> str | None:
    cells = [text(v) for v in values if text(v)]
    if not cells:
        return "blank"
    first = norm(cells[0])
    if first in {"合计", "总计", "小计", "汇总", "收入合计", "支出合计"} or first.startswith(("交易记录合计", "共计", "温馨提示", "特别提示", "说明：")):
        return "footer"
    if all(set(c) <= set("-—=*") for c in cells):
        return "separator"
    return None


def _date_like(values):
    return any(parse_date(v)[0] for v in values)


def _location(segment, row):
    return {"segment": segment.name, "kind": segment.kind, "sheet": segment.name if segment.kind == "sheet" else None, "page": segment.page, "table": segment.table, "row": row.line, "end_row": row.end_line or row.line}


def import_files(paths, *, mappings: dict | None = None, allow_duplicate_files: bool = False,
                 ocr: bool = True, ocr_python=None, ocr_timeout: int = 180, progress=None) -> dict:
    """Read local files; optional local OCR only, no network or external AI.

    mappings: {file_sha256: {segment_name_or_star: explicit_mapping_config}}.
    An identical file is reported and skipped by default, never a transaction dedupe.
    """
    mappings = mappings or {}
    if not isinstance(mappings, dict):
        raise ValueError("映射配置必须是对象。")
    batch_id = uuid.uuid4().hex
    dataset = {
        "schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
        "batch_id": batch_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "config_hash": digest(mappings, allow_duplicate_files, ocr), "network_used": False,
        "files": [], "segments": [], "raw_rows": [], "records": [], "issues": [],
        "reviews": [], "assets": [], "_assets": {},
    }
    paths = list(paths)
    def report(stage, completed, filename, **details):
        if progress:
            progress(dict(stage=stage, completed=completed, total=len(paths), filename=filename, **details))

    occurrences = Counter()
    for file_index, given in enumerate(paths):
        path = Path(given)
        report('reading', file_index, path.name)
        file_info = {"filename": path.name, "sha256": None, "status": "failed", "format": None}
        dataset["files"].append(file_info)
        before_records, before_issues = len(dataset["records"]), len(dataset["issues"])
        try:
            if not path.is_file():
                raise ReadFailure("FILE_NOT_FOUND", "文件不存在或不是普通文件。")
            if path.stat().st_size > MAX_BYTES:
                raise ReadFailure("FILE_TOO_LARGE", "单文件超过 50 MiB，未读取。")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            occurrences[sha] += 1
            instance = digest(sha, occurrences[sha])
            file_info.update(sha256=sha, file_id=instance, occurrence=occurrences[sha], bytes=path.stat().st_size)
            if occurrences[sha] > 1 and not allow_duplicate_files:
                file_info["status"] = "duplicate_file_skipped"
                dataset["issues"].append(issue("DUPLICATE_FILE", "文件内容与本批次另一文件完全一致，已明确跳过；可显式允许重复导入。", "warning", file_id=instance))
                report('file_done', file_index + 1, path.name, outcome='duplicate_file_skipped')
                continue
            result = read_document(path)
            document_text = '\n'.join(text(v) for s in result.segments if s.kind == 'pdf_text' for row in s.rows for v in row.values)
            file_info.update(format=result.format, reader_metadata=result.metadata)
            dataset["issues"].extend({**p, "file_id": instance} for p in result.issues if not (ocr and p['code']=='OCR_REQUIRED'))
            previous_mapping, previous_layout, previous_page = None, None, None
            file_config = mappings.get(sha, {})
            if not isinstance(file_config, dict):
                raise ReadFailure("MAPPING_INVALID", "文件映射必须是按工作表或片段名称索引的对象。")
            for segment_index, segment in enumerate(result.segments):
                report('ocr' if segment.kind == 'scan' and ocr else 'standardizing', file_index,
                       path.name, segment=segment_index + 1, segments=len(result.segments), page=segment.page)
                if segment.kind=='scan' and ocr:
                    from .ocr import import_page
                    import_page(dataset,path,instance,sha,occurrences[sha],segment.page if result.format=='pdf' else None,
                                python=ocr_python,timeout=ocr_timeout)
                    continue
                header = find_header(segment.rows) if segment.kind not in {"pdf_text", "scan"} else None
                config = file_config.get(segment.name, file_config.get("*"))
                if config is not None:
                    try:
                        header = explicit_mapping(segment.rows, config)
                    except (ValueError, TypeError) as exc:
                        dataset["issues"].append(issue("MAPPING_INVALID", str(exc), file_id=instance, segment=segment.name))
                        header = None
                elif header is None and segment.kind == "pdf_table" and previous_mapping and segment.rows:
                    width = max(map(lambda r: len(r.values), segment.rows))
                    consecutive = segment.page in {previous_page, previous_page+1}
                    same_layout = len(segment.layout) == len(previous_layout) and all(abs(a-b) <= 3 for a,b in zip(segment.layout, previous_layout))
                    if width == len(previous_mapping.headers) and consecutive and same_layout and _date_like(segment.rows[0].values):
                        header = copy.deepcopy(previous_mapping)
                        header.start, header.depth, header.inherited = -1, 1, True
                        dataset["issues"].append(issue("HEADER_INHERITED", "同列几何布局的连续 PDF 表格沿用前页映射，每行仍独立校验。", "warning", file_id=instance, segment=segment.name))
                if header and segment.kind == 'pdf_table':
                    header = document_profile(header, document_text)
                segment_meta = {"file_id": instance, "name": segment.name, "kind": segment.kind, "page": segment.page, "table": segment.table, "physical_rows": len(segment.rows), "mapping": asdict(header) if header else None}
                dataset["segments"].append(segment_meta)
                if header and segment.kind == "pdf_table":
                    previous_mapping, previous_layout, previous_page = header, segment.layout, segment.page
                if header is None and segment.kind not in {"pdf_text", "scan"} and any(any(text(v) for v in row.values) for row in segment.rows):
                    tabular = any(sum(bool(text(v)) for v in r.values) >= 2 or _date_like(r.values) for r in segment.rows)
                    dataset["issues"].append(issue("HEADER_UNRECOGNIZED", "未定位可靠交易表头；所有行已保留，需指定表头与字段映射。" if tabular else "说明性片段已保留，没有识别为交易。", "error" if tabular else "warning", file_id=instance, segment=segment.name))
                for idx, row in enumerate(segment.rows):
                    location = _location(segment, row)
                    raw_id = digest(sha, occurrences[sha], location)
                    raw = {"raw_id": raw_id, "file_id": instance, "filename": path.name, **location, "values": raw_value(row.values), "cell_types": row.cell_types, "formats": row.formats, "formulas": row.formulas, "disposition": "unmapped"}
                    dataset["raw_rows"].append(raw)
                    structural = _non_transaction(row.values)
                    if structural:
                        raw["disposition"] = structural
                        continue
                    if header is None:
                        raw["disposition"] = "unmapped" if segment.kind != "pdf_text" else "document_text"
                        transaction_text = any(re.match(r"^\s*(?:\d+\s+)?20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+", text(v)) for v in row.values)
                        if segment.kind == "pdf_text" and (_date_like(row.values) or transaction_text):
                            dataset["issues"].append(issue("UNMAPPED_TEXT", "表格外存在日期内容，请检查是否遗漏交易。", file_id=instance, raw_id=raw_id))
                        continue
                    if header.start <= idx < header.start + header.depth:
                        raw["disposition"] = "header"
                        continue
                    if idx < header.start:
                        raw["disposition"] = "preamble"
                        if _date_like(row.values):
                            dataset["issues"].append(issue("DATA_BEFORE_HEADER", "表头之前存在日期值，请核对是否有未识别交易。", file_id=instance, raw_id=raw_id, severity="warning"))
                        continue
                    repeated = find_header([row])
                    if repeated:
                        header = document_profile(repeated, document_text) if segment.kind == 'pdf_table' else repeated
                        # This header is at idx, not the beginning of the segment.
                        header.start = idx
                        raw["disposition"] = "header"
                        continue
                    raw["disposition"] = "transaction"
                    record_id = digest(raw_id, PARSER_VERSION, asdict(header))
                    try:
                        rec, problems = normalize(row, header)
                    except (ValueError, TypeError, OverflowError):
                        raw["disposition"] = "unparsed_transaction"
                        dataset["issues"].append(issue("ROW_NORMALIZATION_FAILED", "此行类型或结构异常，原始值已保留；未停止其他记录的读取。", file_id=instance, raw_id=raw_id))
                        continue
                    rec.update(record_id=record_id, raw_id=raw_id, file_id=instance, filename=path.name, **location, mapping_columns=header.columns, raw_headers=header.headers)
                    dataset["records"].append(rec)
                    dataset["issues"].extend({**p, "file_id": instance, "record_id": record_id, "raw_id": raw_id} for p in problems)
            count = len(dataset["records"]) - before_records
            if count == 0:
                dataset["issues"].append(issue("NO_TRANSACTIONS", "未得到交易记录；不能视为已验证的空账单。", file_id=instance))
            file_issues = dataset["issues"][before_issues:]
            errors = any(p["severity"] == "error" for p in file_issues)
            file_info["status"] = "partial" if count and errors else "failed" if errors else "completed"
        except ReadFailure as exc:
            dataset["issues"].append(issue(exc.code, str(exc), file_id=file_info.get("file_id"), filename=path.name))
        except Exception as exc:
            dataset["issues"].append(issue("IMPORT_FAILED", f"导入失败（{type(exc).__name__}），未将失败解释为零交易。", file_id=file_info.get("file_id"), filename=path.name))
        file_records = dataset["records"][before_records:]
        file_info["record_count"] = len(file_records)
        file_info["ready_count"] = sum(r["parse_status"] == "ready" for r in file_records)
        report('file_done', file_index + 1, path.name, outcome=file_info['status'])
    counts = Counter(r["parse_status"] for r in dataset["records"])
    dataset["summary"] = {
        "file_count": len(dataset["files"]), "record_count": len(dataset["records"]),
        "ready_count": counts["ready"], "needs_review_count": counts["needs_review"],
        "raw_row_count": len(dataset["raw_rows"]), "error_count": sum(p["severity"] == "error" for p in dataset["issues"]),
        "warning_count": sum(p["severity"] == "warning" for p in dataset["issues"]),
        "duplicate_files_skipped": sum(f["status"] == "duplicate_file_skipped" for f in dataset["files"]),
        "transaction_deduplication": False,
        "review_item_count": len(dataset['reviews']), "evidence_file_count":len(dataset['assets']),
    }
    dataset["status"] = "needs_review" if dataset["summary"]["error_count"] else "completed"
    return dataset
