from __future__ import annotations
import io, json, os, gzip, base64, re, html
from pathlib import Path
from urllib.parse import quote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=25
UA='Mozilla/5.0 (compatible; FK-Catalog-Image-Library/3.0)'

BASES=['https://www.homefinish.com.br/wp-content/uploads','https://homefinish.com.br/wp-content/uploads']
EXTS=['.jpg','-1.jpg','_1.jpg']

def hf_candidates(col, ref):
    ref_lookup=re.sub(r'^(?:BH|MI)','',str(ref),flags=re.I)
    out=[]
    def add(u):
        if u and u not in out: out.append(u)
    if col=='BIO Habitat':
        months=['2023/08','2024/09']; stems=[f'papel-parede-nacional-home-finish-bio-habitat-{ref_lookup}',f'papel-parede-nacional-homefinish-bio-habitat-{ref_lookup}']
    elif col=='Biomas':
        months=['2024/09','2023/08']; stems=[f'papel-parede-nacional-homefinish-biomas-{ref_lookup}',f'papel-parede-nacional-home-finish-biomas-{ref_lookup}']
    elif col=='Bosque da Imaginação':
        months=['2024/04','2024/09']; stems=[f'papel-parede-nacional-homefinish-bosque-da-imaginacao-{ref_lookup}',f'papel-parede-nacional-home-finish-bosque-da-imaginacao-{ref_lookup}']
    elif col=='Botânica':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-homefinish-botanica-{ref_lookup}',f'papel-parede-nacional-home-finish-botanica-{ref_lookup}']
    elif col=='Doce Estilo':
        months=['2024/09','2025/01']; stems=[f'papel-parede-nacional-doce-estilo-home-finish-{ref_lookup}',f'papel-parede-nacional-home-finish-doce-estilo-{ref_lookup}']
    elif col=='Era Uma Vez':
        months=['2025/10','2025/09']; stems=[f'{ref_lookup}-papel-parede-era-uma-vez-home-finish',f'papel-parede-era-uma-vez-home-finish-{ref_lookup}']
    elif col=='Flora':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-flora-{ref_lookup}',f'papel-parede-nacional-homefinish-flora-{ref_lookup}']
    elif col=='Natureza Lúdica':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-natureza-ludica-{ref_lookup}',f'papel-parede-nacional-homefinish-natureza-ludica-{ref_lookup}']
    elif col=='Provence':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-provence-{ref_lookup}',f'papel-parede-nacional-homefinish-provence-{ref_lookup}']
    elif col=='Tartan':
        months=['2025/10','2025/09']; stems=[f'{ref_lookup}-papel-parede-home-finish-studio-tartan',f'{ref_lookup}-papel-parede-homefinish-studio-tartan']
    elif col=='Passeio no Campo':
        months=['2026/04','2026/03']; stems=[f'papel-parede-homefinish-{ref_lookup}',f'papel-parede-home-finish-{ref_lookup}']
    else:
        return out
    for b in BASES:
        for m in months:
            for st in stems:
                for ex in EXTS: add(f'{b}/{m}/{st}{ex}')
    return out

def normalize_collection(data):
    if data.get('records'):
        return data['records']
    vendor=data.get('fornecedor'); col=data.get('colecao'); slug=data.get('slug')
    return [{'f':vendor,'c':col,'s':slug,'r':item.get('r'),'u':item.get('u'),'crop':item.get('crop') or data.get('crop_default')} for item in data.get('itens',[])]

def load_records():
    records=[]
    for p in sorted(DATA_DIR.glob('*.json')):
        records.extend(normalize_collection(json.loads(p.read_text(encoding='utf-8'))))
    for p in sorted(DATA_DIR.glob('*.json.gz.b64')):
        raw=gzip.decompress(base64.b64decode(p.read_text(encoding='ascii'))).decode('utf-8')
        records.extend(normalize_collection(json.loads(raw)))
    seen=set(); dedup=[]
    for r in records:
        k=(r.get('f'),r.get('c'),str(r.get('r')))
        if k in seen: continue
        seen.add(k); dedup.append(r)
    return dedup

def find_download_jpg(page_html, base_url):
    txt=html.unescape(page_html).replace('\\/','/')
    pats=[
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>[^<]{0,120}BAIXAR\s+JPG',
        r'href=["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^>]*>[^<]{0,160}BAIXAR\s+JPG',
        r'["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^<>]{0,350}BAIXAR\s+JPG'
    ]
    for pat in pats:
        m=re.search(pat,txt,re.I|re.S)
        if m: return urljoin(base_url,m.group(1))
    pos=re.search(r'BAIXAR\s+JPG',txt,re.I)
    if pos:
        chunk=txt[max(0,pos.start()-1600):pos.end()+1600]
        urls=re.findall(r'https?://[^"\'<>\s]+\.jpe?g(?:\?[^"\'<>\s]*)?',chunk,re.I)
        if urls: return urls[-1]
    return None

def discover_hf_official(ref, collection):
    ref_s=str(ref)
    lookup=re.sub(r'^(?:BH|MI)','',ref_s,flags=re.I)
    s=requests.Session(); s.headers.update({'User-Agent':UA})
    for term in (ref_s,lookup):
        try:
            api=f'https://www.homefinish.com.br/wp-json/wc/store/v1/products?search={quote(term)}&per_page=30'
            r=s.get(api,timeout=TIMEOUT); r.raise_for_status()
            products=r.json() if isinstance(r.json(),list) else []
            ranked=[]
            for p in products:
                vals=' '.join(str(p.get(k,'') or '') for k in ('sku','name','slug','permalink'))
                if not re.search(r'(^|[^0-9])'+re.escape(lookup)+r'([^0-9]|$)',vals):
                    continue
                score=20 if str(p.get('sku','')) in {ref_s,lookup} else 0
                if collection.lower() in vals.lower(): score+=5
                ranked.append((score,p))
            ranked.sort(key=lambda x:x[0],reverse=True)
            for _,p in ranked[:3]:
                page=p.get('permalink')
                if not page: continue
                pr=s.get(page,timeout=TIMEOUT); pr.raise_for_status()
                jpg=find_download_jpg(pr.text,page)
                if jpg and not any(x in jpg.lower() for x in ('watermark','marca-dagua','marca_dagua','com-marca')):
                    return jpg
        except Exception:
            pass
    return None

def fetch_one(rec):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r'])
    base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    orig=base/'originals'/f'{ref}.jpg'; thumb=base/'thumbnails'/f'{ref}.jpg'
    if orig.exists() and thumb.exists():
        try:
            with Image.open(orig) as im: w,h=im.size
            return 'ready',{**rec,'original':str(orig.relative_to(ROOT)),'thumbnail':str(thumb.relative_to(ROOT)),'width':w,'height':h,'status':'ready'}
        except Exception:
            pass
    s=requests.Session(); s.headers.update({'User-Agent':UA})
    urls=[]
    if vendor=='Kantai':
        if rec.get('u'): urls=[rec.get('u')]
    else:
        official=discover_hf_official(ref,col)
        if official: urls.append(official)
        urls += [u for u in hf_candidates(col,ref) if u not in urls]
    im=None; source=None
    for url in urls:
        try:
            r=s.get(url,timeout=TIMEOUT,allow_redirects=True)
            if r.status_code!=200 or not r.content or 'image' not in r.headers.get('content-type','').lower(): continue
            test=Image.open(io.BytesIO(r.content)); test.load(); w,h=test.size
            if min(w,h)<500 or max(w,h)<700: continue
            im=test.convert('RGB'); source=url; break
        except Exception:
            continue
    if im is None: return 'fail',{**rec,'status':'not_found'}
    if vendor=='Kantai' and rec.get('crop') in {'top-half','kt'}:
        w,h=im.size; im=im.crop((0,0,w,h//2))
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    im.save(orig,'JPEG',quality=92,optimize=True,progressive=True)
    ImageOps.fit(im,(480,480),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(thumb,'JPEG',quality=82,optimize=True,progressive=True)
    return 'ready',{**rec,'source_resolved':source,'original':str(orig.relative_to(ROOT)),'thumbnail':str(thumb.relative_to(ROOT)),'width':im.width,'height':im.height,'status':'ready'}

def main():
    records=load_records(); manifest=[]; failures=[]
    workers=int(os.environ.get('LIBRARY_WORKERS','8'))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs={ex.submit(fetch_one,r):r for r in records}
        done=0
        for fut in as_completed(futs):
            done+=1; rec=futs[fut]
            try: status,item=fut.result()
            except Exception as e: status,item='fail',{**rec,'status':'error','error':str(e)[:200]}
            if status=='ready': manifest.append(item); print(f'[{done}/{len(records)}] OK {item.get("f")} {item.get("c")} {item.get("r")}',flush=True)
            else: failures.append(item); print(f'[{done}/{len(records)}] FAIL {item.get("f")} {item.get("c")} {item.get("r")}',flush=True)
    (ROOT/'dados').mkdir(exist_ok=True)
    manifest.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    failures.sort(key=lambda x:(x.get('f',''),x.get('c',''),str(x.get('r',''))))
    (ROOT/'dados'/'biblioteca-imagens.json').write_text(json.dumps({'ready':len(manifest),'failed':len(failures),'items':manifest,'failures':failures},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'READY={len(manifest)} FAILED={len(failures)}')

if __name__=='__main__': main()
