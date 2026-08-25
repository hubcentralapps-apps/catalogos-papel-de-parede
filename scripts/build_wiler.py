from __future__ import annotations
import html as htmlmod
import io, json, re, time
from pathlib import Path
from urllib.parse import quote
import requests
from PIL import Image, ImageOps, ImageStat

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'/'wiler'
TIMEOUT=30
UA='Mozilla/5.0 (compatible; FK-Catalog-Clean-Library/4.0)'


def load_items():
    out=[]
    for p in sorted(DATA_DIR.glob('wiler-*.json')):
        data=json.loads(p.read_text(encoding='utf-8'))
        for item in data.get('itens',[]):
            out.append({'f':'Wiler','c':data.get('colecao'),'s':data.get('slug'),'r':item.get('r') or item.get('referencia'),'page':item.get('u')})
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
            if x.get('imageUrl'): add_candidate(c,x.get('imageUrl'),x.get('imageLabel') or x.get('imageText') or '')
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(data); return c


def candidates_from_html(txt):
    c=[]; txt=htmlmod.unescape(txt).replace('\\u002F','/').replace('\\/','/')
    for pat in [r'https?://[^"\'<>\s]+(?:vteximg|vtexassets)[^"\'<>\s]+',r'https?://[^"\'<>\s]+/arquivos/ids/[^"\'<>\s]+']:
        for u in re.findall(pat,txt,re.I): add_candidate(c,u)
    return c


def score(url,label,ref):
    t=(url+' '+label).lower(); r=ref.lower(); digits=re.sub(r'\D','',r); s=0
    if 'liso' in t: s+=35
    if any(k in t for k in ('amostra','padrao','pattern')): s+=20
    if r in t or digits in t: s+=15
    if any(k in t for k in ('texture','tramas','tacto','bambine')): s+=6
    if 'ambiente' in t: s-=40
    if 'rolo' in t: s-=30
    if 'thumbnail' in t or '-50-' in t or '-100-' in t: s-=15
    return s


def fetch_candidates(session,rec):
    ref=rec['r']; c=[]
    for endpoint in [f'https://www.wiler.com.br/api/catalog_system/pub/products/search?fq=alternateIds_RefId:{quote(ref)}',f'https://www.wiler.com.br/api/catalog_system/pub/products/search?ft={quote(ref)}']:
        try:
            r=session.get(endpoint,timeout=TIMEOUT)
            if r.ok and 'json' in r.headers.get('content-type','').lower():
                for x in candidates_from_json(r.json()): add_candidate(c,*x)
        except Exception: pass
    if rec.get('page'):
        try:
            r=session.get(rec['page'],timeout=TIMEOUT)
            if r.ok:
                for x in candidates_from_html(r.text): add_candidate(c,*x)
        except Exception: pass
    return sorted(c,key=lambda x:score(x[0],x[1],ref),reverse=True)


def row_stats(im,y):
    w,_=im.size
    samples=[im.getpixel((x,y)) for x in range(0,w,max(1,w//80))]
    vals=[sum(p)/3 for p in samples]
    mean=sum(vals)/len(vals); var=sum((v-mean)**2 for v in vals)/len(vals)
    return mean,var**0.5


def trim_catalog_bands(im):
    im=im.convert('RGB'); w,h=im.size
    if h<180: return im
    # Procura o início de uma faixa clara/cinza uniforme no rodapé. É onde
    # Wiler grava códigos como TX-2049 / TR-4044.
    search_top=max(int(h*.72),0)
    candidate=None
    for y in range(search_top,h-4):
        mean,std=row_stats(im,y)
        means=[]; stds=[]
        for yy in range(y,min(h,y+8)):
            m,s=row_stats(im,yy); means.append(m); stds.append(s)
        if sum(means)/len(means) > 185 and sum(stds)/len(stds) < 26:
            # confirma contraste com a textura imediatamente acima
            up=max(0,y-max(8,int(h*.03)))
            m_up,s_up=row_stats(im,up)
            if abs(m_up-sum(means)/len(means))>8 or s_up>sum(stds)/len(stds)+8:
                candidate=y; break
    if candidate and candidate>h*.68:
        return im.crop((0,0,w,candidate))
    return im


def fetch_image(session,cands):
    for url,label in cands:
        try:
            r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
            if not r.ok or not r.content or 'image' not in r.headers.get('content-type','').lower(): continue
            im=Image.open(io.BytesIO(r.content)); im.load(); im=trim_catalog_bands(im)
            w,h=im.size
            if min(w,h)<600 or max(w,h)<800: continue
            return im.convert('RGB'),url,label
        except Exception: continue
    return None,None,None


def save(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=94,optimize=True,progressive=True)
    ImageOps.fit(im,(520,520),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(tp,'JPEG',quality=86,optimize=True,progressive=True)
    return op,tp


def main():
    items=load_items(); session=requests.Session(); session.headers.update({'User-Agent':UA})
    ready=[]; failed=[]
    for n,rec in enumerate(items,1):
        base=OUT/rec['s']; ref=rec['r']
        # Reprocessa sempre: não reaproveita arquivo antigo com código/faixa.
        im,url,label=fetch_image(session,fetch_candidates(session,rec))
        if im is None:
            failed.append({**rec,'status':'not_found'}); print(f'WILER FAIL {ref}',flush=True); continue
        op,tp=save(im,base,ref)
        ready.append({**rec,'source_resolved':url,'source_label':label,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':im.width,'height':im.height,'status':'ready'})
        print(f'WILER OK {ref} {im.width}x{im.height}',flush=True); time.sleep(.03)
    manifest_path=ROOT/'dados'/'biblioteca-imagens.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'items':[],'failures':[]}
    manifest['items']=[x for x in manifest.get('items',[]) if x.get('f')!='Wiler']+ready
    manifest['failures']=[x for x in manifest.get('failures',[]) if x.get('f')!='Wiler']+failed
    manifest['ready']=len(manifest['items']); manifest['failed']=len(manifest['failures'])
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'WILER READY={len(ready)} FAILED={len(failed)}')

if __name__=='__main__': main()
