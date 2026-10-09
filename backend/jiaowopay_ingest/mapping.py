"""Versioned structural profiles and conservative exact header matching."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .model import Row
from .profiles import matching_profiles


def text(value) -> str:
    return "" if value is None else str(value).strip()


def norm(value) -> str:
    value = unicodedata.normalize("NFKC", text(value)).lower()
    return re.sub(r"[\s/:：()（）\[\]【】*、·_\-]+", "", value)


ALIASES = {
    "transaction_at": ["交易时间", "交易日期", "日期时间", "支付时间", "付款时间", "交易日期时间", "datetime", "transaction date", "date", "日期"],
    "transaction_time": ["时间", "交易时刻", "time"],
    "booking_at": ["记账日期", "入账日期", "记账日期时间", "入账时间", "posting date", "booking date"],
    "booking_time": ["记账时间", "入账时刻"],
    "amount": ["金额", "金额(元)", "交易金额", "交易金额(元)", "发生金额", "发生额", "收支金额", "支付金额", "amount", "原始金额"],
    "signed_amount": ["原始有符号金额", "有符号金额", "signed amount"],
    "credit_amount": ["收入金额", "收入", "贷方金额", "贷方发生额", "存入金额", "收入(元)", "credit amount", "credit"],
    "debit_amount": ["支出金额", "支出", "借方金额", "借方发生额", "支取金额", "支出(元)", "debit amount", "debit"],
    "direction": ["收/支", "收支", "收支方向", "借贷标志", "收支标识", "原始方向", "direction"],
    "currency": ["币别", "币种", "currency"],
    "counterparty": ["交易对方", "对方账户名", "对方户名", "商户名称", "对方", "收款方", "付款方", "counterparty"],
    "counterparty_account": ["对方账号", "对方卡号/账号", "对方账户", "counterparty account"],
    "counterparty_combined": ["对方账号与户名"],
    "counterparty_bank": ["对方开户行"],
    "description": ["商品", "商品说明", "商品/说明", "摘要", "交易摘要", "用途", "description"],
    "memo": ["备注", "附言", "交易地点/附言", "交易附言", "附言/备注", "memo", "remark"],
    "payment_method": ["支付方式", "收/付款方式", "付款方式", "渠道", "交易渠道", "支付方式/渠道"],
    "transaction_status": ["当前状态", "交易状态", "状态", "status"],
    "trade_type": ["交易类型", "交易名称", "trade type"],
    "original_category": ["交易分类", "原始分类", "category"],
    "source_record_id": ["交易单号", "交易订单号", "流水号", "订单号", "原始流水号", "reference", "transaction id"],
    "merchant_order_no": ["商户单号", "商家订单号", "商户订单号"],
    "balance": ["余额", "账户余额", "balance"],
    "account": ["账户", "本方账号", "本方账户", "account"],
}
NORMAL_ALIASES = {key: {norm(v) for v in values} for key, values in ALIASES.items()}
MONEY_FIELDS = {"amount", "signed_amount", "credit_amount", "debit_amount"}


@dataclass
class Mapping:
    columns: dict[str, int]
    headers: list
    start: int
    depth: int = 1
    ambiguous: dict = field(default_factory=dict)
    profile: str = "generic.v1"
    source: str | None = None
    currency: str | None = None
    signed: bool = False
    direction_codes: dict = field(default_factory=dict)
    confirmed: bool = False
    inherited: bool = False


def map_header(headers: list) -> tuple[dict, dict]:
    cells = [norm(v) for v in headers]
    columns, ambiguous = {}, {}
    for key, aliases in NORMAL_ALIASES.items():
        matches = [i for i, h in enumerate(cells) if h and h in aliases]
        if len(matches) == 1:
            columns[key] = matches[0]
        elif matches:
            ambiguous[key] = matches
    # A separate date + 交易时间 pair is not an ambiguous date column.
    dates = [i for i,h in enumerate(cells) if h in {"交易日期", "日期"}]
    times = [i for i,h in enumerate(cells) if h == "交易时间"]
    if len(dates) == len(times) == 1:
        columns.update(transaction_at=dates[0], transaction_time=times[0])
        ambiguous.pop("transaction_at", None)
    return columns, ambiguous


def _profile(mapping: Mapping) -> Mapping:
    matches = matching_profiles({norm(v) for v in mapping.headers})
    # Preserve legacy precedence in production until ambiguous-format review ships.
    if matches:
        p = matches[0]
        mapping.profile, mapping.source = p.id, p.label
        mapping.currency, mapping.signed = p.currency, p.signed
    return mapping


def find_header(rows: list[Row]) -> Mapping | None:
    best, best_score = None, -1
    for i, row in enumerate(rows[:200]):
        for depth in (1, 2):
            if depth == 2:
                if i+1 >= len(rows):
                    continue
                other = rows[i+1].values
                headers = []
                previous = ""
                for j in range(max(len(row.values), len(other))):
                    top = text(row.values[j]) if j < len(row.values) else ""
                    bottom = text(other[j]) if j < len(other) else ""
                    # Limited parent carry only for explicit income/expense groups.
                    if top:
                        previous = top
                    if not top and previous in ("收入", "支出", "借方", "贷方"):
                        top = previous
                    headers.append(top + bottom if bottom and top != bottom else top or bottom)
            else:
                headers = row.values
            columns, ambiguous = map_header(headers)
            fields = set(columns) | set(ambiguous)
            if not fields & {"transaction_at", "booking_at"} or not fields & MONEY_FIELDS:
                continue
            score = len(columns) + 0.25 * len(ambiguous) + 0.1*(depth-1)
            if score > best_score:
                best_score = score
                best = _profile(Mapping(columns, list(headers), i, depth, ambiguous))
    return best


def document_profile(mapping: Mapping, document_text: str) -> Mapping:
    """Recognize a bank document heading and table together, never its filename."""
    matches = matching_profiles({norm(v) for v in mapping.headers}, norm(document_text))
    if not mapping.confirmed and mapping.profile == 'generic.v1' and matches:
        p = matches[0]
        mapping.profile, mapping.source, mapping.signed = p.id, p.label, p.signed
    return mapping


def explicit_mapping(rows: list[Row], config: dict) -> Mapping:
    allowed = {"header_row", "columns", "currency", "amount_signed", "direction_codes", "source"}
    if set(config) - allowed:
        raise ValueError("映射配置包含不支持的字段。")
    row_number = config.get("header_row")
    if type(row_number) is not int:
        raise ValueError("header_row 必须是原始行号整数。")
    start = next((i for i,r in enumerate(rows) if r.line == row_number), None)
    if start is None:
        raise ValueError("指定表头行不存在。")
    columns = config.get("columns", {})
    if not isinstance(columns, dict) or any(k not in ALIASES or type(v) is not int or not 0 <= v < len(rows[start].values) for k,v in columns.items()):
        raise ValueError("columns 必须使用支持的字段和从 0 开始的有效列号。")
    if len(set(columns.values())) != len(columns):
        raise ValueError("不同字段不能指向同一列。")
    if not set(columns) & {"transaction_at", "booking_at"} or not set(columns) & MONEY_FIELDS:
        raise ValueError("映射必须包含日期和金额字段。")
    codes = config.get("direction_codes", {})
    if not isinstance(codes, dict) or any(v not in {"in", "out", "none"} for v in codes.values()):
        raise ValueError("direction_codes 的结果必须是 in、out 或 none。")
    if type(config.get("amount_signed", False)) is not bool:
        raise ValueError("amount_signed 必须是布尔值。")
    return Mapping(dict(columns), rows[start].values, start, profile="user.confirmed.v1", source=config.get("source"), currency=config.get("currency"), signed=config.get("amount_signed", False), direction_codes=codes, confirmed=True)
