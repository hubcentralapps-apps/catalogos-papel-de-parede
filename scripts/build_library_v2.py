from __future__ import annotations
import io, json, os, gzip, base64, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=30
UA='Mozilla/5.0 (compatible; FK-Catalog-Clean-Library/5.0)'
HF_MAP_PATH=ROOT/'dados'/'home-finish-urls-validadas-v2.json.gz.b64'

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
    s=''.join(HF_MAP_PATH.read_text(encoding='ascii').split())
    raw=gzip.decompress(base64.b64decode(s)).decode('utf-8')
    data=json.loads(raw)
    return data.get('items',{})

HF_MAP=load_hf_map()

def normalize_ref(ref):
    return re.sub(r'^(?:BH|MI)','',str(ref),flags=re.I)

def normalize_wix_url(url):
    if not url: return url
    m=re.match(r'^(https://static\.wixstatic\.com/media/[^?]+?~mv2\.(?:jpg|jpeg|png))(?:/v1/.*)?$',url,re.I)
    return m.group(1) if m else url

def fetch_image(session,url):
    r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
    if r.status_code!=200 or not r.content or 'image' not in r.headers.get('content-type','').lower():
        raise ValueError(f'not-image-{r.status_code}')
    im=Image.open(io.BytesIO(r.content)); im.load()
    return im.convert('RGB')

def save_pair(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'
    od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=94,optimize=True,progressive=True)
    ImageOps.fit(im,(520,520),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(tp,'JPEG',quality=86,optimize=True,progressive=True)
    return op,tp

def fetch_one(rec):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r'])
    base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    session=requests.Session(); session.headers.update({'User-Agent':UA})
    try:
        if vendor=='Home Finish':
            key=f'{col}|{normalize_ref(ref)}'
            entry=HF_MAP.get(key)
            if not entry or not entry.get('url'):
                raise ValueError('validated-hf-url-missing')
            source=entry['url']
            im=fetch_image(session,source)
        else:
            source=normalize_wix_url(rec.get('u'))
            if not source: raise ValueError('kantai-source-missing')
            im=fetch_image(session,source)
        w,h=im.size
        if min(w,h)<500 or max(w,h)<700:
            raise ValueError(f'low-resolution-{w}x{h}')
        op,tp=save_pair(im,base,ref)
        return 'ready',{**rec,'source_resolved':source,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':w,'height':h,'status':'ready'}
    except Exception as e:
        return 'fail',{**rec,'status':'not_found','error':str(e)[:180]}

def main():
    records=load_records(); ready=[]; failed=[]
    workers=int(os.environ.get('LIBRARY_WORKERS','8'))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs={ex.submit(fetch_one,r):r for r in records}
        for n,fut in enumerate(as_completed(futs),1):
            status,item=fut.result()
            (ready if status=='ready' else failed).append(item)
            print(f'[{n}/{len(records)}] {status.upper()} {item.get("f")} {item.get("c")} {item.get("r")}',flush=True)
    manifest_path=ROOT/'dados'/'biblioteca-imagens.json'
    old=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'items':[],'failures':[]}
    items=[x for x in old.get('items',[]) if x.get('f') not in {'Home Finish','Kantai'}]+ready
    failures=[x for x in old.get('failures',[]) if x.get('f') not in {'Home Finish','Kantai'}]+failed
    items.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    failures.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    manifest_path.write_text(json.dumps({'ready':len(items),'failed':len(failures),'items':items,'failures':failures},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'CLEAN READY={len(ready)} FAILED={len(failed)}')

if __name__=='__main__':
    main()
