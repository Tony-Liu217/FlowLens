"""Isolated local OCR worker. Parent enforces resources; no truth/network input."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import time


from jiaowopay_ocr.consensus import merge_observations
from jiaowopay_ocr.table import from_tokens, record, recover_currency_column


def reread_cells(engine, views, initial, output):
    """Retry unresolved numeric cells only, without truth or bank coordinates.

    Strict whole-cell parsing: no 'first number' extraction or balance guessing.
    Crops are bounded by inferred columns; boundary risks stay for review.
    """
    from PIL import Image, ImageOps
    evidence, audit = {}, []
    height, width = views['original'].shape[:2]
    attempts = 0
    for i, row in enumerate(initial):
        geom = row['source_geometry']
        for group, column in [('amount+direction', 'signed'), ('balance', 'balance')]:
            if all(row.get(f) is not None for f in group.split('+')):
                continue
            bounds = geom['columns'].get(column)
            if bounds is None or None in bounds:
                continue
            left, right = bounds
            low, high = geom['row_y_bounds']
            # Date anchors can sit on the first line of a two-line date/time
            # cell, above the neighbouring amount. Small vertical context keeps
            # detector padding from clipping that amount. Any extra text still
            # has to pass strict whole-crop parsing; no token is discarded.
            margin = (high-low)*.15
            low, high = low-margin, high+margin
            box = [max(0,int(left)), max(0,int(low)), min(width,int(right)), min(height,int(high))]
            if box[2]-box[0] < 10 or box[3]-box[1] < 10:
                continue
            for name, pixels in views.items():
                if attempts >= 60:
                    return evidence, audit, ['CELL_RETRY_BUDGET_EXCEEDED']
                attempts += 1
                crop = Image.fromarray(pixels).crop(box)
                # Enlargement and white padding help the detector see small
                # digits. No pixel content is invented underneath an occlusion.
                crop = crop.resize((crop.width*2,crop.height*2),Image.Resampling.LANCZOS)
                crop = ImageOps.expand(crop,border=20,fill='white')
                path = output/f'cell-{i:04d}-{column}-{name}.png'
                crop.save(path)
                result = engine(str(path))
                texts = list(result.txts) if result.txts is not None else []
                scores = list(result.scores) if result.scores is not None else []
                boxes = plain(result.boxes)
                value = record({column:' '.join(texts)})
                # A crop-edge token may be a truncated number/sign. Reject it.
                near_edge = any(min(v[0] for v in b) <= 22 or max(v[0] for v in b) >= crop.width-22
                                or min(v[1] for v in b) <= 22 or max(v[1] for v in b) >= crop.height-22
                                for b in (boxes or []))
                accepted = bool(scores) and min(scores) >= .9 and not near_edge
                fields = {f:value[f] if accepted else None for f in group.split('+')}
                route = 'cell_'+name
                evidence.setdefault(i,{}).setdefault(group,{})[route] = fields
                audit.append({'row_index':i,'column':column,'route':route,'crop_box':box,
                              'image':path.name,'texts':texts,'scores':scores,'boxes':boxes,
                              'accepted_as_evidence':accepted,'edge_risk':near_edge,'fields':fields})
    return evidence, audit, []


def plain(value):
    return value.tolist() if hasattr(value, 'tolist') else list(value) if isinstance(value, tuple) else value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('image', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--page', type=int, help='One-based PDF page; otherwise an image')
    ap.add_argument('--render-only', action='store_true')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    blocked = []
    def deny(*a, **kw):
        blocked.append(True)
        raise RuntimeError('NETWORK_DISABLED_FOR_PRIVATE_OCR')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['OMP_NUM_THREADS'] = '4'
    import numpy as np
    from PIL import Image
    from jiaowopay_ocr.watermark import recognition_views, MAX_PIXELS, VERSION
    started = time.perf_counter()
    if args.page:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(args.image) as pdf:
            if not 1 <= args.page <= min(len(pdf),300):
                raise ValueError('PAGE_RANGE')
            page = pdf[args.page-1]
            try:
                bitmap = page.render(scale=2200/max(page.get_size()))
                try:
                    rgb = np.array(bitmap.to_pil().convert('RGB'))
                finally:
                    bitmap.close()
            finally:
                page.close()
    else:
        with Image.open(args.image) as im:
            if im.width * im.height > MAX_PIXELS or getattr(im,'n_frames',1) != 1:
                raise ValueError('IMAGE_SIZE_OR_FRAMES_LIMIT')
            rgb = np.asarray(im.convert('RGB'))
    Image.fromarray(rgb).save(args.output/'original.png')
    if args.render_only:
        return
    from rapidocr import RapidOCR
    views, preprocessing = recognition_views(rgb)
    views = {'original': rgb, **views}
    config = {'EngineConfig.onnxruntime.intra_op_num_threads':4,
              'EngineConfig.onnxruntime.inter_op_num_threads':1,
              'Global.log_level':'warning', 'Global.max_side_len':2400}
    engine = RapidOCR(params=config)
    observations, page_issues, timings = {}, {}, {}
    raw_views = {}
    for name, pixels in views.items():
        path = args.output/(name+'.png')
        Image.fromarray(pixels).save(path)
        begin = time.perf_counter()
        try:
            res = engine(str(path))
            raw = {'boxes':plain(res.boxes), 'texts':plain(res.txts), 'scores':plain(res.scores)}
            rows, issues = from_tokens(raw, with_geometry=True)
        except Exception as exc:
            raw = {'error':type(exc).__name__}
            rows, issues = [], ['VIEW_FAILED']
        timings[name] = time.perf_counter()-begin
        observations[name], page_issues[name], raw_views[name] = rows, issues, raw
    header_recovery = recover_currency_column(raw_views.get('original',{}),observations)
    proposals, issues = merge_observations(observations, page_issues)
    begin = time.perf_counter()
    cell_evidence, cell_audit, cell_issues = reread_cells(engine, views, proposals, args.output)
    proposals, issues = merge_observations(observations, page_issues, cell_evidence)
    issues += cell_issues
    timings['cell_retries'] = time.perf_counter()-begin
    # Only normalized proposals; all unmodified observations and original image
    # are retained separately. A COMPLETE marker never means accepted/ready.
    result = {'version':VERSION, 'image_sha256':hashlib.sha256(args.image.read_bytes()).hexdigest(),
              'extractor_version':'ocr-table-2-money-signs',
              'image_size':[rgb.shape[1], rgb.shape[0]], 'preprocessing':preprocessing,
              'raw_views':raw_views, 'observations':observations, 'page_issues':page_issues,
              'cell_audit':cell_audit,
              'header_recovery':header_recovery,
              'proposals':proposals, 'proposal_issues':issues, 'timings':timings,
              'elapsed_seconds':time.perf_counter()-started, 'blocked_network_attempts':len(blocked),
              'versions':{k:importlib.metadata.version(k) for k in ['rapidocr','onnxruntime','numpy','opencv-python','pillow']},
              'config':config, 'status':'needs_review', 'ledger_written':False}
    for i, proposal in enumerate(proposals):
        low, high = proposal['source_geometry']['row_y_bounds']
        pad = max(6, (high-low)*.25)
        box = [0,max(0,int(low-pad)),rgb.shape[1],min(rgb.shape[0],int(high+pad))]
        if box[3] <= box[1]:
            raise ValueError('INVALID_ROW_CROP')
        Image.fromarray(rgb).crop(box).save(args.output/f'row-{i+1:05}.png')
        proposal['review_crop'] = {'path':f'row-{i+1:05}.png','bbox':box}
    (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),encoding='utf-8')
    (args.output/'COMPLETE').write_text('candidate_only\n',encoding='ascii')
    print(json.dumps({'rows':len(proposals),'changed_rows':sum(bool(r['changes']) for r in proposals),
                      'seconds':round(result['elapsed_seconds'],2), 'network_attempts':len(blocked)}),flush=True)


if __name__ == '__main__':
    main()
