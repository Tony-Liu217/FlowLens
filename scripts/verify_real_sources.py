"""Read supplied local bills into a NEW isolated validation workspace. No AI."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from jiaowopay_ingest import import_files, save_bundle
from app.service import App

parser = argparse.ArgumentParser()
parser.add_argument('files', nargs='+', type=Path)
args = parser.parse_args()
run = ROOT / '.local-data' / ('source-validation-' + str(int(time.time())))
run.mkdir(parents=True)
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.files}
started = time.monotonic()
data = import_files(args.files, ocr=True)
app = App(run / 'workspace')
bid = app.catalog.create('隔离来源验证')
bundle = save_bundle(data, app.root / 'books' / bid / 'validation-import', excel=False)
app.catalog.attach(bid, bundle, '五来源读取验证')
app.sync_book()
report = {'seconds': round(time.monotonic()-started, 2),
          'source_files_unchanged': all(hashlib.sha256(p.read_bytes()).hexdigest() == before[str(p)] for p in args.files),
          'summary': app.store.snapshot()['summary'],
          'files': [{'filename': f['filename'], 'status':f['status'],
                     'records':sum(r['file_id']==f.get('file_id') for r in data['records'])} for f in data['files']],
          'network_used': data['network_used'], 'external_ai_used':False,
          'basis':'Local parsing/OCR and persistence check only; no new independent field ground truth.'}
(run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
print(json.dumps({'run':str(run), **report},ensure_ascii=False),flush=True)
