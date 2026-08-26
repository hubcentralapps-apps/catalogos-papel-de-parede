from __future__ import annotations

import base64
import gzip
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'dados' / 'colecoes'
OUT = ROOT / 'imagens' / 'home-finish'
MANIFEST = ROOT / 'dados' / 'biblioteca-imagens.json'
STAGING_ZIP = ROOT / 'staging' / 'home_finish_patch_load.zip'
UPLOAD_COMMIT = '11cc6677f24a3c0c8968388cbcbcf0af3cd6f438'

# Arquivos enviados pela cliente e validados visualmente.
# As duas MI ja vieram na orientacao correta: NAO girar novamente.
TARGETS = {
    '101012': 'BH101012.jpg',
    '101013': 'BH101013.jpg',
    '101015': 'BH101015.jpg',
    '101031': 'BH101031.jpg',
    '101037': 'BH101037.jpg',
    '201020': 'MI201020.jpg',
    '201021': 'MI201021.jpg',
}


def norm(ref):
    return re.sub(r'^(?:BH|MI)', '', str(ref).strip(), flags=re.I)


def normalize_collection(data):
    if data.get('records'):
        return data['records']
    vendor = data.get('fornecedor')
    col = data.get('colecao')
    slug = data.get('slug')
    return [
        {'f': vendor, 'c': col, 's': slug, 'r': item.get('r') or item.get('referencia'), 'u': item.get('u')}
        for item in data.get('itens', [])
    ]


def load_home_finish_records():
    by_norm = {}
    for p in sorted(DATA_DIR.glob('*.json')):
        data = json.loads(p.read_text(encoding='utf-8'))
        for rec in normalize_collection(data):
            if rec.get('f') == 'Home Finish' and rec.get('r'):
                by_norm[norm(rec['r'])] = rec
    for p in sorted(DATA_DIR.glob('*.json.gz.b64')):
        raw = gzip.decompress(base64.b64decode(p.read_text(encoding='ascii'))).decode('utf-8')
        data = json.loads(raw)
        for rec in normalize_collection(data):
            if rec.get('f') == 'Home Finish' and rec.get('r'):
                by_norm[norm(rec['r'])] = rec
    return by_norm


def load_zip_bytes():
    if STAGING_ZIP.exists():
        print(f'PATCH ZIP current:{STAGING_ZIP.relative_to(ROOT)}', flush=True)
        return STAGING_ZIP.read_bytes()
    # O workflow normal remove o ZIP de staging; recuperamos exatamente o upload do commit historico.
    spec = f'{UPLOAD_COMMIT}:staging/home_finish_patch_load.zip'
    print(f'PATCH ZIP history:{spec}', flush=True)
    return subprocess.check_output(['git', 'show', spec])


def open_uploaded_original(zf, filename):
    candidates = [
        f'home_finish_patch_load/originals/{filename}',
        f'originals/{filename}',
    ]
    for name in candidates:
        try:
            raw = zf.read(name)
            im = Image.open(io.BytesIO(raw))
            im.load()
            im = im.convert('RGB')
            if min(im.size) < 500:
                raise ValueError(f'image-too-small:{im.size}')
            return name, im
        except KeyError:
            pass
    raise FileNotFoundError(filename)


def save_pair(im, rec):
    ref = str(rec['r'])
    base = OUT / rec['s']
    od = base / 'originals'
    td = base / 'thumbnails'
    od.mkdir(parents=True, exist_ok=True)
    td.mkdir(parents=True, exist_ok=True)
    op = od / f'{ref}.jpg'
    tp = td / f'{ref}.jpg'
    im.save(op, 'JPEG', quality=95, optimize=True, progressive=True)
    ImageOps.fit(im, (520, 520), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)).save(
        tp, 'JPEG', quality=86, optimize=True, progressive=True
    )
    return op, tp


def patch_item(rec, im, source):
    op, tp = save_pair(im, rec)
    return {
        **rec,
        'source_resolved': source,
        'original': str(op.relative_to(ROOT)),
        'thumbnail': str(tp.relative_to(ROOT)),
        'width': im.width,
        'height': im.height,
        'status': 'ready',
        'patched': True,
    }


def main():
    records = load_home_finish_records()
    missing_records = [t for t in TARGETS if t not in records]
    if missing_records:
        raise RuntimeError('records-not-found:' + ','.join(missing_records))

    manifest = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {'items': [], 'failures': []}
    items = manifest.get('items', [])
    failures = manifest.get('failures', [])
    by_key = {(x.get('f'), x.get('c'), str(x.get('r'))): x for x in items}
    successes = set()

    zip_bytes = load_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for target, filename in TARGETS.items():
            rec = records[target]
            source_name, im = open_uploaded_original(zf, filename)
            by_key[(rec['f'], rec['c'], str(rec['r']))] = patch_item(
                rec, im, f'user-upload:{UPLOAD_COMMIT}:{source_name}'
            )
            successes.add(target)
            print(f'PATCH READY Home Finish {rec["r"]} {im.width}x{im.height} from {filename}', flush=True)

    if successes != set(TARGETS):
        raise RuntimeError(f'patch-incomplete:{len(successes)}/{len(TARGETS)}')

    new_items = list(by_key.values())
    new_failures = [
        x for x in failures
        if not (x.get('f') == 'Home Finish' and norm(x.get('r')) in successes)
    ]
    new_items.sort(key=lambda x: (x.get('f', ''), x.get('c', ''), str(x.get('r', ''))))
    new_failures.sort(key=lambda x: (x.get('f', ''), x.get('c', ''), str(x.get('r', ''))))
    MANIFEST.write_text(
        json.dumps(
            {'ready': len(new_items), 'failed': len(new_failures), 'items': new_items, 'failures': new_failures},
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    print('PATCH SUMMARY ' + ','.join(sorted(successes)) + f' success={len(successes)}/7', flush=True)


if __name__ == '__main__':
    main()
