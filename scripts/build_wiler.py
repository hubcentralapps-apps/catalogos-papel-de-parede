from __future__ import annotations
import html as htmlmod
import io, json, re, time
from pathlib import Path
from urllib.parse import quote
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'/'wiler'
TIMEOUT=25
UA='Mozilla/5.0 (compatible; FK-Catalog-Image-Library/1.0)'

def load_items():
    out=[]
    for p in sorted(DATA_DIR.glob('wiler-*.json')):
        data=json.loads(p.read_text(encoding='utf-8'))
        for item in data.get('itens',[]):
            out.append({'f':'Wiler','c':data.get('colecao'),'s':data.get('slug'),'r':item.get('r'),'page':item.get('u')})
    return out

def add_candidate(cands,url,label=''):
    if not url: return
    url=htmlmod.unescape(str(url)).replace('\\u002F','/').replace('\\/','/')
    if url.startswith('//'): url='https:'+url
    if not url.startswith('http'): return
    if not any(h in url.lower() for h in ('vteximg','vtexassets','wiler.com.br')): return
    if url not in [u for u,_ in cands]: cands.append((url,label or ''))

def candidates_from_json(data):
    c=[]
    def walk(x):
        if isinstance(x,dict):
            if x.get('imageUrl'):
                add_candidate(c,x.get('imageUrl'),x.get('imageLabel') or x.get('imageText') or '')
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(data)
    return c

def candidates_from_html(txt):
    c=[]
    txt=htmlmod.unescape(txt).replace('\\u002F','/').replace('\\/','/')
    pats=[
      r'https?://[^"\'<>\s]+(?:vteximg|vtexassets)[^"\'<>\s]+',
      r'https?://[^"\'<>\s]+/arquivos/ids/[^"\'<>\s]+'
    ]
    for pat in pats:
        for u in re.findall(pat,txt,re.I): add_candidate(c,u)
    return c

def score(url,label,ref):
    t=(url+' '+label).lower(); r=ref.lower(); digits=re.sub(r'\D','',r)
    s=0
    if 'liso' in t: s+=20
    if r in t or digits in t: s+=12
    if 'texture' in t: s+=6
    if 'papeldeparede' in t or 'papel-de-parede' in t: s+=4
    if 'ambiente' in t: s-=25
    if 'rolo' in t: s-=20
    if 'thumbnail' in t or '-50-' in t or '-100-' in t: s-=10
    return s

def fetch_candidates(session,rec):
    ref=rec['r']; c=[]
    for endpoint in [
      f'https://www.wiler.com.br/api/catalog_system/pub/products/search?fq=alternateIds_RefId:{quote(ref)}',
      f'https://www.wiler.com.br/api/catalog_system/pub/products/search?ft={quote(ref)}'
    ]:
        try:
            r=session.get(endpoint,timeout=TIMEOUT)
            if r.ok and 'json' in r.headers.get('content-type','').lower():
                for x in candidates_from_json(r.json()): add_candidate(c,*x)
        except Exception: pass
    try:
        r=session.get(rec['page'],timeout=TIMEOUT)
        if r.ok:
            for x in candidates_from_html(r.text): add_candidate(c,*x)
    except Exception: pass
    return sorted(c,key=lambda x:score(x[0],x[1],ref),reverse=True)

def fetch_image(session,cands):
    for url,label in cands:
        try:
            r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
            if not r.ok or not r.content: continue
            if 'image' not in r.headers.get('content-type','').lower(): continue
            im=Image.open(io.BytesIO(r.content)); im.load(); im=im.convert('RGB')
            w,h=im.size
            if min(w,h)<650 or max(w,h)<850: continue
            return im,url,label
        except Exception: continue
    return None,None,None

def save(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=92,optimize=True,progressive=True)
    ImageOps.fit(im,(480,480),method=Image.Resampling.LANCZOS).save(tp,'JPEG',quality=82,optimize=True,progressive=True)
    return op,tp

def main():
    items=load_items(); session=requests.Session(); session.headers.update({'User-Agent':UA})
    ready=[]; failed=[]
    for n,rec in enumerate(items,1):
        base=OUT/rec['s']; ref=rec['r']; op=base/'originals'/f'{ref}.jpg'; tp=base/'thumbnails'/f'{ref}.jpg'
        if op.exists() and tp.exists():
            ready.append({**rec,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'status':'ready'})
            continue
        cands=fetch_candidates(session,rec)
        im,url,label=fetch_image(session,cands)
        if im is None:
            failed.append({**rec,'status':'not_found'}); print(f'WILER FAIL {ref}',flush=True); continue
        op,tp=save(im,base,ref)
        ready.append({**rec,'source_resolved':url,'source_label':label,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':im.width,'height':im.height,'status':'ready'})
        print(f'WILER OK {ref} {im.width}x{im.height}',flush=True); time.sleep(.05)
    manifest_path=ROOT/'dados'/'biblioteca-imagens.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    else:
        manifest={'items':[],'failures':[]}
    manifest['items']=[x for x in manifest.get('items',[]) if x.get('f')!='Wiler']+ready
    manifest['failures']=[x for x in manifest.get('failures',[]) if x.get('f')!='Wiler']+failed
    manifest['ready']=len(manifest['items']); manifest['failed']=len(manifest['failures'])
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'WILER READY={len(ready)} FAILED={len(failed)}')

if __name__=='__main__': main()
