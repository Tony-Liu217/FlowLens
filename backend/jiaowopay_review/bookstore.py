"""Book-wide views; each immutable batch retains its own revision stream."""
from .store import ReviewStore, ReviewError, load_effective_records

SUMMARY_KEYS = ('total','eligible','pending','excluded','manual','page_pending','blocking_issues')


def book_snapshot(catalog, book_id):
    batches=[]
    summary=dict.fromkeys(SUMMARY_KEYS,0)
    for batch in catalog.batches(book_id):
        item={k:batch[k] for k in ('id','name','created')}
        try:
            snap=ReviewStore(batch['bundle'],batch['states']).snapshot()
            item.update(summary=snap['summary'],revision=snap['revision'],
                        filenames=[f['filename'] for f in snap['files']])
            for key in SUMMARY_KEYS: summary[key]+=snap['summary'][key]
        except Exception:
            item.update(summary=None,error='此批次底稿或证据无法读取，原文件保留。')
            summary['blocking_issues']+=1
        batches.append(item)
    return {'batches':batches,'summary':summary,'unreadable_batches':sum('error' in b for b in batches)}


def export_book(catalog, book_id, *, allow_partial=False):
    book=catalog.get(book_id)
    batches=catalog.batches(book_id)
    if not batches: raise ReviewError('当前账本还没有导入批次。')
    result={'schema_version':'book-effective-1','book_id':book_id,'book_name':book['name'],
            'catalog_revision':catalog.context()['catalog_revision'], 'partial':False,
            'summary':dict.fromkeys(SUMMARY_KEYS,0),'batches':[], 'records':[],
            'pending_record_ids':[],'excluded_record_ids':[],'blockers':[],
            'unreadable_batch_ids':[], 'transaction_deduplication':False}
    for batch in batches:
        entry={k:batch[k] for k in ('id','name','created')}
        try:
            data=load_effective_records(batch['bundle'],batch['states'],allow_partial=True)
        except Exception:
            result['partial']=True
            result['unreadable_batch_ids'].append(batch['id'])
            result['summary']['blocking_issues']+=1
            result['blockers'].append({'batch_id':batch['id'],'code':'BATCH_UNREADABLE',
                                      'message':'此批次底稿或证据无法读取，记录数未知，未计入可用数据。'})
            entry.update(unreadable=True,revision=None)
        else:
            entry.update(workspace_id=data['workspace_id'],revision=data['revision'],
                         reading_policy=data['reading_policy'],
                         source_files=data.get('source_files',[]),
                         summary=data['summary'],partial=data['partial'])
            result['partial'] |= data['partial']
            for key in SUMMARY_KEYS: result['summary'][key]+=data['summary'][key]
            # Source record/file ids can repeat between batches; retain them and
            # expose a separate stable book-wide identity, never overwrite sources.
            for row in data['records']:
                result['records'].append({**row,'batch_id':batch['id'],
                    'book_record_id':batch['id']+':'+row['record_id']})
            for key in ('pending_record_ids','excluded_record_ids'):
                result[key].extend(batch['id']+':'+rid for rid in data[key])
            result['blockers'].extend({**item,'batch_id':batch['id']} for item in data['blockers'])
        result['batches'].append(entry)
    if result['partial'] and not allow_partial:
        raise ReviewError('账本中仍有批次存在待核验记录、页面或文件异常；请完成核验，或明确接受只导出可用部分。')
    # Compatibility fields for single-batch consumers. Multi-batch consumers must
    # read the per-batch revision vector rather than invent a combined revision.
    if len(batches)==1 and not result['unreadable_batch_ids']:
        result.update(workspace_id=result['batches'][0]['workspace_id'],revision=result['batches'][0]['revision'])
    return result
