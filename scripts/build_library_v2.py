from __future__ import annotations
import io, json, os, gzip, base64, re, html
from pathlib import Path
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from PIL import Image, ImageOps, ImageStat

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=35
UA='Mozilla/5.0 (compatible; FK-Catalog-Clean-Library/9.0)'
HF_MAP_DIR=ROOT/'dados'/'home-finish-urls'

# Fontes limpas confirmadas/derivadas do botão oficial BAIXAR JPG.
# São tentadas antes da biblioteca antiga, evitando imagens com marca d'água.
HF_CLEAN_OVERRIDES={
    '101012':['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101012-2.jpg'],
    '101013':['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101013-2.jpg'],
    '101015':['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101015-2.jpg'],
    '101031':['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101031-2.jpg'],
    '101037':['https://homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-bio-habitat-101037-2.jpg'],
    '201020':['https://www.homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-memorias-infancia-201020-sem-marca.jpg'],
    '201021':['https://www.homefinish.com.br/wp-content/uploads/2023/08/papel-parede-nacional-home-finish-memorias-infancia-201021-sem-marca.jpg'],
    # alternativa limpa para o 84858, cuja imagem antiga estava praticamente vazia
    '84858':['https://static.wixstatic.com/media/76c9bb_630c20cb29c64ddfa2367d14e41eb312~mv2.jpg'],
}


def normalize_collection(data):
    if data.get('records'): return data['records']
    vendor=data.get('fornecedor'); col=data.get('colecao'); slug=data.get('slug')
    return [{'f':vendor,'c':col,'s':slug,'r':item.get('r') or item.get('referencia'),'u':item.get('u'),'crop':item.get('crop') or data.get('crop_default')} for item in data.get('itens',[])]


def load_records():
    records=[]
    for p in sorted(DATA_DIR.glob('*.json')):
        records.extend(normalize_collection(json.loads(p.read_text(encoding='utf-8'))))
    for p in sorted(DATA_DIR.glob('*.json.gz.b64')):
        raw=gzip.decompress(base64.b64decode(p.read_text(encoding='ascii'))).decode('utf-8')
        records.extend(normalize_collection(json.loads(raw)))
    seen=set(); out=[]
    for r in records:
        if r.get('f') not in {'Home Finish','Kantai'}: continue
        k=(r.get('f'),r.get('c'),str(r.get('r')))
        if k in seen: continue
        seen.add(k); out.append(r)
    return out


def load_hf_map():
    items={}
    for p in sorted(HF_MAP_DIR.glob('part-*.json')):
        items.update(json.loads(p.read_text(encoding='utf-8')).get('items',{}))
    print(f'HF_MAP loaded={len(items)}',flush=True)
    if len(items)!=619: raise RuntimeError(f'hf-map-incomplete-{len(items)}')
    return items

HF_MAP=load_hf_map()


def normalize_ref(ref):
    return re.sub(r'^(?:BH|MI)','',str(ref),flags=re.I)


def normalize_wix_url(url):
    if not url: return url
    m=re.match(r'^(https://static\.wixstatic\.com/media/[^?]+?~mv2\.(?:jpg|jpeg|png))(?:/v1/.*)?$',url,re.I)
    return m.group(1) if m else url


def decode_image_response(r):
    if r.status_code!=200 or not r.content: raise ValueError(f'not-image-{r.status_code}')
    im=Image.open(io.BytesIO(r.content)); im.load(); return im.convert('RGB')


def fetch_image(session,url):
    try:
        r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
        return decode_image_response(r)
    except Exception:
        if 'homefinish.com.br/' not in url: raise
        u=url.split('://',1)[-1]
        proxied='https://images.weserv.nl/?url='+quote(u,safe='/:?=&%')
        r=session.get(proxied,timeout=TIMEOUT,allow_redirects=True)
        return decode_image_response(r)


def image_is_blank(im):
    small=ImageOps.fit(im.convert('RGB'),(96,96),method=Image.Resampling.BILINEAR)
    stat=ImageStat.Stat(small)
    return sum(stat.mean)/3 > 246 and max(stat.stddev) < 5


def usable(im,ref):
    w,h=im.size
    if min(w,h)<500 or max(w,h)<700: return False
    # Só rejeita branco praticamente puro; texturas claras continuam válidas.
    if normalize_ref(ref)=='84858' and image_is_blank(im): return False
    return True


def media_urls(text,ref,base_url='https://www.homefinish.com.br/'):
    txt=html.unescape(text).replace('\\/','/')
    lookup=normalize_ref(ref); urls=[]
    patterns=[
        r'(https?://[^\s\]\)"\'<>]+/wp-content/uploads/[^\s\]\)"\'<>]+\.(?:jpe?g|png)(?:\?[^\s\]\)"\'<>]*)?)',
        r'\((https?://[^\s\)]+\.(?:jpe?g|png)(?:\?[^\s\)]*)?)\)',
        r'(?:href|src)=["\']([^"\']+\.(?:jpe?g|png)(?:\?[^"\']*)?)["\']'
    ]
    for pat in patterns:
        for m in re.finditer(pat,txt,re.I):
            u=urljoin(base_url,m.group(1).replace('&amp;','&'))
            low=u.lower()
            if lookup not in u: continue
            if any(x in low for x in ('watermark','marca-dagua','marca_dagua','logo','cropped-','inverted')): continue
            urls.append(u)
    return sorted(dict.fromkeys(urls),key=lambda u:(0 if ('sem-marca' in u.lower() or '-2.jpg' in u.lower()) else 1,0 if 'wp-content/uploads' in u else 1,len(u)))


def candidate_pages(ref):
    lookup=normalize_ref(ref)
    return [
        f'https://www.homefinish.com.br/papel-de-parede/{lookup}/',
        f'https://homefinish.com.br/papel-de-parede/{lookup}/',
        f'https://www.homefinish.com.br/papel-de-parede/papel-de-parede-{lookup}/',
        f'https://homefinish.com.br/papel-de-parede/papel-de-parede-{lookup}/',
    ]


def discover_hf_official(session,ref):
    for page in candidate_pages(ref):
        try:
            r=session.get(page,timeout=TIMEOUT,allow_redirects=True)
            if r.ok:
                for u in media_urls(r.text,ref,r.url):
                    try:
                        im=fetch_image(session,u)
                        if usable(im,ref): return u,im
                    except Exception: pass
        except Exception: pass
    for page in candidate_pages(ref):
        try:
            r=session.get('https://r.jina.ai/'+page,timeout=TIMEOUT)
            if not r.ok: continue
            for u in media_urls(r.text,ref,page):
                try:
                    im=fetch_image(session,u)
                    if usable(im,ref): return u,im
                except Exception: pass
        except Exception: pass
    return None,None


def fetch_first(session,urls,ref):
    for u in urls:
        try:
            im=fetch_image(session,u)
            if usable(im,ref): return u,im
        except Exception: pass
    return None,None


def apply_kantai_crop(rec,im):
    if rec.get('crop') in {'top-half','kt'}:
        w,h=im.size
        return im.crop((0,0,w,h//2))
    return im


def save_pair(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=94,optimize=True,progressive=True)
    ImageOps.fit(im,(520,520),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(tp,'JPEG',quality=86,optimize=True,progressive=True)
    return op,tp


def existing_item(rec,old_by_key):
    key=(rec.get('f'),rec.get('c'),str(rec.get('r')))
    item=old_by_key.get(key)
    if not item: return None
    op=ROOT/item.get('original',''); tp=ROOT/item.get('thumbnail','')
    return item if op.exists() and tp.exists() else None


def fetch_one(rec,old_by_key):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r']); norm=normalize_ref(ref)
    base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    session=requests.Session(); session.headers.update({'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,image/avif,image/webp,image/apng,*/*;q=0.8'})
    try:
        if vendor=='Home Finish':
            source=im=None
            if norm in HF_CLEAN_OVERRIDES:
                source,im=fetch_first(session,HF_CLEAN_OVERRIDES[norm],ref)
            if im is None:
                entry=HF_MAP.get(f'{col}|{norm}'); mapped=(entry or {}).get('url')
                if mapped and mapped.startswith(('http://','https://')):
                    source,im=fetch_first(session,[mapped],ref)
            if im is None:
                source,im=discover_hf_official(session,ref)
            if im is None or not source: raise ValueError('clean-hf-source-not-found')
        else:
            source=normalize_wix_url(rec.get('u'))
            if not source: raise ValueError('kantai-source-missing')
            im=fetch_image(session,source)
            im=apply_kantai_crop(rec,im)
            if not usable(im,ref): raise ValueError(f'low-resolution-{im.width}x{im.height}')
        op,tp=save_pair(im,base,ref)
        return 'ready',{**rec,'source_resolved':source,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':im.width,'height':im.height,'status':'ready'}
    except Exception as e:
        old=existing_item(rec,old_by_key)
        if old:
            return 'kept',{**old,'status':'ready','kept_after_error':str(e)[:160]}
        return 'fail',{**rec,'status':'not_found','error':str(e)[:180]}


def main():
    manifest_path=ROOT/'dados'/'biblioteca-imagens.json'
    old=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'items':[],'failures':[]}
    old_by_key={(x.get('f'),x.get('c'),str(x.get('r'))):x for x in old.get('items',[])}
    records=load_records(); ready=[]; failed=[]; workers=int(os.environ.get('LIBRARY_WORKERS','8'))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs={ex.submit(fetch_one,r,old_by_key):r for r in records}
        for n,fut in enumerate(as_completed(futs),1):
            status,item=fut.result()
            if status in {'ready','kept'}: ready.append(item)
            else: failed.append(item)
            print(f'[{n}/{len(records)}] {status.upper()} {item.get("f")} {item.get("c")} {item.get("r")}',flush=True)
    # Mantém Wiler intacto e substitui somente Home Finish/Kantai pelo resultado consolidado.
    items=[x for x in old.get('items',[]) if x.get('f') not in {'Home Finish','Kantai'}]+ready
    failures=[x for x in old.get('failures',[]) if x.get('f') not in {'Home Finish','Kantai'}]+failed
    items.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r','')))); failures.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    manifest_path.write_text(json.dumps({'ready':len(items),'failed':len(failures),'items':items,'failures':failures},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'CLEAN READY={len(ready)} FAILED={len(failed)}',flush=True)

if __name__=='__main__': main()
