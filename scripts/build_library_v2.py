from __future__ import annotations
import io, json, os, gzip, base64, re
from pathlib import Path
from urllib.parse import quote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=10
UA='Mozilla/5.0 (compatible; FK-Catalog-Image-Library/2.0)'

BASES=['https://www.homefinish.com.br/wp-content/uploads','https://homefinish.com.br/wp-content/uploads']
EXTS=['.jpg','-1.jpg','_1.jpg']

def hf_candidates(col, ref):
    out=[]
    def add(u):
        if u and u not in out: out.append(u)
    if col=='BIO Habitat':
        months=['2023/08','2024/09']; stems=[f'papel-parede-nacional-home-finish-bio-habitat-{ref}',f'papel-parede-nacional-homefinish-bio-habitat-{ref}']
    elif col=='Biomas':
        months=['2024/09','2023/08']; stems=[f'papel-parede-nacional-homefinish-biomas-{ref}',f'papel-parede-nacional-home-finish-biomas-{ref}']
    elif col=='Bosque da Imaginação':
        months=['2024/04','2024/09']; stems=[f'papel-parede-nacional-homefinish-bosque-da-imaginacao-{ref}',f'papel-parede-nacional-home-finish-bosque-da-imaginacao-{ref}']
    elif col=='Botânica':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-homefinish-botanica-{ref}',f'papel-parede-nacional-home-finish-botanica-{ref}']
    elif col=='Doce Estilo':
        months=['2024/09','2025/01']; stems=[f'papel-parede-nacional-doce-estilo-home-finish-{ref}',f'papel-parede-nacional-home-finish-doce-estilo-{ref}']
    elif col=='Era Uma Vez':
        months=['2025/10','2025/09']; stems=[f'{ref}-papel-parede-era-uma-vez-home-finish',f'papel-parede-era-uma-vez-home-finish-{ref}']
    elif col=='Flora':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-flora-{ref}',f'papel-parede-nacional-homefinish-flora-{ref}']
    elif col=='Natureza Lúdica':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-natureza-ludica-{ref}',f'papel-parede-nacional-homefinish-natureza-ludica-{ref}']
    elif col=='Provence':
        months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-provence-{ref}',f'papel-parede-nacional-homefinish-provence-{ref}']
    elif col=='Tartan':
        months=['2025/10','2025/09']; stems=[f'{ref}-papel-parede-home-finish-studio-tartan',f'{ref}-papel-parede-homefinish-studio-tartan']
    elif col=='Passeio no Campo':
        months=['2026/04','2026/03']; stems=[f'papel-parede-homefinish-{ref}',f'papel-parede-home-finish-{ref}']
    else:
        return out
    for b in BASES[:1]:
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

def wp_original(url):
    if not url: return url
    url=url.split(' ')[0].strip()
    return re.sub(r'-\d+x\d+(?=\.(?:jpe?g|png|webp)(?:\?|$))','',url,flags=re.I)

def image_candidates_from_html(txt, ref):
    soup=BeautifulSoup(txt,'html.parser')
    out=[]; digits=re.sub(r'\D','',str(ref))
    for img in soup.find_all('img'):
        alt=' '.join([img.get('alt',''),img.get('title','')])
        urls=[]
        for a in ('data-src','data-lazy-src','src'):
            if img.get(a): urls.append(img.get(a))
        for a in ('srcset','data-srcset'):
            if img.get(a):
                for part in img.get(a).split(','): urls.append(part.strip().split(' ')[0])
        for u in urls:
            if not u: continue
            u=urljoin('https://homefinish.com.br/',u)
            t=(u+' '+alt).lower(); score=0
            if str(ref).lower() in t or (digits and digits in t): score+=40
            if 'papel' in t or 'parede' in t: score+=8
            if 'home' in t or 'finish' in t: score+=4
            if 'ambiente' in t or 'banner' in t or 'logo' in t: score-=35
            if '150x150' in t or '100x100' in t or 'woocommerce_thumbnail' in t: score-=8
            out.append((score,wp_original(u),alt))
    out.sort(key=lambda x:x[0],reverse=True)
    seen=set(); clean=[]
    for x in out:
        if x[1] in seen: continue
        seen.add(x[1]); clean.append(x)
    return clean

def discover_hf_source(ref):
    s=requests.Session(); s.headers.update({'User-Agent':UA})
    search_url=f'https://homefinish.com.br/?s={quote(str(ref))}&post_type=product'
    try:
        r=s.get(search_url,timeout=TIMEOUT,allow_redirects=True)
        if r.ok:
            cands=image_candidates_from_html(r.text,ref)
            if cands and cands[0][0] >= 35: return cands[0][1]
            soup=BeautifulSoup(r.text,'html.parser')
            links=[]
            for a in soup.find_all('a',href=True):
                href=urljoin(search_url,a['href'])
                text=a.get_text(' ',strip=True)
                parent=a.parent.get_text(' ',strip=True) if a.parent else ''
                if str(ref) in (text+' '+parent) and 'homefinish.com.br' in href and '/papel-de-parede/' in href:
                    links.append(href)
            for href in links[:2]:
                try:
                    rp=s.get(href,timeout=TIMEOUT,allow_redirects=True)
                    if rp.ok:
                        cands=image_candidates_from_html(rp.text,ref)
                        if cands and cands[0][0] >= 25: return cands[0][1]
                except Exception:
                    pass
    except Exception:
        pass
    return None

def fetch_one(rec):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r'])
    base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    orig=base/'originals'/f'{ref}.jpg'; thumb=base/'thumbnails'/f'{ref}.jpg'
    if orig.exists() and thumb.exists():
        return 'ready',{**rec,'original':str(orig.relative_to(ROOT)),'thumbnail':str(thumb.relative_to(ROOT)),'status':'ready'}
    s=requests.Session(); s.headers.update({'User-Agent':UA})
    urls=[]
    if vendor=='Kantai':
        if rec.get('u'): urls=[rec.get('u')]
    else:
        discovered=discover_hf_source(ref)
        if discovered: urls.append(discovered)
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
    workers=int(os.environ.get('LIBRARY_WORKERS','10'))
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
