from __future__ import annotations

import base64
import gzip
import io
import json
import re
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'dados' / 'colecoes'
OUT = ROOT / 'imagens' / 'home-finish'
MANIFEST = ROOT / 'dados' / 'biblioteca-imagens.json'
TIMEOUT = 35
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36'

# Patch deliberadamente pequeno: somente as referencias apontadas na validacao visual.
# Usamos arquivos de imagem diretos, nunca as paginas Home Finish (que bloqueiam o runner com 403).
REPLACE = {
    '101012': ['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101012-2.jpg'],
    '101013': ['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101013-2.jpg'],
    '101015': ['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101015-2.jpg'],
    '101031': ['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101031-2.jpg'],
    '101037': ['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101037-2.jpg'],
    '84858': ['https://static.wixstatic.com/media/76c9bb_630c20cb29c64ddfa2367d14e41eb312~mv2.jpg'],
}

# As duas abaixo nao precisam de outra arte: apenas rotacao de 180 graus.
ROTATE_180 = {
    '201020': ['https://www.homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-memorias-infancia-201020-sem-marca.jpg'],
    '201021': ['https://www.homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-memorias-infancia-201021-sem-marca.jpg'],
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


def decode_image_response(r):
    if r.status_code != 200 or not r.content:
        raise ValueError(f'not-image-{r.status_code}')
    ctype = (r.headers.get('content-type') or '').lower()
    if 'text/html' in ctype:
        raise ValueError('html-instead-of-image')
    im = Image.open(io.BytesIO(r.content))
    im.load()
    return im.convert('RGB')


def image_is_blank(im):
    small = ImageOps.fit(im.convert('RGB'), (96, 96), method=Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    return sum(stat.mean) / 3 > 246 and max(stat.stddev) < 5


def usable(im, ref):
    w, h = im.size
    if min(w, h) < 500 or max(w, h) < 700:
        return False
    if norm(ref) == '84858' and image_is_blank(im):
        return False
    return True


def fetch_image(session, url):
    headers = {'User-Agent': UA, 'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'}
    attempts = [url]
    if 'homefinish.com.br/' in url:
        hostless = url.split('://', 1)[-1]
        attempts += [
            'https://images.weserv.nl/?url=' + quote(hostless, safe='/:?=&%'),
            'https://wsrv.nl/?url=' + quote(url, safe=':/?=&%'),
        ]
    last = None
    for candidate in attempts:
        try:
            r = session.get(candidate, timeout=TIMEOUT, allow_redirects=True, headers=headers)
            return candidate, decode_image_response(r)
        except Exception as e:
            last = e
    raise last or ValueError('image-fetch-failed')


def fetch_first(session, urls, ref):
    errors = []
    for url in urls:
        try:
            resolved, im = fetch_image(session, url)
            if usable(im, ref):
                return url, resolved, im
            errors.append(f'unusable:{url}')
        except Exception as e:
            errors.append(f'{type(e).__name__}:{e}')
    raise ValueError('|'.join(errors[:6]) or 'no-image-source')


def save_pair(im, rec):
    ref = str(rec['r'])
    base = OUT / rec['s']
    od = base / 'originals'
    td = base / 'thumbnails'
    od.mkdir(parents=True, exist_ok=True)
    td.mkdir(parents=True, exist_ok=True)
    op = od / f'{ref}.jpg'
    tp = td / f'{ref}.jpg'
    im.save(op, 'JPEG', quality=94, optimize=True, progressive=True)
    ImageOps.fit(im, (520, 520), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)).save(
        tp, 'JPEG', quality=86, optimize=True, progressive=True
    )
    return op, tp


def find_existing_original(rec):
    expected = OUT / rec['s'] / 'originals' / f"{rec['r']}.jpg"
    if expected.exists():
        return expected
    target_norm = norm(rec['r'])
    for p in OUT.glob('*/originals/*.jpg'):
        if norm(p.stem) == target_norm:
            return p
    return None


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
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {'items': [], 'failures': []}
    items = manifest.get('items', [])
    failures = manifest.get('failures', [])
    by_key = {(x.get('f'), x.get('c'), str(x.get('r'))): x for x in items}
    successes = set()
    session = requests.Session()

    for target, urls in REPLACE.items():
        rec = records.get(target)
        if not rec:
            print(f'PATCH FAIL Home Finish {target}: record-not-found', flush=True)
            continue
        try:
            canonical, resolved, im = fetch_first(session, urls, rec['r'])
            by_key[(rec['f'], rec['c'], str(rec['r']))] = patch_item(rec, im, canonical)
            successes.add(target)
            print(f'PATCH READY Home Finish {rec["r"]} {im.width}x{im.height} via {resolved}', flush=True)
        except Exception as e:
            print(f'PATCH KEEP Home Finish {rec["r"]}: {e}', flush=True)

    for target, fallback_urls in ROTATE_180.items():
        rec = records.get(target)
        if not rec:
            print(f'ROTATE FAIL Home Finish {target}: record-not-found', flush=True)
            continue
        try:
            src = find_existing_original(rec)
            if src:
                im = Image.open(src)
                im.load()
                source = f'rotated-existing:{src.relative_to(ROOT)}'
            else:
                canonical, resolved, im = fetch_first(session, fallback_urls, rec['r'])
                source = f'rotated-restored:{canonical}'
            im = im.convert('RGB').rotate(180, expand=False)
            by_key[(rec['f'], rec['c'], str(rec['r']))] = patch_item(rec, im, source)
            successes.add(target)
            print(f'ROTATE READY Home Finish {rec["r"]} 180deg', flush=True)
        except Exception as e:
            print(f'ROTATE KEEP Home Finish {rec["r"]}: {e}', flush=True)

    new_items = list(by_key.values())
    new_failures = [x for x in failures if not (x.get('f') == 'Home Finish' and norm(x.get('r')) in successes)]
    new_items.sort(key=lambda x: (x.get('f', ''), x.get('c', ''), str(x.get('r', ''))))
    new_failures.sort(key=lambda x: (x.get('f', ''), x.get('c', ''), str(x.get('r', ''))))
    MANIFEST.write_text(
        json.dumps({'ready': len(new_items), 'failed': len(new_failures), 'items': new_items, 'failures': new_failures}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print('PATCH SUMMARY ' + ','.join(sorted(successes)) + f' success={len(successes)}/8', flush=True)


if __name__ == '__main__':
    main()
