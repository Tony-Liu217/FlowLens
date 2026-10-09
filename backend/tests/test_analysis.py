import copy
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis.core import build, fingerprint, merge
from analysis.store import AnalysisStore
from analysis.privacy import Protector
from analysis.ai import checked
from jiaowopay_review.store import Conflict,ReviewError


def row(rid,source='微信',**kw):
    return {'book_record_id':rid,'record_id':rid,'batch_id':'batch','file_id':source,
            'source':source,'amount':'58.00','currency':'CNY','direction':'out',
            'transaction_at':'2026-01-01 12:00:00','transaction_at_precision':'second',
            'counterparty':'某超市','description':'商品详情','transaction_status':'posted',
            'payment_method':'建设银行储蓄卡(1234)','source_record_id':'order0001',**kw}


def bank(rid='bank',**kw):
    return row(rid,'建设银行',counterparty='财付通',description='消费',memo='财付通-微信支付-某超市',
               source_record_id='bank0001',transaction_status='unknown',payment_method=None,**kw)


def data(*rows):
    return {'book_id':'book','batches':[{'revision':0}],'partial':False,'records':list(rows),'summary':{},'blockers':[]}


class AnalysisTests(unittest.TestCase):
    def test_mirror_union_and_exact_totals(self):
        result=build(data(row('wx'),bank()))
        self.assertEqual(result['totals']['CNY']['out'],'58.00')
        self.assertEqual(result['transaction_count'],1)
        tx=result['transactions'][0]
        self.assertEqual(tx['counterparty'],'某超市')
        self.assertEqual(tx['description'],'商品详情')
        self.assertEqual(len(tx['fields']['source_record_id']['alternatives']),2)
        self.assertEqual(len(tx['sources']),2)

    def test_same_amount_time_alone_not_adopted(self):
        b=bank();b.update(memo='快捷支付',counterparty=None)
        result=build(data(row('wx'),b))
        self.assertEqual(len(result['relations']),0)
        self.assertEqual(result['transaction_count'],2)

    def test_competition_prevents_automatic_merge(self):
        result=build(data(row('wx1'),row('wx2',source_record_id='order0002'),bank()))
        self.assertEqual(len(result['relations']),0)
        self.assertEqual(len(result['candidates']),2)

    def test_unique_same_amount_date_two_sided_channel(self):
        b=bank();b.update(memo='财付通-微信支付-美团平台商户',transaction_at='2026-01-01',transaction_at_precision='day')
        a=row('wx',counterparty='美团',trade_type='商户消费')
        result=build(data(a,b))
        self.assertEqual(result['transaction_count'],1)
        self.assertTrue(any('双向渠道' in e for e in result['relations'][0]['evidence']))
        # Same-day repeated equal purchases remain ambiguous, even with different names.
        c=row('second',counterparty='另一个商户',source_record_id='other001')
        self.assertFalse(build(data(a,b,c))['relations'])

    def test_channel_rule_does_not_adopt_untyped_transfer_or_conflicting_processor(self):
        b=bank();b.update(memo='财付通-微信支付-快捷支付')
        a=row('wx',counterparty='张某',description='转账')
        self.assertFalse(build(data(a,b))['relations'])
        a=row('wx',counterparty='别的商户')
        b.update(memo='支付宝-商户',counterparty='支付宝')
        self.assertFalse(build(data(a,b))['relations'])

    def test_wechat_transfer_and_group_collection_keep_friend_name(self):
        for kind in ('转账','群收款'):
            a=row('wx',counterparty='好友昵称',description='私人备注',trade_type=kind)
            b=bank();b.update(memo='财付通-微信支付-微信'+kind,counterparty='财付通-微信'+kind,
                             description='充值',transaction_at='2026-01-01',transaction_at_precision='day')
            result=build(data(a,b))
            self.assertEqual(result['transaction_count'],1)
            self.assertEqual(result['transactions'][0]['counterparty'],'好友昵称')
            self.assertEqual(len(result['transactions'][0]['fields']['counterparty']['alternatives']),2)
            self.assertTrue(any('好友名' in e for e in result['relations'][0]['evidence']))

    def test_wechat_transfer_ambiguity_conflicts_refunds_and_wallet(self):
        a=row('wx',counterparty='好友昵称',trade_type='转账',description='转账')
        b=bank();b.update(memo='财付通-微信转账')
        cases=[{'trade_type':'转账-退款'},{'direction':'in'},{'payment_method':'零钱'},
               {'payment_method':'中国银行储蓄卡(1234)'},{'trade_type':'群收款','description':'群收款'},
               {'transaction_status':'void'},{'currency':'USD'}]
        for changes in cases:
            self.assertFalse(build(data({**a,**changes},b))['relations'],changes)
        b['account']='622200005678'
        self.assertFalse(build(data(a,b))['relations'])
        b.pop('account')
        another={**a,'book_record_id':'other','source_record_id':'other0002'}
        self.assertFalse(build(data(a,another,b))['relations'])

    def test_wechat_transfer_rejection_survives_recalculation(self):
        a=row('wx',counterparty='好友昵称',trade_type='转账',description='转账')
        b=bank();b['memo']='财付通-微信转账'
        with tempfile.TemporaryDirectory() as work:
            store=AnalysisStore(work);source=data(a,b);view=store.view(source)
            store.apply(source,{'action':'reject','relation_id':view['relations'][0]['id'],
                'request_id':'transfer-reject','fingerprint':view['fingerprint'],'expected_revision':0})
            self.assertEqual(AnalysisStore(work).view(source)['transaction_count'],2)

    def test_weak_combination_does_not_block_unique_single(self):
        a=row('a',amount='20.00');b=row('b',amount='38.00',source_record_id='order0002')
        c=bank();c['amount']='20.00'
        combined=bank('combined');combined['amount']='58.00';combined['memo']='快捷支付'
        result=build(data(a,b,c,combined))
        self.assertEqual(len(result['relations']),1)
        self.assertTrue(any(p['decision']=='conflict' for p in result['candidates']))

    def test_currency_direction_and_failed_order(self):
        for changes in [{'currency':'USD'},{'direction':'in'},{'transaction_status':'void'}]:
            result=build(data(row('wx',**changes),bank()))
            self.assertEqual(len(result['relations']),0)

    def test_same_source_overlap_preserves_multiplicity(self):
        a=row('a');b=row('b',batch_id='other')
        self.assertEqual(build(data(a,b))['transaction_count'],1)
        c=row('c')
        self.assertEqual(build(data(a,b,c))['transaction_count'],3)

    def test_strong_reference_not_mechanically_limited_by_window(self):
        b=bank();b.update(transaction_at='2026-01-04',memo='平台订单 order0001')
        self.assertEqual(build(data(row('wx'),b))['transaction_count'],1)

    def test_combined_payment_retains_each_order(self):
        a=row('a',amount='20.00',source_record_id='order0001',description='苹果')
        b=row('b',amount='38.00',source_record_id='order0002',description='牛奶')
        c=bank();c['memo']='order0001 order0002'
        result=build(data(a,b,c))
        self.assertEqual(result['transaction_count'],2)
        self.assertEqual(result['totals']['CNY']['out'],'58.00')
        self.assertEqual({t['description'] for t in result['transactions']},{'苹果','牛奶'})

    def test_account_conflict_blocks_even_reference_match(self):
        b=bank(account='6222888899995678');b['memo']='平台订单 order0001'
        result=build(data(row('wx'),b))
        self.assertEqual(result['transaction_count'],2)
        self.assertFalse(result['relations'])

    def test_refunds_keep_both_cashflows_and_cap_total(self):
        a=row('buy');b=row('refund',amount='20.00',direction='in',trade_type='退款',transaction_at='2026-02-01')
        result=build(data(a,b))
        self.assertEqual(result['totals']['CNY'],{'in':'20.00','out':'58.00','refund':'20.00'})
        self.assertEqual(result['transaction_count'],2)
        self.assertEqual(result['relations'][0]['kind'],'refund')
        c=row('refund2',amount='50.00',direction='in',trade_type='退款',transaction_at='2026-02-02')
        self.assertFalse(build(data(a,b,c))['relations'])

    def test_refund_mirror_is_one_inflow_but_purchase_stays(self):
        credit=row('wx-refund',direction='in',trade_type='中铁网络-退款',counterparty='中铁网络',
                   source_record_id='refund001',transaction_at='2026-01-02 12:00:00')
        b=bank();b.update(direction='in',description='消费退货',memo='财付通-中铁网络',
                         transaction_at='2026-01-02 12:00:00')
        purchase=row('purchase',transaction_at='2026-01-01 12:00:00',source_record_id='purchase001')
        result=build(data(credit,b,purchase))
        self.assertEqual(result['transaction_count'],2)
        self.assertEqual(result['totals']['CNY'],{'out':'58.00','in':'58.00','refund':'58.00'})
        tx=next(t for t in result['transactions'] if t['direction']=='in')
        self.assertTrue(tx['refund']);self.assertEqual(len(tx['sources']),2)
        self.assertFalse(tx['refund_linked'])

    def test_two_equal_purchases_and_refund_are_four_options_not_four_events(self):
        early=row('wx-early',amount='21',transaction_at='2026-04-03 11:49:26',raw_status='已全额退款')
        later=row('wx-later',amount='21',transaction_at='2026-04-03 12:29:28',source_record_id='order0002')
        credit=row('wx-credit',amount='21',direction='in',transaction_at='2026-04-03 11:54:11',trade_type='某超市-退款',source_record_id='refund0001')
        banks=[]
        for rid,seq,direction in [('b188',188,'out'),('b189',189,'in'),('b190',190,'out')]:
            b=bank(rid);b.update(amount='21.00',direction=direction,transaction_at='2026-04-03',transaction_at_precision='day',
                source_record_id=rid+'0000',row=seq,description='消费退货' if direction=='in' else '消费')
            banks.append(b)
        source=data(early,later,credit,*banks);result=build(source)
        self.assertEqual(len(result['relations']),1)
        self.assertEqual(len(result['candidates']),4)
        self.assertEqual(len({s for p in result['candidates'] for s in p['source_ids']}),4)
        self.assertEqual(sum(t['direction']=='in' for t in result['transactions']),1)
        self.assertEqual(result['totals']['CNY']['refund'],'21')
        self.assertEqual(result['transaction_count'],5) # Two duplicate debits remain unresolved, visibly provisional.
        reversed_result=build({**source,'records':list(reversed(source['records']))})
        self.assertEqual({p['id'] for p in result['relations']},{p['id'] for p in reversed_result['relations']})
        self.assertEqual({p['id'] for p in result['candidates']},{p['id'] for p in reversed_result['candidates']})

    def test_income_then_wallet_withdrawal_not_assumed_duplicate(self):
        income=row('wx-in',direction='in',trade_type='转账',counterparty='好友',payment_method='零钱',description='转账')
        b=bank();b.update(direction='in',description='充值',memo='财付通-微信提现')
        result=build(data(income,b))
        self.assertEqual(result['transaction_count'],2)
        self.assertFalse(result['relations'])

    def test_wechat_redpacket_card_mirror_and_refund_not_confused(self):
        a=row('wx',trade_type='微信红包（群红包）',counterparty='发出群红包',description='红包')
        b=bank();b.update(description='充值',memo='财付通-微信支付-微信红包')
        self.assertEqual(build(data(a,b))['transaction_count'],1)
        b['memo']='财付通-微信红包退款'
        self.assertFalse(build(data(a,b))['relations'])

    def test_fingerprint_accounts_for_code_identity(self):
        from unittest.mock import patch
        source=data(row('wx'))
        before=fingerprint(source)
        with patch('analysis.core.IMPLEMENTATION','other-implementation'):
            self.assertNotEqual(before,fingerprint(source))

    def test_unlinked_refund_and_refunded_purchase(self):
        refund=row('refund',direction='none',source='支付宝',raw_status='退款成功')
        result=build(data(refund))
        self.assertEqual(result['totals']['CNY']['refund'],'58.00')
        self.assertFalse(result['transactions'][0]['refund_linked'])
        purchase=row('buy',raw_status='已全额退款')
        self.assertFalse(build(data(purchase))['transactions'][0]['refund'])

    def test_source_input_is_immutable(self):
        source=data(row('wx'),bank());before=copy.deepcopy(source);build(source)
        self.assertEqual(source,before)

    def test_decision_persistence_rejection_undo_and_stale(self):
        with tempfile.TemporaryDirectory() as work:
            store=AnalysisStore(work);source=data(row('wx'),bank());view=store.view(source);p=view['relations'][0]
            def act(action,**extra):
                v=store.view(source)
                return store.apply(source,{'action':action,'relation_id':p['id'],'request_id':uuid.uuid4().hex,
                    'fingerprint':v['fingerprint'],'expected_revision':v['revision'],**extra})
            view=act('reject');self.assertEqual(view['transaction_count'],2)
            store=AnalysisStore(work);self.assertEqual(store.view(source)['transaction_count'],2)
            view=act('accept');self.assertEqual(view['transaction_count'],1)
            target=view['revision'];view=act('undo',target=target)
            self.assertEqual(view['transaction_count'],2)
            view=act('accept');source['records'][0]['description']='修正商品'
            view=store.view(source);self.assertEqual(view['transaction_count'],2)
            self.assertTrue(view['stale_decisions'])
            with self.assertRaises(Conflict):
                store.apply(source,{'action':'reject','relation_id':p['id'],'request_id':'stale',
                                    'fingerprint':'old','expected_revision':view['revision']})

    def test_display_correction_not_source_mutation_and_idempotency(self):
        with tempfile.TemporaryDirectory() as work:
            store=AnalysisStore(work);source=data(row('wx'),bank());view=store.view(source)
            p={'action':'display','relation_id':view['transactions'][0]['id'],'fields':{'counterparty':'用户确认的商户'},
               'request_id':'once','fingerprint':view['fingerprint'],'expected_revision':0}
            v=store.apply(source,p);store.apply(source,p)
            self.assertEqual(v['transactions'][0]['counterparty'],'用户确认的商户')
            self.assertEqual(len(store.events()),1)
            self.assertEqual(source['records'][0]['counterparty'],'某超市')

    def test_rejection_survives_an_overlapping_export(self):
        with tempfile.TemporaryDirectory() as work:
            store=AnalysisStore(work);source=data(row('wx'),bank());view=store.view(source)
            store.apply(source,{'action':'reject','relation_id':view['relations'][0]['id'],
                'request_id':'reject','fingerprint':view['fingerprint'],'expected_revision':0})
            source['records'].append(row('wx-overlap',batch_id='second'))
            view=store.view(source)
            self.assertEqual(view['transaction_count'],2)
            self.assertFalse(any(p['kind']=='payment' for p in view['relations']))

    def test_privacy_keeps_merchant_protects_transfer_and_embedded_pii(self):
        with tempfile.TemporaryDirectory() as work:
            personal=row('p',counterparty='张小明',description='转账',account='6222000000001234')
            shopping=row('s',trade_type='商户消费',description='牛奶 13800138000',memo='联系张小明')
            protector=Protector(work,[personal,shopping]);out=json.dumps([protector.row(r) for r in [personal,shopping]],ensure_ascii=False)
            for secret in ['张小明','6222000000001234','13800138000','order0001']:
                self.assertNotIn(secret,out)
            self.assertIn('某超市',out)
            self.assertEqual(protector.token('张小明'),Protector(work,[]).token('张小明'))
        with tempfile.TemporaryDirectory() as work2:
            self.assertNotEqual(protector.token('张小明'),Protector(work2,[]).token('张小明'))

    def test_ai_schema_rejects_extra_ids_duplicate_and_free_text(self):
        self.assertEqual(checked({'decisions':[{'id':'a','verdict':'same'}]},{'a'}),{'a':'same'})
        for payload in [{'decisions':[{'id':'b','verdict':'same'}]},
                        {'decisions':[{'id':'a','verdict':'same','name':'secret'}]}, {'decisions':[]}]:
            with self.assertRaises(ReviewError):checked(payload,{'a'})

    def test_bank_known_merchant_survives_privacy_but_transfer_does_not(self):
        with tempfile.TemporaryDirectory() as work:
            purchase=row('wx',counterparty='示例餐饮品牌',trade_type='商户消费')
            b=bank();b.update(counterparty='财付通-微信支付-示例餐饮品牌',memo='财付通-微信支付-示例餐饮品牌')
            person=row('person',counterparty='张小明',description='转账')
            p=Protector(work,[purchase,b,person])
            self.assertIn('示例餐饮品牌',p.row(b)['counterparty'])
            self.assertNotIn('张小明',json.dumps(p.row(person),ensure_ascii=False))


if __name__=='__main__':unittest.main()
