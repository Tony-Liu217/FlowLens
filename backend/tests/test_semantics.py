import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jiaowopay_ingest.mapping import Mapping, find_header
from jiaowopay_ingest.model import Row
from jiaowopay_ingest.normalize import normalize
from jiaowopay_ingest.semantics import enrich, searchable
from jiaowopay_review.effective_policy import apply_reading_policy
from jiaowopay_review.store import ReviewStore, EMPTY_STATE, checked_fields

CCB = ['序号','摘要','币别','钞汇','交易日期','交易金额','账户余额','交易地点/附言','对方账号与户名']
VALUES = ['1','消费','人民币','钞','20260801','-12.30','100.00','财付通-微信支付-合成商户','Z***0010/**商户']


class SemanticTests(unittest.TestCase):
    def test_ccb_full_context_without_identity_guess_or_financial_change(self):
        rec, problems = normalize(Row(VALUES, 2), find_header([Row(CCB, 1)]))
        self.assertEqual(rec['counterparty'], VALUES[7])
        self.assertEqual(rec['memo'], VALUES[7])
        self.assertEqual(rec['description'], '消费')
        self.assertEqual(rec['counterparty_combined'], VALUES[8])
        self.assertEqual(rec['source_fields'][8]['usage'], 'retained')
        self.assertEqual((rec['amount'], rec['direction'], rec['parse_status']), ('12.30', 'out', 'ready'))
        self.assertIn('counterparty', rec['derived_fields'])

    def test_boc_memo_summary_keeps_transaction_type(self):
        headers=['记账日期','记账时间','币别','金额','余额','交易名称','渠道','网点名称','附言','对方账户名','对方卡号/账号','对方开户行']
        values=['2026-08-01','10:20:30','人民币','-20','100','网上快捷支付','银企对接','支行说明','合成群收款','支付机构','123***','某行']
        rec,_=normalize(Row(values,2),find_header([Row(headers,1)]))
        self.assertEqual(rec['description'],'合成群收款')
        self.assertEqual(rec['trade_type'],'网上快捷支付')
        self.assertEqual(rec['payment_method'],'银企对接')
        self.assertEqual(rec['counterparty'],'支付机构')
        self.assertIn('网点名称',rec['unmapped_headers'])
        self.assertIn('支行说明',searchable(rec))

    def test_generic_or_user_mapping_does_not_guess_party(self):
        for confirmed in (False, True):
            mapping=Mapping({'transaction_at':0,'amount':1,'memo':2},['日期','金额','交易地点/附言'],0,confirmed=confirmed)
            rec,_=normalize(Row(['20260801','-2','北京市某地点'],2),mapping)
            self.assertIsNone(rec['counterparty'])
            self.assertEqual(rec['memo'],'北京市某地点')
        mapping=find_header([Row(CCB,1)])
        mapping.confirmed=True
        rec,_=normalize(Row(VALUES,2),mapping)
        self.assertIsNone(rec['counterparty'])

    def test_unknown_columns_and_placeholders(self):
        rec={}
        enrich(rec,['日期','新业务说明','空列'],{'transaction_at':0},['20260801','保留的线索','---'])
        self.assertEqual(rec['unmapped_headers'],['新业务说明'])
        self.assertIn('保留的线索',searchable(rec))
        self.assertEqual(len(rec['source_fields']),3)

    def test_human_correction_removes_stale_derived_label(self):
        rec,_=normalize(Row(VALUES,2),find_header([Row(CCB,1)]))
        corrected=checked_fields(rec,{'counterparty':'已核实商户'})
        self.assertNotIn('counterparty',corrected['derived_fields'])
        self.assertEqual(corrected['semantic_hints']['counterparty']['value'],VALUES[7])
        self.assertIn('counterparty',rec['derived_fields'])

    def test_old_overlay_and_human_blank_preserved(self):
        mapping=find_header([Row(CCB,1)])
        rec,_=normalize(Row(VALUES,2),mapping)
        for key in ('source_fields','semantic_policy','semantic_hints','derived_fields','unmapped_headers'):
            rec.pop(key,None)
        rec.update(counterparty=None,record_id='r',raw_id='raw',file_id='f',raw_headers=CCB,mapping_columns=mapping.columns)
        original=copy.deepcopy(rec)
        processed,_,_=apply_reading_policy({'r':rec},{'raw':{'values':VALUES}},[],{},Path('.'))
        self.assertEqual(rec,original)
        self.assertEqual(processed['r']['counterparty'],VALUES[7])
        store=object.__new__(ReviewStore)
        store.processing_records=processed; store.originals={'r':rec};store.tasks={}
        state=copy.deepcopy(EMPTY_STATE)
        state['overrides']['r']={**rec,'counterparty':None,'description':'人工摘要','human_confirmed':True}
        effective=store._rows(state)['r']
        self.assertIsNone(effective['counterparty'])
        self.assertEqual(effective['description'],'人工摘要')
        self.assertEqual(effective['semantic_hints']['counterparty']['value'],VALUES[7])
        self.assertIn('合成商户',searchable(effective))
        self.assertEqual(effective['amount'],original['amount'])


if __name__ == '__main__':
    unittest.main()
