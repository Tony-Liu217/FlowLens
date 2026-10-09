"""Bridge isolated local OCR into canonical records and durable review evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .mapping import Mapping
from .model import Row, digest, issue, ReadFailure
from .normalize import normalize
from jiaowopay_ocr.policy import VERSION, MIN_SCORE, assess, page_gate, auxiliary
from .review import reason_text

BACKEND = Path(__file__).resolve().parents[1]
MAX_ASSET_BYTES = 200*1024*1024


def runtime(explicit=None):
    if explicit:
        path=Path(explicit).resolve()
        if not path.is_file():raise ReadFailure('OCR_RUNTIME_MISSING','指定的本地 OCR 运行环境不存在。')
        return path
    if all(importlib.util.find_spec(m) for m in ['rapidocr','pypdfium2','cv2','numpy']):
        return Path(sys.executable)
    # Transitional local setup; portable installations use their own environment.
    for path in [BACKEND/'.ocr-env/Scripts/python.exe',BACKEND/'.local-data/ocr-benchmark-env/Scripts/python.exe']:
        if path.is_file():return path
    raise ReadFailure('OCR_RUNTIME_MISSING','缺少本地 OCR 环境；请安装 backend/jiaowopay_ocr/requirements.txt 或指定 --ocr-python。')


def run_worker(source, page, output, python=None, timeout=180):
    import psutil
    if not hasattr(psutil,'Process'):
        raise ReadFailure('OCR_DEPENDENCY_MISSING','进程监控依赖不可读取，请检查本地依赖安装权限。')
    executable=runtime(python)
    cmd=[str(executable),'-B','-X','utf8','-m','jiaowopay_ocr.worker',str(source.resolve()),'--output',str(output)]
    if page is not None:cmd+=['--page',str(page)]
    started=time.monotonic()
    # Never echo financial exception text. It lives only in the temporary private log.
    with (output.parent/'worker.log').open('w',encoding='utf-8') as log:
        worker=subprocess.Popen(cmd,cwd=BACKEND,stdout=log,stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        proc=None
        failure=None
        try:
            proc=psutil.Process(worker.pid)
            while worker.poll() is None:
                try:
                    family=[proc]+proc.children(recursive=True)
                    rss=sum(p.memory_info().rss for p in family if p.is_running())
                except psutil.NoSuchProcess:
                    rss=0
                if time.monotonic()-started>timeout or rss>3*1024**3:
                    failure='OCR_TIMEOUT' if time.monotonic()-started>timeout else 'OCR_MEMORY_LIMIT'
                    break
                time.sleep(.1)
        finally:
            if worker.poll() is None:
                if proc is None:
                    worker.kill()
                    worker.wait(timeout=10)
                    raise ReadFailure('OCR_SUPERVISION_FAILED','无法建立 OCR 进程监控，已停止本次识别。')
                try:
                    children=proc.children(recursive=True)
                except psutil.NoSuchProcess:
                    children=[]
                for child in reversed(children):
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
                try:proc.kill()
                except psutil.NoSuchProcess:pass
            code=worker.wait(timeout=10)
        if failure:raise ReadFailure(failure,'本页 OCR 超出本地资源预算，未将失败视为零交易。')
        if code or not (output/'COMPLETE').is_file():
            raise ReadFailure('OCR_WORKER_FAILED','本地 OCR 未完成，可能缺少依赖、模型或文件不支持；已保留可用原页。')
    return json.loads((output/'result.json').read_text(encoding='utf-8'))


def asset(dataset, relative, data, role):
    if sum(len(v) for v in dataset['_assets'].values())+len(data)>MAX_ASSET_BYTES:
        raise ReadFailure('OCR_EVIDENCE_LIMIT','本批截图与识别证据超过 200 MiB，请分批导入；没有省略证据后继续放行。')
    dataset['_assets'][relative]=data
    entry={'path':relative,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'role':role}
    dataset['assets'].append(entry)
    return relative


def add_review(dataset, file_id, page, reasons, evidence, *, record=None, bbox=None):
    review_id=digest(file_id,page,record.get('record_id') if record else 'page',VERSION)
    review={'review_id':review_id,'status':'pending','scope':'record' if record else 'page',
            'file_id':file_id,'page':page,'record_id':record.get('record_id') if record else None,
            'raw_id':record.get('raw_id') if record else None,'reason_codes':sorted(set(reasons)),
            'reason_messages':[reason_text(c) for c in sorted(set(reasons))],
            'evidence':evidence,'bbox':bbox,'coordinate_space':'rendered_image_pixels',
            'policy_version':VERSION,'minimum_ocr_score':MIN_SCORE,
            'suggested_fields':{k:record.get(k) for k in ['transaction_at','amount','direction','currency','balance']} if record else {},
            'required_action':'核对原图后确认或修正；页面级任务需检查漏行并手工补录'}
    dataset['reviews'].append(review)
    if record:record['review_id']=review_id
    return review


def import_page(dataset, source, file_id, sha, occurrence, page, *, python=None, timeout=180):
    from . import PARSER_VERSION
    work=BACKEND/'.local-data/ocr-work'
    work.mkdir(parents=True,exist_ok=True)
    prefix=f'evidence/{file_id}/page-{page or 1:04d}'
    evidence={}
    location={'segment':f'page-{page or 1}-ocr','kind':'ocr','sheet':None,'page':page or 1,'table':None}
    with tempfile.TemporaryDirectory(prefix='page-',dir=work) as temp:
        output=Path(temp)/'worker'
        try:
            result=run_worker(source,page,output,python,timeout)
        except (ReadFailure,ImportError) as exc:
            if (output/'original.png').is_file():
                evidence['page_image']=asset(dataset,prefix+'/page.png',(output/'original.png').read_bytes(),'original_page')
            code=exc.code if isinstance(exc,ReadFailure) else 'OCR_DEPENDENCY_MISSING'
            dataset['issues'].append(issue(code,str(exc) if isinstance(exc,ReadFailure) else '本地 OCR 缺少依赖。',file_id=file_id,page=page or 1))
            add_review(dataset,file_id,page or 1,[code],evidence)
            return
        evidence['page_image']=asset(dataset,prefix+'/page.png',(output/'original.png').read_bytes(),'original_page')
        evidence['ocr_json']=asset(dataset,prefix+'/ocr.json',(output/'result.json').read_bytes(),'ocr_observations')
        if result.get('image_sha256')!=sha:
            raise ReadFailure('OCR_SOURCE_CHANGED','OCR 期间原文件内容发生变化，拒绝关联识别结果。')
        reasons,count=page_gate(result)
        dataset['segments'].append({'file_id':file_id,'name':location['segment'],'kind':'ocr','page':page or 1,
                                    'physical_rows':len(result['proposals']),'mapping':{'method':'ocr_exact_header'},
                                    'evidence':evidence,'image_size':result['image_size'],'printed_count':count,
                                    'ocr_versions':result['versions'],'policy_version':VERSION})
        if reasons or not result['proposals']:
            reasons=reasons or ['OCR_NO_ROWS']
            add_review(dataset,file_id,page or 1,reasons,evidence)
            dataset['issues'].append(issue('OCR_PAGE_REVIEW','页完整性未通过核验，请核对原页及漏行。',file_id=file_id,page=page or 1,reason_codes=reasons))
        originals=result['observations'].get('original',[])
        fields=['transaction_at','amount','direction','balance','currency','account','description',
                'counterparty','counterparty_account','payment_method','source_record_id']
        mapping=Mapping({f:i for i,f in enumerate(fields)},fields,0,profile='ocr.table.v1')
        for i,proposal in enumerate(result['proposals']):
            original=originals[i]
            values=[proposal.get(f) if f in fields[:4] else original['raw_fields'].get(f) for f in fields]
            rec,problems=normalize(Row(values,i+1),mapping)
            raw_id=digest(sha,occurrence,{**location,'row':i+1,'end_row':i+1})
            record_id=digest(raw_id,PARSER_VERSION,VERSION,mapping.columns)
            raw={'raw_id':raw_id,'file_id':file_id,'filename':source.name,**location,'row':i+1,'end_row':i+1,
                 'values':list(original['raw_fields'].values()),'cell_types':[],'formats':[],'formulas':{},
                 'disposition':'transaction','ocr_evidence':evidence,'ocr_original_fields':original['raw_fields']}
            raw_keys=list(original['raw_fields'])
            source_fields={'transaction_at':'date','signed_amount':'signed','credit_amount':'credit','debit_amount':'debit',
                           **{f:f for f in fields if f not in fields[:3]}}
            source_columns={f:raw_keys.index(k) for f,k in source_fields.items() if k in raw_keys}
            rec.update(record_id=record_id,raw_id=raw_id,file_id=file_id,filename=source.name,**location,
                       row=i+1,end_row=i+1,mapping_columns=source_columns,
                       raw_headers=[original.get('column_labels',{}).get(k,k) for k in raw_keys],
                       extraction_method='local_ocr',ocr_evidence=dict(evidence),ocr_policy=VERSION,
                       ocr_geometry=proposal['source_geometry'],ocr_changes=proposal.get('changes',[]))
            from .semantics import enrich
            enrich(rec, rec['raw_headers'], source_columns, raw['values'])
            gate=assess(result,i,rec)
            rec.update(auxiliary(result, i))
            gate += [p['code'] for p in problems if p['severity']=='error']
            if gate:
                rec['parse_status']='needs_review'
                crop=proposal['review_crop']
                # Worker-generated names, never trust an arbitrary path from JSON.
                expected=f'row-{i+1:05}.png'
                if crop['path']!=expected:raise ReadFailure('OCR_EVIDENCE_INVALID','OCR 截图位置无效。')
                row_image=asset(dataset,prefix+'/'+expected,(output/expected).read_bytes(),'original_row')
                rec['ocr_evidence']['row_image']=row_image
                add_review(dataset,file_id,page or 1,gate,rec['ocr_evidence'],record=rec,bbox=crop['bbox'])
                problems.append(issue('OCR_RECORD_REVIEW','OCR 证据不满足放行规则，已保留原行截图供人工核验。',reason_codes=sorted(set(gate))))
            else:
                rec['parse_status']='ready'
            dataset['raw_rows'].append(raw)
            dataset['records'].append(rec)
            dataset['issues'].extend({**p,'file_id':file_id,'page':page or 1,'raw_id':raw_id,'record_id':record_id} for p in problems)
