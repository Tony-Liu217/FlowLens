import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jiaowopay_review.attention import field_attention
from jiaowopay_ocr.consensus import merge_observations
import test_ocr_integration as fixtures


class FieldAttentionTests(unittest.TestCase):
    def setUp(self):
        self.row={'record_id':'r','review_status':'pending','row':1,'transaction_at':'2026-01-01','amount':'0.01','direction':'out'}
        self.original={'raw_headers':['交易日期','收入/支出金额','余额'],
                       'mapping_columns':{'transaction_at':0,'signed_amount':1,'balance':2}}

    def test_amount_direction_locate_shared_raw_column_without_balance(self):
        result=fixtures.fixture('sha')
        result['observations']['dark_gray'][0]['direction']=None
        result['proposals'],_=merge_observations(result['observations'],{})
        result['observations']['original'][0]['field_scores']['balance']=[.1]
        original=copy.deepcopy(result)
        attention=field_attention(self.row,self.original,[],result)
        self.assertEqual(set(attention['fields']),{'amount','direction'})
        self.assertEqual([r['index'] for r in attention['raw_columns']],[1])
        self.assertEqual(result,original)

    def test_date_low_score_only_marks_date(self):
        result=fixtures.fixture('sha');result['observations']['original'][0]['field_scores']['date']=[.7]
        attention=field_attention(self.row,self.original,[],result)
        self.assertEqual(set(attention['fields']),{'transaction_at'})
        self.assertEqual([r['index'] for r in attention['raw_columns']],[0])

    def test_currency_default_and_balance_advisories_not_marked(self):
        issues=[{'record_id':'r','severity':'warning','code':'CURRENCY_DEFAULTED','field':'currency','message':'默认人民币'}]
        result=fixtures.fixture('sha');result['observations']['original'][0]['field_scores']['balance']=[.1]
        self.assertEqual(field_attention(self.row,self.original,issues,result)['fields'],{})

    def test_explicit_direction_column_is_not_an_amount_error(self):
        original={'raw_headers':['金额','日期','收支'], 'mapping_columns':{'amount':0,'transaction_at':1,'direction':2}}
        issues=[{'record_id':'r','severity':'error','code':'DIRECTION_UNKNOWN','field':'direction','message':'编码待核对'}]
        result=field_attention(self.row,original,issues)
        self.assertEqual(set(result['fields']),{'direction'})
        self.assertEqual(result['raw_columns'][0]['index'],2)

    def test_unsigned_direction_uses_amount_when_no_direction_column(self):
        issues=[{'record_id':'r','severity':'error','code':'DIRECTION_MISSING','field':'direction','message':'方向待定'}]
        result=field_attention(self.row,self.original,issues)
        self.assertEqual(set(result['fields']),{'direction'})
        self.assertEqual(result['raw_columns'][0]['index'],1)

    def test_ambiguous_columns_mark_both_candidates(self):
        original={'raw_headers':['金额','交易金额','日期'],'mapping_columns':{'transaction_at':2}}
        issues=[{'record_id':'r','severity':'error','code':'COLUMN_AMBIGUOUS','field':'amount','message':'列不明确'}]
        result=field_attention(self.row,original,issues)
        self.assertEqual([r['index'] for r in result['raw_columns']],[0,1])

    def test_row_problem_does_not_invent_field_location(self):
        issues=[{'record_id':'r','severity':'error','code':'ROW_WIDTH_MISMATCH','message':'行错位'}]
        result=field_attention(self.row,self.original,issues)
        self.assertEqual(result['fields'],{})
        self.assertEqual(result['unlocalized'],['行错位'])

    def test_confirmed_or_excluded_record_does_not_keep_stale_highlights(self):
        issues=[{'record_id':'r','severity':'error','code':'AMOUNT_INVALID','field':'amount','message':'旧金额有误'}]
        for status in ['confirmed','excluded','parsed']:
            self.assertEqual(field_attention({**self.row,'review_status':status},self.original,issues)['raw_columns'],[])

if __name__=='__main__':unittest.main()
