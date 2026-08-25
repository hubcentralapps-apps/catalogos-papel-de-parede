from __future__ import annotations
import concurrent.futures as cf
import html, io, json, re, unicodedata
from pathlib import Path
from urllib.parse import quote, urljoin
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'dados'/'colecoes'/'home-finish-catalogo-completo.json'
OUT=ROOT/'imagens'/'home-finish'
TIMEOUT=30
UA='Mozilla/5.0 (compatible; FK-Catalog-Image-Library/1.0)'

def slug(s):
    s=unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+','-',s).strip('-')

def exact_ref_product(products, ref, collection):
    ref_s=str(ref)
    col=slug(collection).replace('-',' ')
    ranked=[]
    for p in products if isinstance(products,list) else []:
        vals=' '.join(str(p.get(k,'') or '') for k in ('sku','name','slug','permalink'))
        if not re.search(r'(^|[^0-9])'+re.escape(ref_s)+r'([^0-9]|$)', vals):
            continue
        cats=' '.join(str(c.get('name','')) for c in p.get('categories',[]) if isinstance(c,dict))
        blob=unicodedata.normalize('NFKD',(vals+' '+cats)).encode('ascii','ignore').decode().lower()
        score=(20 if str(p.get('sku',''))==ref_s else 0)+(5 if col and col in blob else 0)
        ranked.append((score,p))
    ranked.sort(key=lambda x:x[0],reverse=True)
    return ranked[0][1] if ranked else None

def find_download_jpg(page_html, base_url):
    txt=html.unescape(page_html).replace('\\/','/')
    patterns=[
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>[^<]{0,80}BAIXAR\s+JPG',
        r'href=["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^>]*>[^<]{0,120}BAIXAR\s+JPG',
        r'["\']([^"\']+\.jpe?g(?:\?[^"\']*)?)["\'][^<>]{0,300}BAIXAR\s+JPG',
    ]
    for pat in patterns:
        m=re.search(pat,txt,re.I|re.S)
        if m:
            return urljoin(base_url,m.group(1))
    pos=re.search(r'BAIXAR\s+JPG',txt,re.I)
    if pos:
        chunk=txt[max(0,pos.start()-1200):pos.end()+1200]
        urls=re.findall(r'https?://[^"\'<>\s]+\.jpe?g(?:\?[^"\'<>\s]*)?',chunk,re.I)
        if urls: return urls[-1]
    return None

def fetch_one(item):
    ref=str(item['referencia']); collection=item['colecao']; s=requests.Session()
    s.headers.update({'User-Agent':UA})
    api=f'https://www.homefinish.com.br/wp-json/wc/store/v1/products?search={quote(ref)}&per_page=20'
    r=s.get(api,timeout=TIMEOUT); r.raise_for_status()
    product=exact_ref_product(r.json(),ref,collection)
    if not product: raise RuntimeError('produto_exato_nao_encontrado')
    page=product.get('permalink')
    if not page: raise RuntimeError('sem_permalink')
    pr=s.get(page,timeout=TIMEOUT); pr.raise_for_status()
    jpg=find_download_jpg(pr.text,page)
    if not jpg: raise RuntimeError('download_jpg_nao_encontrado')
    low=jpg.lower()
    if any(x in low for x in ('watermark','marca-dagua','marca_dagua','com-marca')):
        raise RuntimeError('url_com_marca')
    ir=s.get(jpg,timeout=TIMEOUT,allow_redirects=True); ir.raise_for_status()
    if 'image' not in ir.headers.get('content-type','').lower():
        raise RuntimeError('download_nao_imagem')
    im=Image.open(io.BytesIO(ir.content)); im.load(); im=im.convert('RGB')
    w,h=im.size
    if min(w,h)<600 or max(w,h)<800:
        raise RuntimeError(f'baixa_resolucao_{w}x{h}')
    cslug=slug(collection)
    od=OUT/cslug/'originals'; td=OUT/cslug/'thumbnails'
    od.mkdir(parents=True,exist_ok=True); td.mkdir(parents=True,exist_ok=True)
    op=od/f'{ref}.jpg'; tp=td/f'{ref}.jpg'
    im.save(op,'JPEG',quality=92,optimize=True,progressive=True)
    ImageOps.fit(im,(480,480),method=Image.Resampling.LANCZOS).save(tp,'JPEG',quality=84,optimize=True,progressive=True)
    return {**item,'f':'Home Finish','c':collection,'s':cslug,'r':ref,'source_resolved':jpg,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':w,'height':h,'status':'ready'}

def main():
    data=json.loads(DATA.read_text(encoding='utf-8'))
    items=data['itens']
    ready=[]; failed=[]; pending=[]
    for it in items:
        ref=str(it['referencia']); cslug=slug(it['colecao'])
        op=OUT/cslug/'originals'/f'{ref}.jpg'; tp=OUT/cslug/'thumbnails'/f'{ref}.jpg'
        if op.exists() and tp.exists():
            try:
                with Image.open(op) as im: w,h=im.size
                ready.append({**it,'f':'Home Finish','c':it['colecao'],'s':cslug,'r':ref,'original':str(op.relative_to(ROOT)),'thumbnail':str(tp.relative_to(ROOT)),'width':w,'height':h,'status':'ready'})
            except Exception:
                pending.append(it)
        else:
            pending.append(it)
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(fetch_one,it):it for it in pending}
        for fut in cf.as_completed(futs):
            it=futs[fut]
            try:
                rec=fut.result(); ready.append(rec); print('HF OK',rec['r'],rec['c'],flush=True)
            except Exception as e:
                failed.append({**it,'f':'Home Finish','c':it['colecao'],'r':str(it['referencia']),'status':'not_found','reason':str(e)})
                print('HF FAIL',it['referencia'],it['colecao'],str(e),flush=True)
    mp=ROOT/'dados'/'biblioteca-imagens.json'
    manifest=json.loads(mp.read_text(encoding='utf-8')) if mp.exists() else {'items':[],'failures':[]}
    manifest['items']=[x for x in manifest.get('items',[]) if x.get('f')!='Home Finish']+sorted(ready,key=lambda x:(x['c'],x['r']))
    manifest['failures']=[x for x in manifest.get('failures',[]) if x.get('f')!='Home Finish']+failed
    manifest['ready']=len(manifest['items']); manifest['failed']=len(manifest['failures'])
    mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'HOME FINISH READY={len(ready)} FAILED={len(failed)} TOTAL={len(items)}')

if __name__=='__main__':
    main()
