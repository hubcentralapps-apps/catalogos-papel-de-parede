from __future__ import annotations
import io, json, os, gzip, base64, re, html
from pathlib import Path
from urllib.parse import quote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA_DIR=ROOT/'dados'/'colecoes'
OUT=ROOT/'imagens'
TIMEOUT=30
UA='Mozilla/5.0 (compatible; FK-Catalog-Clean-Library/4.1)'
BASES=['https://homefinish.com.br/wp-content/uploads','https://www.homefinish.com.br/wp-content/uploads']


def normalize_collection(data):
    if data.get('records'): return data['records']
    vendor=data.get('fornecedor'); col=data.get('colecao'); slug=data.get('slug')
    return [{'f':vendor,'c':col,'s':slug,'r':item.get('r') or item.get('referencia'),'u':item.get('u')} for item in data.get('itens',[])]


def load_records():
    records=[]
    for p in sorted(DATA_DIR.glob('*.json')): records.extend(normalize_collection(json.loads(p.read_text(encoding='utf-8'))))
    for p in sorted(DATA_DIR.glob('*.json.gz.b64')):
        raw=gzip.decompress(base64.b64decode(p.read_text(encoding='ascii'))).decode('utf-8'); records.extend(normalize_collection(json.loads(raw)))
    seen=set(); out=[]
    for r in records:
        if r.get('f') not in {'Home Finish','Kantai'}: continue
        k=(r.get('f'),r.get('c'),str(r.get('r')))
        if k in seen: continue
        seen.add(k); out.append(r)
    return out


def find_download_jpg(page_html, base_url):
    txt=html.unescape(page_html).replace('\\/','/')
    for pat in [r'<a[^>]+href=["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^>]*>[^<]{0,240}BAIXAR\s+JPG',r'href=["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^>]*>[^<]{0,300}(?:BAIXAR|DOWNLOAD)\s+JPG',r'["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^<>]{0,600}BAIXAR\s+JPG']:
        m=re.search(pat,txt,re.I|re.S)
        if m:
            u=urljoin(base_url,m.group(1))
            if not any(x in u.lower() for x in ('watermark','marca-dagua','marca_dagua','com-marca')): return u
    return None


def discover_hf_official(session, ref, collection):
    ref_s=str(ref); lookup=re.sub(r'^(?:BH|MI)','',ref_s,flags=re.I)
    # Primeiro tenta a página exata do produto: é o caminho mais confiável para BAIXAR JPG.
    for page in [f'https://homefinish.com.br/papel-de-parede/{lookup}/',f'https://www.homefinish.com.br/papel-de-parede/{lookup}/']:
        try:
            pr=session.get(page,timeout=TIMEOUT)
            if pr.ok:
                jpg=find_download_jpg(pr.text,page)
                if jpg: return jpg
        except Exception: pass
    # Depois tenta a API de produtos.
    for term in dict.fromkeys([ref_s,lookup]):
        try:
            api=f'https://homefinish.com.br/wp-json/wc/store/v1/products?search={quote(term)}&per_page=30'
            rr=session.get(api,timeout=TIMEOUT); rr.raise_for_status(); products=rr.json() if isinstance(rr.json(),list) else []
            for p in products[:10]:
                vals=' '.join(str(p.get(k,'') or '') for k in ('sku','name','slug','permalink'))
                if not re.search(r'(^|[^0-9])'+re.escape(lookup)+r'([^0-9]|$)',vals,re.I): continue
                page=p.get('permalink')
                if not page: continue
                pr=session.get(page,timeout=TIMEOUT)
                if pr.ok:
                    jpg=find_download_jpg(pr.text,page)
                    if jpg: return jpg
        except Exception: pass
    return None


def hf_candidates(col,ref):
    r=re.sub(r'^(?:BH|MI)','',str(ref),flags=re.I); months=[]; stems=[]
    if col=='BIO Habitat': months=['2023/08','2024/09']; stems=[f'papel-parede-nacional-home-finish-bio-habitat-{r}',f'papel-parede-nacional-homefinish-bio-habitat-{r}']
    elif col=='Biomas': months=['2024/09','2023/08']; stems=[f'papel-parede-nacional-homefinish-biomas-{r}',f'papel-parede-nacional-home-finish-biomas-{r}']
    elif col=='Bosque da Imaginação': months=['2024/04','2024/09']; stems=[f'papel-parede-nacional-homefinish-bosque-da-imaginacao-{r}',f'papel-parede-nacional-home-finish-bosque-da-imaginacao-{r}']
    elif col=='Botânica': months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-homefinish-botanica-{r}',f'papel-parede-nacional-home-finish-botanica-{r}']
    elif col=='Doce Estilo': months=['2024/09','2025/01']; stems=[f'papel-parede-nacional-doce-estilo-home-finish-{r}',f'papel-parede-nacional-home-finish-doce-estilo-{r}']
    elif col=='Era Uma Vez': months=['2025/10','2025/09']; stems=[f'{r}-papel-parede-era-uma-vez-home-finish',f'papel-parede-era-uma-vez-home-finish-{r}']
    elif col=='Flora': months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-flora-{r}',f'papel-parede-nacional-homefinish-flora-{r}']
    elif col=='Natureza Lúdica': months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-natureza-ludica-{r}',f'papel-parede-nacional-homefinish-natureza-ludica-{r}']
    elif col=='Provence': months=['2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-provence-{r}',f'papel-parede-nacional-homefinish-provence-{r}']
    elif col=='Tartan': months=['2025/10','2025/09']; stems=[f'{r}-papel-parede-home-finish-studio-tartan',f'{r}-papel-parede-homefinish-studio-tartan']
    elif col=='Passeio no Campo': months=['2026/04','2026/03']; stems=[f'papel-parede-homefinish-{r}',f'papel-parede-home-finish-{r}']
    out=[]
    for b in BASES:
        for m in months:
            for st in stems:
                for ex in ('.jpg','-1.jpg','_1.jpg'):
                    u=f'{b}/{m}/{st}{ex}'
                    if u not in out: out.append(u)
    return out


def normalize_wix_url(url):
    if not url: return url
    m=re.match(r'^(https://static\.wixstatic\.com/media/[^?]+?~mv2\.(?:jpg|jpeg|png))(?:/v1/.*)?$',url,re.I)
    return m.group(1) if m else url


def fetch_image(session,url):
    r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
    if r.status_code!=200 or not r.content or 'image' not in r.headers.get('content-type','').lower(): raise ValueError('not-image')
    im=Image.open(io.BytesIO(r.content)); im.load(); return im.convert('RGB')


def save_pair(im,base,ref):
    od=base/'originals'; td=base/'thumbnails'; od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'; im.save(op,'JPEG',quality=94,optimize=True,progressive=True)
    ImageOps.fit(im,(520,520),method=Image.Resampling.LANCZOS,centering=(0.5,0.5)).save(tp,'JPEG',quality=86,optimize=True,progressive=True)
    return op,tp


def fetch_one(rec):
    vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r']); base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
    session=requests.Session(); session.headers.update({'User-Agent':UA})
    try:
        if vendor=='Home Finish':
            urls=[]; official=discover_hf_official(session,ref,col)
            if official: urls.append(official)
            urls.extend([u for u in hf_candidates(col,ref) if u not in urls])
            im=source=None
            for u in urls:
                try:
                    test=fetch_image(session,u); w,h=test.size
                    if min(w,h)>=500 and max(w,h)>=700: im,source=test,u; break
                except Exception: pass
            if im is None: raise ValueError('clean-hf-jpg-not-found')
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
    print(f'CLEAN READY={len(ready)} FAILED={len(failed)}')

if __name__=='__main__': main()
