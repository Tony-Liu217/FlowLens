"""Format contracts, not reconciliation rules.

Headers are normalized by the caller. Display names are deliberately separate
from source roles and institution IDs. Adding a bank must not add an analysis
branch. These contracts cover the existing five formats, not arbitrary exports.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    id: str
    headers: frozenset[str]
    label: str
    role: str
    institution: str
    currency: str | None = None
    signed: bool = False
    document_marker: str | None = None


PROFILES = (
    Profile('wechat.export.v1', frozenset({'交易时间','交易类型','商品','当前状态','交易单号','商户单号'}),
            '微信', 'payment_platform', 'wechat_pay', currency='CNY'),
    Profile('alipay.export.v1', frozenset({'交易时间','交易分类','对方账号','商品说明','交易订单号','商家订单号'}),
            '支付宝', 'payment_platform', 'alipay', currency='CNY'),
    Profile('ccb.account_activity.v1', frozenset({'序号','摘要','币别','钞汇','交易日期','交易金额','账户余额','对方账号与户名'}),
            '建设银行', 'account_ledger', 'ccb', signed=True),
    Profile('boc.account_activity.v1', frozenset({'记账日期','记账时间','币别','金额','余额','交易名称','对方账户名','对方开户行'}),
            '中国银行', 'account_ledger', 'boc', signed=True),
    Profile('ccb.account_activity.pdf.v1', frozenset({'序号','摘要','交易日期','交易金额','账户余额','交易地点附言','对方账号与户名'}),
            '建设银行', 'account_ledger', 'ccb', signed=True,
            document_marker='中国建设银行个人活期账户全部交易明细'),
)
BY_ID = {p.id: p for p in PROFILES}


def matching_profiles(headers, document_text=None):
    """Return all matches; callers must not mistake a match for complete coverage."""
    return tuple(p for p in PROFILES if p.headers <= set(headers) and
                 ((document_text is None and p.document_marker is None) or
                  (document_text is not None and p.document_marker is not None and
                   p.document_marker in document_text)))
