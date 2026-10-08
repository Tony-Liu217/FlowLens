from __future__ import annotations

import re
import math
import unicodedata
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from .mapping import Mapping, norm, text
from .model import Row, issue

EMPTY = {"", "-", "--", "—", "/", "无", "N/A", "null"}


def money_parts(value):
    """Return value and explicit sign; currency decoration never supplies direction."""
    if value is None or isinstance(value, bool) or text(value) in EMPTY:
        return None, None
    s = unicodedata.normalize("NFKC", str(value)).strip().replace("−", "-")
    s = re.sub(r"(?:元|人民币)$", "", s).strip()
    parentheses = s.startswith('(') and s.endswith(')')
    if parentheses:
        s = s[1:-1].strip()
    currency = r'(?:人民币|CNY|RMB|USD|EUR|HKD|JPY|GBP|CHF|AUD|CAD|SGD|NZD|KRW|TWD|MOP|[¥$€£])'
    # Only accept unambiguous dot-decimal / correctly grouped comma thousands.
    match = re.fullmatch(rf'(?P<c1>{currency})?\s*(?P<sign>[+-]|负)?\s*(?P<c2>{currency})?\s*(?P<number>(?:\d+|\d{{1,3}}(?:,\d{{3}})+)(?:\.\d+)?)(?P<tail>-)?', s, re.I)
    if not match or match['c1'] and match['c2']:
        return None, None
    if sum([parentheses, bool(match['sign']), bool(match['tail'])]) > 1:
        return None, None
    sign = '-' if parentheses or match['tail'] or match['sign'] in {'-', '负'} else match['sign']
    try:
        result = Decimal(match['number'].replace(',', ''))
    except InvalidOperation:
        return None, None
    if not result.is_finite() or abs(result) >= Decimal("1e15"):
        return None, None
    return (-result if sign == '-' else result), sign


def parse_money(value) -> Decimal | None:
    return money_parts(value)[0]


def money_currency(value):
    if parse_money(value) is None:
        return None
    token = unicodedata.normalize('NFKC', str(value)).upper()
    match = re.search(r'人民币|CNY|RMB|USD|EUR|HKD|JPY|GBP|CHF|AUD|CAD|SGD|NZD|KRW|TWD|MOP|[€£]', token)
    # A bare $ or yen sign is not a unique ISO currency identifier.
    return {'人民币':'CNY','RMB':'CNY','€':'EUR','£':'GBP'}.get(match[0],match[0]) if match else None


def parse_date(value) -> tuple[str | None, str | None]:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds"), "second"
    if isinstance(value, date):
        return value.isoformat(), "day"
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        # Excel serials must have been resolved by a reader using cell styles/epoch.
        if not math.isfinite(value) or int(value) != value or not 19000101 <= value <= 21991231:
            return None, None
        value = str(int(value))
    s = text(value).replace("\n", "").replace("T", " ")
    formats = [("%Y%m%d", "day"), ("%Y-%m-%d", "day"), ("%Y/%m/%d", "day"), ("%Y.%m.%d", "day"), ("%Y年%m月%d日", "day"), ("%Y%m%d%H%M%S", "second")]
    for d in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y年%m月%d日"):
        formats.extend([(d+" %H:%M:%S", "second"), (d+" %H:%M", "minute")])
    for fmt, precision in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            return (parsed.date().isoformat() if precision == "day" else parsed.isoformat(sep=" ")), precision
        except ValueError:
            continue
    return None, None


def parse_time(value) -> tuple[str | None, str | None]:
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        return value.isoformat(timespec="seconds"), "second"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 0 <= value < 1:
            seconds = round(value * 86400)
            if seconds >= 86400:
                return None, None
            return f"{seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02}", "second"
        return None, None
    s = re.sub(r"\s", "", text(value))
    for fmt, precision in (("%H:%M:%S", "second"), ("%H:%M", "minute"), ("%H%M%S", "second")):
        try:
            return datetime.strptime(s, fmt).time().isoformat(), precision
        except ValueError:
            continue
    return None, None


def normalize(row: Row, mapping: Mapping) -> tuple[dict, list[dict]]:
    problems = []

    def add(code, message, field=None, severity="error"):
        problems.append(issue(code, message, severity, field=field))

    def get(key):
        index = mapping.columns.get(key)
        return row.values[index] if index is not None and index < len(row.values) else None

    result = {}
    for field in ("transaction_at", "booking_at"):
        value, precision = parse_date(get(field))
        time_field = "transaction_time" if field == "transaction_at" else "booking_time"
        if field in mapping.columns and value is None:
            add("DATE_INVALID", "日期缺失或格式无法可靠解释。", field)
        if time_field in mapping.columns and text(get(time_field)) not in EMPTY:
            clock, time_precision = parse_time(get(time_field))
            if clock and value:
                if precision != "day" and value[11:] != clock:
                    add("TIME_CONFLICT", "完整日期时间与独立时间列冲突。", field)
                else:
                    value, precision = value[:10] + " " + clock, time_precision
            else:
                add("TIME_INVALID", "时间列无法解析，未补为零点。", field)
        result[field], result[field + "_precision"] = value, precision
    if not result["transaction_at"] and not result["booking_at"]:
        add("DATE_MISSING", "没有可用的交易日期或记账日期。")

    values = {k: parse_money(get(k)) for k in ("amount", "signed_amount", "credit_amount", "debit_amount")}
    for k,v in values.items():
        if k in mapping.columns and text(get(k)) not in EMPTY and v is None:
            add("AMOUNT_INVALID", "金额格式不明确或不是有限数值。", k)
    amount, derived = values["amount"], None
    amount_direction = None
    if amount is not None and (mapping.signed or amount < 0 or money_parts(get('amount'))[1] is not None):
        amount_direction = 'out' if amount < 0 else 'in' if amount > 0 else 'none'
    signed = values["signed_amount"]
    if signed is not None:
        if amount is not None and abs(amount) != abs(signed):
            add("AMOUNT_CONFLICT", "金额列与有符号金额列不一致。", "amount")
        amount = signed
        derived = "out" if signed < 0 else "in" if signed > 0 else "none"
        if amount_direction and amount_direction != derived:
            add('DIRECTION_CONFLICT', '金额列与有符号金额列的方向相互矛盾。', 'direction')
    credit, debit = values["credit_amount"], values["debit_amount"]
    if {"credit_amount", "debit_amount"} & set(mapping.columns):
        if (credit is not None and credit < 0) or (debit is not None and debit < 0):
            add("SPLIT_AMOUNT_NEGATIVE", "收支分列含负数，需要确认冲正语义。", "amount")
        if credit and debit:
            add("BOTH_DIRECTIONS", "同一行收入和支出同时非零，未合并抵消。", "amount")
            amount = None
        elif credit is not None or debit is not None:
            split = credit if credit else debit if debit else Decimal(0)
            if amount is not None and abs(amount) != abs(split):
                add("AMOUNT_CONFLICT", "总金额与收支分列金额不一致。", "amount")
            split_direction = "in" if credit else "out" if debit else "none"
            if (derived or amount_direction) and (derived or amount_direction) != split_direction:
                add('DIRECTION_CONFLICT', '收支分列与有符号金额的方向相互矛盾。', 'direction')
            amount = split
            derived = split_direction
    raw_direction = text(get("direction"))
    codes = {"收入": "in", "收": "in", "收款": "in", "入账": "in", "转入": "in", "in": "in", "income": "in", "支出": "out", "支": "out", "付款": "out", "消费": "out", "扣款": "out", "转出": "out", "out": "out", "expense": "out", "不计收支": "none", "不计": "none", "/": "none", "中性交易": "none", "none": "none"}
    codes.update({text(k).lower(): v for k,v in mapping.direction_codes.items()})
    direction = codes.get(raw_direction.lower())
    if raw_direction and direction is None:
        add("DIRECTION_UNKNOWN", "收支编码无法确定，需明确借贷等编码的账户视角。", "direction")
    if amount is not None:
        if derived is None and (mapping.signed or amount < 0 or money_parts(get('amount'))[1] is not None):
            derived = "out" if amount < 0 else "in" if amount > 0 else "none"
        if direction and derived and direction != derived and direction != "none":
            add("DIRECTION_CONFLICT", "方向字段与金额方向证据不一致。", "direction")
        direction = direction or derived
    if direction is None:
        add("DIRECTION_MISSING", "无法可靠判断收支方向，未将正金额默认当收入。", "direction")
    if amount is None:
        add("AMOUNT_MISSING", "没有可用的交易金额，未补为零。", "amount")
    result.update(amount=format(abs(amount), "f") if amount is not None else None, direction=direction, signed_amount=format(amount, "f") if amount is not None and (mapping.signed or signed is not None) else None, raw_direction=raw_direction or None)

    currency_raw = text(get("currency")) or mapping.currency
    currencies = {"人民币": "CNY", "人民币元": "CNY", "RMB": "CNY", "CNY": "CNY", "美元": "USD", "USD": "USD", "欧元": "EUR", "EUR": "EUR", "港币": "HKD", "港元": "HKD", "HKD": "HKD", "日元": "JPY", "JPY": "JPY"}
    currencies.update({code:code for code in ['GBP','CHF','AUD','CAD','SGD','NZD','KRW','TWD','MOP']})
    currency = currencies.get(text(currency_raw).upper())
    amount_currencies = {money_currency(get(key)) for key in values} - {None}
    currency_evidence = amount_currencies | ({currency} if currency else set())
    if len(currency_evidence) > 1:
        add('CURRENCY_CONFLICT', '金额中的币种标识与币种列或其他金额列冲突，请核对。', 'currency')
    elif currency is None and amount_currencies:
        currency = next(iter(amount_currencies))
    if currency is None and len(currency_evidence) <= 1:
        currency = "CNY"
        add("CURRENCY_DEFAULTED", "币种缺失或未识别，按默认人民币处理；原始字段保留。", "currency", "warning")
    result["currency"] = currency
    for key in ("counterparty", "counterparty_account", "counterparty_bank", "counterparty_combined", "description", "memo", "payment_method", "trade_type", "original_category", "source_record_id", "merchant_order_no", "account"):
        value = get(key)
        if key in {"source_record_id", "merchant_order_no", "counterparty_account", "account"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            # Text in the source is authoritative. Numeric storage may have already lost digits.
            add("IDENTIFIER_NUMERIC", "标识符以数字存储，无法保证前导零和长数字精度，请核对原账单。", key)
            result[key] = str(int(value)) if math.isfinite(value) and int(value) == value else str(value)
        else:
            result[key] = text(value) or None
    balance = parse_money(get("balance"))
    result["balance"] = format(balance, "f") if balance is not None else None
    status = text(get("transaction_status"))
    result["raw_status"] = status or None
    if not status:
        normalized_status = "unknown"
    elif any(k in status for k in ("交易关闭", "已关闭", "已撤销", "失败", "取消")):
        normalized_status = "void"
    elif any(k in status for k in ("待支付", "处理中", "待付款", "进行中")):
        normalized_status = "pending"
    elif any(k in status for k in ("成功", "已入账", "已收款", "对方已收钱", "已存入", "已转账", "已到账", "已支出", "已支付", "退款")):
        normalized_status = "posted"
    else:
        normalized_status = "unknown"
        add("STATUS_UNKNOWN", "原交易状态未映射，已保留待核对。", "transaction_status", "warning")
    result["transaction_status"] = normalized_status
    for key, indices in mapping.ambiguous.items():
        add("COLUMN_AMBIGUOUS", "同一字段有多个候选列，需要确认映射。", key)
    for key, index in mapping.columns.items():
        if index in row.formulas:
            add("FORMULA_CELL", "源字段来自公式，缓存结果未独立验证。", key)
        if index < len(row.cell_types) and row.cell_types[index] == "e":
            add("CELL_ERROR", "源单元格包含错误。", key)
    if len(row.values) > len(mapping.headers) and any(text(v) for v in row.values[len(mapping.headers):]):
        add("ROW_WIDTH_MISMATCH", "数据行列数超过表头，可能发生字段错位。")
    result["parse_status"] = "needs_review" if any(p["severity"] == "error" for p in problems) else "ready"
    result["mapping_method"] = "user_confirmed" if mapping.confirmed else "profile" if mapping.profile != "generic.v1" else "exact_alias"
    result["profile"] = mapping.profile
    result["source"] = mapping.source or "未知来源"
    return result, problems
