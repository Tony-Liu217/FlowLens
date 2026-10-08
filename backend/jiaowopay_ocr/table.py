"""Conservative OCR row extraction. No bank coordinates or truth inputs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
import statistics
import unicodedata
from jiaowopay_ingest.normalize import money_parts


def clean(text):
    text = unicodedata.normalize("NFKC", str(text))
    return re.sub(r"\s+", "", text).replace("−", "-").replace("—", "-").replace("–", "-")


def field(text):
    text = clean(text)
    return {"交易日期":"date", "交易时间":"date", "日期":"date",
            "收入/支出金额":"signed", "收入支出金额":"signed", "交易金额":"signed",
            "支出金额":"debit", "收入金额":"credit", "余额":"balance", "账户余额":"balance",
            "币种":"currency", "币别":"currency", "账号":"account", "本方账号":"account",
            "摘要":"description", "交易摘要":"description", "对方户名":"counterparty",
            "对方账号":"counterparty_account", "渠道":"payment_method", "流水号":"source_record_id"}.get(text)


DATE = re.compile(r"(?<!\d)(20\d{2})[-/](\d{2})[-/](\d{2})(?!\d)")
TIME = re.compile(r"(?<!\d)(\d{2}):(\d{2}):(\d{2})(?!\d)")


def date_value(text):
    # A damaged time must not quietly become a valid day-only transaction.
    text = clean(text)
    m = re.fullmatch(r'(20\d{2})[-/](\d{2})[-/](\d{2})(?:(\d{2}):(\d{2}):(\d{2}))?',text)
    if not m:
        return None
    value = '-'.join(m.groups()[:3])
    if m.group(4):
        value += ' '+':'.join(m.groups()[3:])
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def money(text):
    value, _ = money_parts(text)
    # Preserve the OCR extractor's two-decimal limit; never round extra digits.
    return value if value is not None and value.as_tuple().exponent >= -2 else None


def record(values, scores=None):
    issues = []
    amount = direction = None
    if "signed" in values:
        raw = values["signed"]
        number = money(raw)
        if number is not None:
            amount = f"{abs(number):.2f}"
            sign = money_parts(raw)[1]
            direction = 'out' if sign == '-' else 'in' if sign == '+' else None
    else:
        credit, debit = money(values.get("credit", "")), money(values.get("debit", ""))
        invalid = any(clean(values.get(k, '')) not in {'', '-', '--', '/', '无'} and money(values[k]) is None for k in ['credit','debit'])
        if invalid:
            issues.append('AMOUNT_UNREADABLE')
        elif (credit is not None and credit<0) or (debit is not None and debit<0):
            issues.append('SPLIT_AMOUNT_NEGATIVE')
        elif credit and not debit:
            amount, direction = f"{abs(credit):.2f}", "in"
        elif debit and not credit:
            amount, direction = f"{abs(debit):.2f}", "out"
        elif credit == 0 or debit == 0:
            amount, direction = '0.00', 'none'
        else:
            issues.append("AMBIGUOUS_DUAL_AMOUNT")
    when = date_value(values.get("date", ""))
    balance = money(values.get("balance", ""))
    if amount is None:
        issues.append("AMOUNT_UNREADABLE")
    if direction is None:
        issues.append("DIRECTION_UNKNOWN")
    if when is None:
        issues.append("DATE_UNREADABLE")
    if balance is None:
        issues.append("BALANCE_UNREADABLE")
    if scores and min(scores) < .9:
        issues.append("LOW_OCR_SCORE")
    return {"transaction_at":when,"amount":amount,"direction":direction,
            "balance":f"{balance:.2f}" if balance is not None else None,
            "issues":issues,"raw_fields":values}


def validate_chain(records):
    # This benchmark's account statements are known chronological/single-currency.
    # Do not use this assumption as a general bank-product rule.
    for prev, cur in zip(records, records[1:]):
        if all(x is not None for x in [prev["balance"],cur["balance"],cur["amount"],cur["direction"]]):
            delta = Decimal(cur["amount"])*(1 if cur["direction"] == "in" else -1)
            if Decimal(prev["balance"])+delta != Decimal(cur["balance"]):
                for r in [prev,cur]:
                    if "BALANCE_CHAIN_CONFLICT" not in r["issues"]:
                        r["issues"].append("BALANCE_CHAIN_CONFLICT")
    return records


def from_tokens(raw, confirmed_columns=None, header_floor=None, *, with_geometry=False):
    if raw.get("error"):
        return [], ["ENGINE_FAILED"]
    tokens = []
    for box,text,score in zip(raw.get("boxes") or [], raw.get("texts") or [], raw.get("scores") or []):
        xs,ys = zip(*box)
        tokens.append({"text":text,"score":score,"x":(min(xs)+max(xs))/2,
                       "y":(min(ys)+max(ys))/2,"h":max(ys)-min(ys), "w":max(xs)-min(xs)})
    labels={}
    if confirmed_columns is None:
        date_headers = [t for t in tokens if field(t['text']) == "date"]
        if not date_headers:
            return [], ["HEADER_UNREADABLE"]
        header = min(date_headers, key=lambda t:t['y'])
        heads = sorted([t for t in tokens if abs(t['y']-header['y']) < max(header['h'],t['h'])*.7], key=lambda t:t['x'])
        columns = {}
        for i,t in enumerate(heads):
            f = field(t['text'])
            if f:
                labels[f]=t['text']
                if f in columns:
                    return [], ["HEADER_AMBIGUOUS"]
                columns[f] = ((heads[i-1]['x']+t['x'])/2 if i else float('-inf'),
                              (t['x']+heads[i+1]['x'])/2 if i+1 < len(heads) else float('inf'))
    else:
        # Diagnostic only: independently confirmed page columns, never amounts/dates.
        # Report separately from automatic recognition, NOT as automatic accuracy.
        columns = confirmed_columns
        header = {'y':header_floor, 'h':0}
    if not ("signed" in columns or {"credit","debit"} <= columns.keys()):
        return [], ["AMOUNT_HEADER_UNREADABLE"]
    left,right = columns['date']
    dates = sorted([t for t in tokens if left <= t['x'] < right and t['y'] > header['y']+header['h']*.8 and date_value(t['text'])], key=lambda t:t['y'])
    if not dates:
        return [], ["NO_DATE_ROWS"]
    gaps = [b['y']-a['y'] for a,b in zip(dates,dates[1:])]
    gap = statistics.median(gaps) if gaps else dates[0]['h']*2.8
    records = []
    for i,t in enumerate(dates):
        low = (dates[i-1]['y']+t['y'])/2 if i else t['y']-gap/2
        high = (t['y']+dates[i+1]['y'])/2 if i+1<len(dates) else t['y']+gap/2
        values,scores,field_scores = {},[],{}
        for f,(left,right) in columns.items():
            cells = sorted([v for v in tokens if left <= v['x'] < right and low <= v['y'] < high], key=lambda v:(round(v['y']/8), v['x']))
            values[f] = " ".join(c['text'] for c in cells)
            scores += [c['score'] for c in cells]
            field_scores[f] = [c['score'] for c in cells]
        row = record(values,scores)
        if with_geometry:
            row['column_labels']=labels
            row['field_scores'] = field_scores
            row['source_geometry'] = {'date_x':t['x'], 'date_y':t['y'],
                                      'date_width':t['w'], 'row_height':high-low,
                                      'row_y_bounds':[low,high],
                                      'columns':{f:[l if abs(l)!=float('inf') else None,
                                                    r if abs(r)!=float('inf') else None]
                                                 for f,(l,r) in columns.items()}}
        records.append(row)
    # Bank statements need not be ascending/single-account. Integration applies
    # its own explicit safety policy instead of the old benchmark assumption.
    return records, []


def recover_currency_column(raw, observations):
    """Recover ONLY an auxiliary header location from two agreeing views.

    Currency text itself is read from untouched original OCR tokens. No bank
    defaults, fixed coordinates, or OCR alias substitutions are used.
    """
    from .consensus import match_rows
    base=observations.get('original',[])
    if not base or 'currency' in base[0]['raw_fields']:
        return None
    routes=[]
    for name in ['dark_gray','dark_neutral']:
        matched=match_rows(base,observations.get(name,[]))
        if matched:
            bounds=matched[0]['source_geometry']['columns'].get('currency')
            if bounds and None not in bounds:
                routes.append(bounds)
    if len(routes)!=2 or any(abs(a-b)>3 for a,b in zip(*routes)):
        return None
    left,right=routes[0]
    for row in base:
        low,high=row['source_geometry']['row_y_bounds']
        cells=[]
        for box,text,score in zip(raw.get('boxes') or [],raw.get('texts') or [],raw.get('scores') or []):
            xs,ys=zip(*box)
            x,y=(min(xs)+max(xs))/2,(min(ys)+max(ys))/2
            if left<=x<right and low<=y<high:
                cells.append((y,x,text,score))
        cells.sort()
        row['raw_fields']['currency']=' '.join(t for _,_,t,_ in cells)
        row['field_scores']['currency']=[s for *_,s in cells]
        row['source_geometry']['columns']['currency']=[left,right]
        row['column_provenance']={'currency':'two_derived_headers_original_body'}
    return {'field':'currency','bounds':[left,right], 'method':'two_derived_headers_original_body'}


class TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.complex_span = False
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row = []
        if tag in {"td","th"}:
            self.cell = []
            if any(k in {"rowspan","colspan"} and v not in {None,"1"} for k,v in attrs):
                self.complex_span = True
        if tag == "br" and self.cell is not None: self.cell.append(" ")
    def handle_data(self,data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in {"td","th"} and self.row is not None and self.cell is not None:
            self.row.append("".join(self.cell)); self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row); self.row = None


def from_tables(raw):
    records,issues = [],[]
    for table in raw.get("tables",[]):
        parser = TableHTML()
        parser.feed(table['html'])
        # Never silently flatten merged cells, which can shift amounts into wrong columns.
        if parser.complex_span:
            issues.append("MERGED_CELLS_UNSUPPORTED_BY_BENCHMARK_ADAPTER")
            continue
        mapping = None
        for row in parser.rows:
            fields = {field(v):i for i,v in enumerate(row) if field(v)}
            if "date" in fields and ("signed" in fields or {"debit","credit"} <= fields.keys()):
                mapping = fields
                continue
            if mapping is None:
                continue
            values = {f:row[i] if i<len(row) else "" for f,i in mapping.items()}
            if date_value(values['date']):
                records.append(record(values))
    if not records:
        issues.append("NO_NATIVE_TABLE_ROWS")
    return validate_chain(records),issues
