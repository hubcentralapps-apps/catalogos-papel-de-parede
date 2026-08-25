from __future__ import annotations
import io, json, os, gzip, base64, re, html
from pathlib import Path
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=35
UA='Mozilla/5.0 (compatible; FK-Catalog-Clean-Library/7.0)'
HF_MAP_DIR=ROOT/'dados'/'home-finish-urls'

def normalize_collection(data):
    if data.get('records'): return data['records']
    vendor=data.get('fornecedor'); col=data.get('colecao'); slug=data.get('slug')
    return [{'f':vendor,'c':col,'s':slug,'r':item.get('r') or item.get('referencia'),'u':item.get('u')} for item in data.get('itens',[])]

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
        # Home Finish bloqueia alguns IPs de datacenter. O proxy é usado somente
        # durante a montagem da biblioteca; o catálogo final continua 100% local.
        if 'homefinish.com.br/' not in url: raise
        u=url.split('://',1)[-1]
        proxied='https://images.weserv.nl/?url='+quote(u,safe='/:?=&%')
        r=session.get(proxied,timeout=TIMEOUT,allow_redirects=True)
        return decode_image_response(r)

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
    return sorted(dict.fromkeys(urls),key=lambda u:(0 if 'wp-content/uploads' in u else 1,0 if 'papel-parede' in u.lower() else 1,len(u)))

def candidate_pages(ref):
    lookup=normalize_ref(ref)
    return [
        f'https://www.homefinish.com.br/papel-de-parede/papel-de-parede-{lookup}/',
        f'https://homefinish.com.br/papel-de-parede/papel-de-parede-{lookup}/',
        f'https://www.homefinish.com.br/papel-de-parede/{lookup}/',
        f'https://homefinish.com.br/papel-de-parede/{lookup}/',
    ]

def discover_hf_official(session,ref):
    # 1) página oficial direta
    for page in candidate_pages(ref):
        try:
            r=session.get(page,timeout=TIMEOUT,allow_redirects=True)
            if r.ok:
                for u in media_urls(r.text,ref,r.url):
                    try:
                        im=fetch_image(session,u); w,h=im.size
                        if min(w,h)>=500 and max(w,h)>=700: return u,im
                    except Exception: pass
        except Exception: pass
    # 2) Jina Reader como ponte de leitura para páginas bloqueadas a datacenters.
    for page in candidate_pages(ref):
        try:
            jr='https://r.jina.ai/'+page
            r=session.get(jr,timeout=TIMEOUT)
            if not r.ok: continue
            for u in media_urls(r.text,ref,page):
                try:
                    im=fetch_image(session,u); w,h=im.size
                    if min(w,h)>=500 and max(w,h)>=700: return u,im
                except Exception: pass
        except Exception: pass
    return None,None

def save_pair(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=94,optimize=True,progressive=True)
    ImageOps.fit(im,(520,520),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(tp,'JPEG',quality=86,optimize=True,progressive=True)
    return op,tp

def fetch_one(rec):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r']); base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    session=requests.Session(); session.headers.update({'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,image/avif,image/webp,image/apng,*/*;q=0.8'})
    try:
        if vendor=='Home Finish':
            key=f'{col}|{normalize_ref(ref)}'; entry=HF_MAP.get(key); source=(entry or {}).get('url'); im=None
            if source and source.startswith(('http://','https://')):
                try: im=fetch_image(session,source)
                except Exception: im=None
            if im is None: source,im=discover_hf_official(session,ref)
            if im is None or not source: raise ValueError('clean-hf-source-not-found')
        else:
            source=normalize_wix_url(rec.get('u'))
            if not source: raise ValueError('kantai-source-missing')
            im=fetch_image(session,source)
        w,h=im.size
        if min(w,h)<500 or max(w,h)<700: raise ValueError(f'low-resolution-{w}x{h}')
        op,tp=save_pair(im,base,ref)
        return 'ready',{**rec,'source_resolved':source,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':w,'height':h,'status':'ready'}
    except Exception as e:
        return 'fail',{**rec,'status':'not_found','error':str(e)[:180]}

def main():
    records=load_records(); ready=[]; failed=[]; workers=int(os.environ.get('LIBRARY_WORKERS','8'))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs={ex.submit(fetch_one,r):r for r in records}
        for n,fut in enumerate(as_completed(futs),1):
            status,item=fut.result(); (ready if status=='ready' else failed).append(item)
            print(f'[{n}/{len(records)}] {status.upper()} {item.get("f")} {item.get("c")} {item.get("r")}',flush=True)
    manifest_path=ROOT/'dados'/'biblioteca-imagens.json'; old=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'items':[],'failures':[]}
    items=[x for x in old.get('items',[]) if x.get('f') not in {'Home Finish','Kantai'}]+ready
    failures=[x for x in old.get('failures',[]) if x.get('f') not in {'Home Finish','Kantai'}]+failed
    items.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r','')))); failures.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    manifest_path.write_text(json.dumps({'ready':len(items),'failed':len(failures),'items':items,'failures':failures},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'CLEAN READY={len(ready)} FAILED={len(failed)}',flush=True)

if __name__=='__main__': main()
