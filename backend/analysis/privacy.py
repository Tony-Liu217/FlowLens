"""Book-scoped outbound copies. Original records and identity maps never leave disk."""
import hashlib
import hmac
import re
import secrets
from pathlib import Path

VERSION='pairing-privacy-2'
PRIVATE_FIELDS=('account','counterparty_account','counterparty_combined','source_record_id','merchant_order_no')
DIRECT=re.compile(r'转账|红包|群收款|收款码|亲属|生活费|借款|还款|代付|提现')
SENSITIVE=re.compile(r'收货|收件|姓名|电话|手机|地址|身份证|联系|密码|密钥|token|https?://',re.I)


class Protector:
    def __init__(self,directory,records):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        path=directory/'privacy.key'
        if not path.exists():
            try:
                with path.open('xb') as stream:stream.write(secrets.token_bytes(32))
            except FileExistsError:pass
        self.key=path.read_bytes()
        self.private=set()
        self.merchants={str(r.get('counterparty') or '').strip() for r in records
                        if r.get('trade_type')=='商户消费' and not self.direct(r)}-{'','/','-','未知'}
        for row in records:
            if self.direct(row):
                value=str(row.get('counterparty') or '').strip()
                if value and value not in {'/','-','未知'}:self.private.add(value)
            for field in PRIVATE_FIELDS:
                value=str(row.get(field) or '').strip()
                if len(value)>1 and value not in {'未知','/'}:self.private.add(value)

    def token(self,value):
        return 'P'+hmac.new(self.key,str(value).encode(),hashlib.sha256).hexdigest()[:20]

    def direct(self,row):
        return bool(DIRECT.search(' '.join(str(row.get(k) or '') for k in ('trade_type','description','memo'))))

    def clean(self,value):
        text=str(value or '')
        for private in sorted(self.private,key=len,reverse=True):text=text.replace(private,self.token(private))
        text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',lambda m:self.token(m[0]),text)
        text=re.sub(r'(?<![A-Za-z0-9])\+?\d[\d ()-]{6,}\d',lambda m:self.token(m[0]),text)
        if SENSITIVE.search(text):return '[含私人信息的文本已省略]'
        return text[:320]

    def row(self,row):
        direct=self.direct(row)
        # Unknown/non-corporate names are protected by default. Do not infer ownership.
        name=str(row.get('counterparty') or '')
        merchant_name=re.sub(r'^(?:财付通[-－](?:微信支付[-－])?|支付宝[-－](?:支付宝外部商户[-－])?)','',name)
        merchant=not direct and (row.get('trade_type')=='商户消费' or
                    (merchant_name in self.merchants and merchant_name not in self.private) or
                    bool(re.search(r'公司|商店|超市|旗舰店|餐厅|便利店|商城|商行',name)))
        result={k:row.get(k) for k in ('source','amount','currency','direction','transaction_at','booking_at','transaction_status')}
        result['id']=self.token(row['book_record_id'])
        result['counterparty']=self.clean(name) if merchant else self.token(name) if name else None
        result['description']=self.clean(row.get('description')) if merchant else '[个人或未明确对象，说明未外发]'
        result['memo']=self.clean(row.get('memo')) if merchant else '[未外发]'
        method=str(row.get('payment_method') or '')
        method=re.sub(r'[（(](\d{3,6})[）)]',lambda m:'('+self.token('card-tail:'+m[1])+')',method)
        result['payment_method']=self.clean(method)
        # Bank narratives only expose known processor and exact business evidence via
        # candidate flags; raw narrative may contain the account holder's name.
        for field in ('source_record_id','merchant_order_no'):
            result[field]=self.token(str(row.get('source'))+':'+field+':'+str(row[field])) if row.get(field) else None
        return result

    def candidate(self,proposal,records):
        rows={r['book_record_id']:r for r in records}
        return {'id':self.token(proposal['id']),'kind':proposal['kind'],
                'competing_candidates':proposal.get('competing_candidates',0),
                'facts':proposal['evidence'],
                'records':[self.row(rows[rid]) for rid in proposal['source_ids']]}
