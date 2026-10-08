"""Synthetic missed-page fixture for browser testing; never touches real data."""
from pathlib import Path
import hashlib
import io
import sys
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.service import App
from jiaowopay_ingest import import_files, save_bundle
from jiaowopay_ingest.model import issue

workspace=Path(sys.argv[1])
source=workspace.parent/'page-fixture.csv'
source.write_text('交易日期,金额,收支,币种\n2026-09-05,15,支出,人民币\n',encoding='utf-8')
data=import_files([source],ocr=False)
file=data['files'][0];fid=file['file_id']
file.update(format='pdf',filename='synthetic-page.pdf',reader_metadata={'page_count':1})
image=Image.new('RGB',(900,420),'white');draw=ImageDraw.Draw(image)
draw.text((30,40),'SYNTHETIC STATEMENT - BROWSER QA',fill='black',font_size=30)
draw.text((30,120),'Date             Direction     Amount     Currency',fill='black',font_size=24)
draw.text((30,180),'2026-09-05       OUT           15.00      CNY',fill='black',font_size=24)
draw.text((30,300),'Total records: 1',fill='black',font_size=24)
buffer=io.BytesIO();image.save(buffer,format='PNG');content=buffer.getvalue()
data['records']=[]
data['issues']=[issue('OCR_NO_ROWS','本页未识别出记录，请对照原页补录。',file_id=fid,page=1),issue('NO_TRANSACTIONS','没有读出记录。',file_id=fid)]
data['reviews']=[{'review_id':'synthetic-page-task','scope':'page','record_id':None,'file_id':fid,'page':1,'status':'pending','evidence':{'page_image':'evidence/page.png'},'reason_codes':['OCR_NO_ROWS']}]
data['_assets']={'evidence/page.png':content}
data['assets']=[{'path':'evidence/page.png','sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content),'role':'original_page'}]
app=App(workspace,ocr=False);bid=app.catalog.create('整页补录合成验收')
bundle=save_bundle(data,workspace/'books'/bid/'page-fixture',excel=False)
app.catalog.attach(bid,bundle,'未识别页面')
print('Synthetic page fixture ready.')
