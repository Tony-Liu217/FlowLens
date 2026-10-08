"""Synthetic evidence of current mapping behavior, including known limitations.

Does not open or change user ledgers. A passed check is not bank compatibility certification.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from jiaowopay_ingest.mapping import find_header, document_profile
from jiaowopay_ingest.model import Row
from jiaowopay_ingest.normalize import normalize


def examine(name, headers, values, title='', expected=None):
    mapping = find_header([Row(headers, 1), Row(values, 2)])
    result = {'case': name, 'header_recognized': mapping is not None}
    if mapping:
        mapping = document_profile(mapping, title)
        record, issues = normalize(Row(values, 2), mapping)
        result.update(profile=mapping.profile, amount=record.get('amount'),
                      direction=record.get('direction'), errors=[i['code'] for i in issues if i['severity']=='error'])
    for key, value in (expected or {}).items():
        assert result.get(key) == value, result
    return result


def main():
    cases = [
        examine('reordered_columns', ['收支','交易金额','交易日期'], ['支出','￥123.00','20260101'], expected={'direction':'out','errors':[]}),
        examine('extra_unrelated_column', ['备注信息','交易日期','收支','金额'], ['新列','20260101','支出','123.00'], expected={'direction':'out','errors':[]}),
        examine('known_alias_and_spacing', [' 交易 日期 ','发生额','收 / 支'], ['20260101','＋￥123.00','收入'], expected={'direction':'in','errors':[]}),
        examine('unknown_amount_header', ['交易日期','本次动账数值','收支'], ['20260101','123.00','支出'], expected={'header_recognized':False}),
        examine('unknown_unsigned_semantics', ['交易日期','金额'], ['20260101','123.00'], expected={'direction':None,'errors':['DIRECTION_MISSING']}),
    ]
    headers = ['序号','摘要','交易日期','交易金额','账户余额','交易地点/附言','对方账号与户名']
    values = ['1','示例','20260101','123.00','200.00','','']
    title = '中国建设银行个人活期账户全部交易明细'
    cases.append(examine('known_ccb_unsigned_income', headers, values, title, {'direction':'in','errors':[]}))
    cases.append(examine('ccb_changed_title_falls_back', headers, values, '建设银行新版流水清单', {'profile':'generic.v1','direction':None}))
    cases.append(examine('ccb_missing_signature_column_falls_back', headers[:-1], values[:-1], title, {'profile':'generic.v1','direction':None}))
    # A deliberately hypothetical semantic change: same title/header, positive
    # number now means an expense. Existing code cannot detect that convention.
    limitation = examine('hypothetical_same_schema_changed_meaning', headers, values, title, {'direction':'in','errors':[]})
    limitation['hypothetical_expected_direction'] = 'out'
    limitation['known_blind_spot'] = True
    cases.append(limitation)
    print(json.dumps({'scope':'synthetic mapping behavior; includes intentional limitation', 'cases':cases}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
