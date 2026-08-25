from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'dados' / 'colecoes'
OUT = ROOT / 'imagens' / 'home-finish'
MANIFEST = ROOT / 'dados' / 'biblioteca-imagens.json'
TIMEOUT = 35
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36'

# Somente as imagens Home Finish apontadas na validacao visual.
REPLACE = {
    '101012': 'https://homefinish.com.br/papel-de-parede/101012/',
    '101013': 'https://homefinish.com.br/papel-de-parede/101013/',
    '101015': 'https://homefinish.com.br/papel-de-parede/101015/',
    '101031': 'https://homefinish.com.br/papel-de-parede/101031/',
    '101037': 'https://homefinish.com.br/papel-de-parede/101037/',
    '84858': 'https://homefinish.com.br/papel-de-parede/papel-de-parede-off-white-botanica-tropical-chic-84858/',
}
ROTATE_180 = {'201020', '201021'}


def norm(ref):
    return re.sub(r'^(?:BH|MI)', '', str(ref).strip(), flags=re.I)


def normalize_collection(data):
    if data.get('records'):
        return data['records']
    vendor = data.get('fornecedor')
    col = data.get('colecao')
    slug = data.get('slug')
    return [
        {
            'f': vendor,
            'c': col,
            's': slug,
            'r': item.get('r') or item.get('referencia'),
            'u': item.get('u'),
        }
        for item in data.get('itens', [])
    ]


def load_home_finish_records():
    by_norm = {}
    for p in sorted(DATA_DIR.glob('*.json')):
        data = json.loads(p.read_text(encoding='utf-8'))
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
    # 84858 foi a imagem vazia apontada no catalogo: nao aceitamos novamente um JPG quase branco.
    if norm(ref) == '84858' and image_is_blank(im):
        return False
    return True


def fetch_image(session, url, referer=None):
    headers = {
        'User-Agent': UA,
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }
    if referer:
        headers['Referer'] = referer
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
            im = decode_image_response(r)
            return candidate, im
        except Exception as e:
            last = e
    raise last or ValueError('image-fetch-failed')


def page_candidates(session, page_url, ref):
    r = session.get(
        page_url,
        timeout=TIMEOUT,
        allow_redirects=True,
        headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml'},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    out = []

    download = None
    for a in soup.find_all('a', href=True):
        text = ' '.join(a.stripped_strings).upper()
        href = urljoin(r.url, a['href'])
        if 'BAIXAR JPG' in text or ('download' in (a.get('class') or []) and href.lower().endswith(('.jpg', '.jpeg', '.png'))):
            download = href
            break

    meta_images = []
    for attrs in (
        {'property': 'og:image'},
        {'name': 'twitter:image'},
        {'property': 'og:image:secure_url'},
    ):
        m = soup.find('meta', attrs=attrs)
        if m and m.get('content'):
            meta_images.append(urljoin(r.url, m['content']))

    inline = []
    needle = norm(ref)
    for tag in soup.find_all(['img', 'a']):
        value = tag.get('src') or tag.get('href')
        if not value:
            continue
        u = urljoin(r.url, value)
        low = u.lower()
        if needle in u and low.endswith(('.jpg', '.jpeg', '.png')):
            if not any(x in low for x in ('logo', 'marca-home-finish', 'bandeira-brasil')):
                inline.append(u)

    # Para 84858, priorizamos a imagem de destaque se ela tiver conteudo visivel.
    if norm(ref) == '84858':
        out.extend(meta_images)
        out.extend(inline)
        if download:
            out.append(download)
    else:
        if download:
            out.append(download)
        out.extend(meta_images)
        out.extend(inline)

    return r.url, list(dict.fromkeys(out))


def fetch_official(session, page_url, ref):
    page_final, candidates = page_candidates(session, page_url, ref)
    errors = []
    for u in candidates:
        try:
            resolved, im = fetch_image(session, u, referer=page_final)
            if usable(im, ref):
                return u, resolved, im
            errors.append(f'unusable:{u}')
        except Exception as e:
            errors.append(f'{type(e).__name__}:{u}')
    raise ValueError('no-usable-official-image|' + '|'.join(errors[:8]))


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

    # Substitui somente as seis referencias com imagem ruim.
    for target, page in REPLACE.items():
        rec = records.get(target)
        if not rec:
            print(f'PATCH FAIL Home Finish {target}: record-not-found', flush=True)
            continue
        try:
            canonical, resolved, im = fetch_official(session, page, rec['r'])
            new_item = patch_item(rec, im, canonical)
            by_key[(rec['f'], rec['c'], str(rec['r']))] = new_item
            successes.add(target)
            print(f'PATCH READY Home Finish {rec["r"]} {im.width}x{im.height} {canonical}', flush=True)
        except Exception as e:
            print(f'PATCH KEEP Home Finish {rec["r"]}: {e}', flush=True)

    # MI201020 e MI201021: nao busca outra imagem; gira o arquivo atual 180 graus.
    for target in ROTATE_180:
        rec = records.get(target)
        if not rec:
            print(f'ROTATE FAIL Home Finish {target}: record-not-found', flush=True)
            continue
        try:
            src = find_existing_original(rec)
            if not src:
                raise FileNotFoundError('existing-original-not-found')
            im = Image.open(src)
            im.load()
            im = im.convert('RGB').rotate(180, expand=False)
            new_item = patch_item(rec, im, f'rotated-existing:{src.relative_to(ROOT)}')
            by_key[(rec['f'], rec['c'], str(rec['r']))] = new_item
            successes.add(target)
            print(f'ROTATE READY Home Finish {rec["r"]} 180deg', flush=True)
        except Exception as e:
            print(f'ROTATE KEEP Home Finish {rec["r"]}: {e}', flush=True)

    # Mantem todo o restante exatamente como estava. Remove failure apenas das referencias corrigidas.
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
    print('PATCH SUMMARY ' + ','.join(sorted(successes)) + f' success={len(successes)}/8', flush=True)


if __name__ == '__main__':
    main()
