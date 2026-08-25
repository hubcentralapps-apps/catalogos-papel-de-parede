from __future__ import annotations
import io, json, os, sys, time
from pathlib import Path
import requests
from PIL import Image, ImageOps

ROOT=Path(__file__).resolve().parents[1] if '__file__' in globals() else Path('.')
DATA=ROOT/'dados'/'colecoes-processadas.json'
OUT=ROOT/'imagens'
TIMEOUT=20
UA='Mozilla/5.0 (compatible; FK-Catalog-Image-Library/1.0)'

BASES=['https://www.homefinish.com.br/wp-content/uploads','https://homefinish.com.br/wp-content/uploads']
EXTS=['.jpg','-1.jpg','_1.jpg','_2.jpg','-sem-marca.jpg','-sem-marca-1.jpg','.png']

def hf_candidates(col, ref):
    out=[]
    def add(u):
        if u and u not in out: out.append(u)
    if col=='BIO Habitat':
        months=['2023/08','2024/09','2024/04']; stems=[f'papel-parede-nacional-home-finish-bio-habitat-{ref}',f'papel-parede-nacional-homefinish-bio-habitat-{ref}']
    elif col=='Biomas':
        months=['2024/09','2023/08','2024/04']; stems=[f'papel-parede-nacional-homefinish-biomas-{ref}',f'papel-parede-nacional-home-finish-biomas-{ref}']
    elif col=='Bosque da Imaginação':
        months=['2024/04','2024/09','2023/08']; stems=[f'papel-parede-nacional-homefinish-bosque-da-imaginacao-{ref}',f'papel-parede-nacional-home-finish-bosque-da-imaginacao-{ref}']
    elif col=='Botânica':
        months=['2024/09','2024/04','2023/08']; stems=[f'papel-parede-nacional-homefinish-botanica-{ref}',f'papel-parede-nacional-home-finish-botanica-{ref}']
    elif col=='Doce Estilo':
        months=['2024/09','2025/01','2025/04','2024/04','2023/08']; stems=[f'papel-parede-nacional-doce-estilo-home-finish-{ref}',f'papel-parede-nacional-home-finish-doce-estilo-{ref}',f'papel-parede-nacional-homefinish-doce-estilo-{ref}']
    elif col=='Era Uma Vez':
        months=['2025/10','2025/09','2025/08','2025/04','2024/09']; stems=[f'{ref}-papel-parede-era-uma-vez-home-finish',f'papel-parede-era-uma-vez-home-finish-{ref}',f'papel-parede-nacional-home-finish-era-uma-vez-{ref}']
    elif col=='Flora':
        months=['2024/09','2024/04','2023/08','2025/01','2025/04']; stems=[f'papel-parede-nacional-home-finish-flora-{ref}',f'papel-parede-nacional-homefinish-flora-{ref}',f'papel-parede-flora-home-finish-{ref}',f'{ref}-papel-parede-flora-home-finish']
    elif col=='Natureza Lúdica':
        months=['2024/09','2024/04','2023/08','2025/01','2025/04']; stems=[f'papel-parede-nacional-home-finish-natureza-ludica-{ref}',f'papel-parede-nacional-homefinish-natureza-ludica-{ref}',f'papel-parede-natureza-ludica-home-finish-{ref}',f'{ref}-papel-parede-natureza-ludica-home-finish']
    elif col=='Provence':
        months=['2024/09','2024/04','2023/08','2025/01','2025/04']; stems=[f'papel-parede-nacional-home-finish-provence-{ref}',f'papel-parede-nacional-homefinish-provence-{ref}',f'papel-parede-provence-home-finish-{ref}',f'{ref}-papel-parede-provence-home-finish']
    elif col=='Tartan':
        months=['2025/10','2025/09','2025/08','2025/04']; stems=[f'{ref}-papel-parede-home-finish-studio-tartan',f'{ref}-papel-parede-homefinish-studio-tartan',f'papel-parede-home-finish-studio-tartan-{ref}',f'papel-parede-homefinish-tartan-{ref}']
        suff=['.jpg','-1.jpg','_1.jpg','_2.jpg','-11_2.jpg','-11_1.jpg']
        for b in BASES:
            for m in months:
                for st in stems:
                    for s in suff: add(f'{b}/{m}/{st}{s}')
        return out
    elif col=='Passeio no Campo':
        months=['2026/04','2026/03','2026/02','2025/10','2025/09']; stems=[f'papel-parede-homefinish-{ref}',f'papel-parede-home-finish-{ref}',f'{ref}-papel-parede-homefinish',f'{ref}-papel-parede-home-finish']
    else:
        return out
    for b in BASES:
        for m in months:
            for st in stems:
                for ex in EXTS: add(f'{b}/{m}/{st}{ex}')
    return out

def fetch_image(session, urls):
    for url in urls:
        try:
            r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
            if r.status_code!=200 or not r.content: continue
            if 'image' not in r.headers.get('content-type','').lower(): continue
            im=Image.open(io.BytesIO(r.content)); im.load()
            w,h=im.size
            short,long=min(w,h),max(w,h)
            if short < 700 or long < 900: continue
            return im.convert('RGB'),url
        except Exception:
            continue
    return None,None

def save_original_and_thumb(im, base, ref):
    orig_dir=base/'originals'; thumb_dir=base/'thumbnails'
    orig_dir.mkdir(parents=True,exist_ok=True); thumb_dir.mkdir(parents=True,exist_ok=True)
    orig=orig_dir/f'{ref}.jpg'; thumb=thumb_dir/f'{ref}.jpg'
    im.save(orig,'JPEG',quality=92,optimize=True,progressive=True)
    t=ImageOps.fit(im,(480,480),method=Image.Resampling.LANCZOS,centering=(0.5,0.5))
    t.save(thumb,'JPEG',quality=82,optimize=True,progressive=True)
    return orig,thumb

def main():
    data=json.loads(DATA.read_text(encoding='utf-8'))
    session=requests.Session(); session.headers.update({'User-Agent':UA})
    manifest=[]; failures=[]
    for n,rec in enumerate(data['records'],1):
        vendor=rec['f']; col=rec['c']; slug=rec['s']; ref=str(rec['r'])
        base=OUT/('home-finish' if vendor=='Home Finish' else 'kantai')/slug
        orig=base/'originals'/f'{ref}.jpg'; thumb=base/'thumbnails'/f'{ref}.jpg'
        if orig.exists() and thumb.exists():
            manifest.append({**rec,'original':str(orig.relative_to(ROOT)),'thumbnail':str(thumb.relative_to(ROOT)),'status':'ready'})
            continue
        urls=[rec.get('u')] if vendor=='Kantai' else hf_candidates(col,ref)
        urls=[u for u in urls if u]
        im,url=fetch_image(session,urls)
        if im is None:
            failures.append({**rec,'status':'not_found'})
            print(f'[{n}/{len(data["records"])}] FAIL {vendor} {col} {ref}',flush=True)
            continue
        if vendor=='Kantai' and rec.get('crop')=='top-half':
            w,h=im.size
            im=im.crop((0,0,w,h//2))
        orig,thumb=save_original_and_thumb(im,base,ref)
        manifest.append({**rec,'source_resolved':url,'original':str(orig.relative_to(ROOT)),'thumbnail':str(thumb.relative_to(ROOT)),'width':im.width,'height':im.height,'status':'ready'})
        print(f'[{n}/{len(data["records"])}] OK {vendor} {col} {ref} {im.width}x{im.height}',flush=True)
        time.sleep(0.03)
    (ROOT/'dados').mkdir(exist_ok=True)
    (ROOT/'dados'/'biblioteca-imagens.json').write_text(json.dumps({'ready':len(manifest),'failed':len(failures),'items':manifest,'failures':failures},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'READY={len(manifest)} FAILED={len(failures)}')

if __name__=='__main__': main()
