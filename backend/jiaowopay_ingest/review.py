"""Static, local-only review view. No confirmation or database mutation here."""
from html import escape


LABELS = {
    'OCR_SCORE_BELOW_THRESHOLD':'关键字段识别分数低于 0.98',
    'OCR_INSUFFICIENT_AGREEMENT':'原图与处理图的关键字段未获得充分一致证据',
    'OCR_ORIGINAL_UNCERTAIN':'原图识别存在异常',
    'OCR_REQUIRED_FIELD_MISSING':'关键字段或币种尚不完整',
    'OCR_DERIVED_VALUE_REQUIRES_REVIEW':'字段由增强识别恢复，需对照原图确认',
    'OCR_CURRENCY_UNCONFIRMED':'币种缺少明确且一致的识别证据',
    'CURRENCY_UNKNOWN':'币种未识别，未默认当成人民币',
    'OCR_PAGE_COUNT_UNVERIFIED':'未可靠识别本页笔数，需要检查是否漏行',
    'OCR_PAGE_COUNT_MISMATCH':'识别行数与本页笔数不一致',
    'OCR_NO_ROWS':'未识别出交易行，请对照整页手工补录',
    'OCR_TIMEOUT':'识别超时，需要重试或手工处理',
    'OCR_MEMORY_LIMIT':'识别超出内存预算，需要分批或手工处理',
    'OCR_WORKER_FAILED':'本地识别未完成，请检查依赖或文件',
    'OCR_RUNTIME_MISSING':'本地 OCR 运行环境缺失',
}


def reason_text(code):
    for prefix,label in [('OCR_VIEW_CONFLICT:','不同识别结果冲突：'),
                         ('RECOVERED_FROM_DERIVED_VIEWS:','增强识别恢复字段：'),
                         ('VIEW_LOST_FIELD:','部分识别路径遗漏字段：'),
                         ('UNRESOLVED_FIELD:','仍无法确定字段：')]:
        if code.startswith(prefix):return label+code[len(prefix):]
    return LABELS.get(code,code)


def export_review_html(dataset, path):
    cards=[]
    for review in dataset.get('reviews',[]):
        title=f"第 {review['page']} 页 · "+('记录待确认' if review['scope']=='record' else '整页待确认')
        values=' | '.join(f'{name}: {review["suggested_fields"].get(key) or "待确定"}' for key,name in
                         [('transaction_at','日期'),('amount','金额'),('direction','方向'),('currency','币种'),('balance','余额')]) if review['scope']=='record' else '请检查漏行，并准备手工补录。'
        evidence=review['evidence']
        image=evidence.get('row_image',evidence.get('page_image'))
        picture=f'<img loading="lazy" src="{escape(image,quote=True)}" alt="原始账单截图">' if image else '<p>截图未生成，请保留并查看原始文件。</p>'
        link=f'<a href="{escape(evidence["page_image"],quote=True)}">打开完整原页</a>' if evidence.get('page_image') else ''
        reasons=''.join(f'<li>{escape(reason_text(c))}</li>' for c in review['reason_codes'])
        cards.append(f'<section><h2>{escape(title)}</h2><p>{escape(values)}</p><ul>{reasons}</ul>{picture}<p>{link}</p><small>核验任务：{escape(review["review_id"])}</small></section>')
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>交我Pay · 待确认截图</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;color:#20354d;background:#f3f5f7}section{padding:20px;margin:18px 0;background:white;border:1px solid #ccc;border-radius:8px}img{max-width:100%;height:auto;border:1px solid #ddd}small{overflow-wrap:anywhere}h2{font-size:18px}a{color:#245ab0}.notice{background:#fff0ce;padding:16px}</style>
<h1>待确认截图</h1><p class="notice">仅限本地核对，含敏感财务信息，请勿公开。此页是只读预览，不能在此确认或修改账本。金额、币种等空值不能当成零。未来前端请读取 SQLite 的 reviews 和 assets。</p>
'''+(''.join(cards) or '<p>本数据包没有 OCR 待确认任务；其他格式的问题仍请查看异常清单。</p>')+'</html>'
    path.write_text(html,encoding='utf-8')
