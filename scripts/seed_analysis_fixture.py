"""Synthetic payment/competition fixture for isolated browser acceptance."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.service import App
from jiaowopay_ingest import import_files,save_bundle


def seed(root):
    app=App(root,ocr=False);bid=app.catalog.create('去重配对合成账本')
    # Catalog.create returns a book id; the active context is authoritative.
    bid=app.catalog.context()['book_id']
    with tempfile.TemporaryDirectory() as folder:
        folder=Path(folder)
        wx=folder/'微信合成.csv';bank=folder/'建行合成.csv'
        wx.write_text('交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n'
                      '2026-01-01 12:00:00,商户消费,某超市,苹果与牛奶,支出,58.00,建设银行储蓄卡(1234),支付成功,order10001,merchant10001,/\n'
                      '2026-01-02 12:00:00,商户消费,甲商店,文具,支出,20.00,建设银行储蓄卡(1234),支付成功,order10002,merchant10002,/\n'
                      '2026-01-02 12:00:00,商户消费,乙商店,书籍,支出,20.00,建设银行储蓄卡(1234),支付成功,order10003,merchant10003,/\n',encoding='utf8')
        bank.write_text('序号,摘要,币别,钞汇,交易日期,交易金额,账户余额,对方账号与户名,交易地点/附言\n'
                        '1,消费,CNY,钞,2026-01-01 12:00:02,-58.00,1000.00,财付通,财付通-微信支付-某超市\n'
                        '2,消费,CNY,钞,2026-01-02 12:00:02,-20.00,980.00,财付通,快捷支付\n',encoding='utf8')
        bundle=save_bundle(import_files([wx,bank],ocr=False),Path(root)/'books'/bid/'synthetic-import',excel=False)
    app.catalog.attach(bid,bundle,'合成来源');app.sync_book()
    return app


if __name__=='__main__':
    app=seed(sys.argv[1]);print('Synthetic book created:',app.catalog.context()['book_id'])
