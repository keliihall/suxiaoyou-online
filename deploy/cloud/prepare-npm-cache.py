#!/usr/bin/env python3
"""Preserve public npm downloads from this deployment's own active builder."""
import base64,gzip,hashlib,json,os,re,shutil,stat,subprocess,tarfile
from pathlib import Path
from urllib.parse import urlsplit
os.umask(0o077)
ROOT=Path('/srv/suxiaoyou');CONFIG=ROOT/'shared/config/20261115-2144'
CONTEXT=CONFIG/'npm-cache-base-v5';ARCHIVE=CONFIG/'npm-cache-verified-v5.tar.gz'
RECEIPT=ROOT/'incoming/npm-cache-base.manifest.json'
BASE='suxiaoyou-base-node:20261115-amd64';TAG='suxiaoyou-npm-cache:20261115-2144'
RUNNER=['runuser','-u','suxiaoyou','--','env','HOME=/home/suxiaoyou','XDG_RUNTIME_DIR=/run/user/1001','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus','DOCKER_HOST=unix:///run/user/1001/docker.sock']
def public_file(path,data,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as f:
  os.fchown(f.fileno(),1001,1001);os.fchmod(f.fileno(),mode);f.write(data)
def mkdir(path,mode):
 path.mkdir(mode=mode);os.chown(path,1001,1001);os.chmod(path,mode)
def image(tag):
 p=subprocess.run(RUNNER+['docker','image','inspect','--format','{{json .}}',tag],capture_output=True,check=False,timeout=30)
 assert p.returncode==0
 return json.loads(p.stdout)
def index_public(raw):
 accepted=[]
 for line in raw.splitlines():
  if not line:continue
  checksum,entry=line.split(b'\t',1)
  assert hashlib.sha1(entry).hexdigest().encode()==checksum
  value=json.loads(entry);key=value.get('key')
  if key is None:continue
  assert isinstance(key,str)
  if 'https://registry.npmjs.org/' not in key:continue
  url=key[key.index('https://'):];p=urlsplit(url)
  assert p.scheme=='https' and p.hostname=='registry.npmjs.org' and not p.username and not p.password and not p.query and not p.fragment
  def check(v):
   if isinstance(v,dict):
    assert not any(k.lower() in {'authorization','proxy-authorization','password','api_key','access_token','set-cookie'} for k in v)
    for x in v.values():check(x)
   elif isinstance(v,list):
    for x in v:check(x)
  check(value);accepted.append(line+b'\n')
 return b''.join(accepted)
def main():
 assert os.getuid()==0 and os.geteuid()==0
 p=Path('/proc/48243');assert (p/'cmdline').read_bytes().startswith(b'npm ci')
 source=p/'root/root/.npm/_cacache';assert source.is_dir() and not source.is_symlink()
 assert not CONTEXT.exists() and not ARCHIVE.exists() and not RECEIPT.exists()
 lock=json.loads((ROOT/'releases/20261115-2144/frontend/package-lock.json').read_bytes())
 assert not (p/'root/app/.npmrc').exists() and not (p/'root/root/.npmrc').exists()
 assert (p/'root/app/package-lock.json').read_bytes()==(ROOT/'releases/20261115-2144/frontend/package-lock.json').read_bytes()
 for package in lock['packages'].values():
  if 'resolved' not in package:continue
  u=urlsplit(package['resolved']);assert u.scheme=='https' and (u.hostname=='registry.npmjs.org' or (package['resolved']=='https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz' and package.get('integrity')=='sha512-oLDq3jw7AcLqKWH2AhCpVTZl8mf6X2YReP+Neh0SJUzV/BdZYjth94tG5toiMB1PPrYtxOCfaoUCkvtuH+3AJA==')) and not u.username and not u.password
 base=image(BASE);assert base['Architecture']=='amd64' and base['Os']=='linux'
 recipe=json.loads((CONFIG/'build-recipe.json').read_bytes())
 notes=[x for x in recipe['offline_resume']['verified_local_images'] if x['source']=='node:22-bookworm-slim']
 assert len(notes)==1 and base['Id']==notes[0]['inspect_image_id'] and base['RootFS']['Layers']==notes[0]['all_rootfs_diff_ids']
 absent=subprocess.run(RUNNER+['docker','image','inspect',TAG],capture_output=True,check=False,timeout=30);assert absent.returncode==1
 mkdir(CONTEXT,0o700);cache=CONTEXT/'cache';mkdir(cache,0o755)
 count=0;total=0;inventory=[]
 for group in ['content-v2','index-v5']:
  directory=source/group;assert directory.is_dir() and not directory.is_symlink()
  for current,dirs,files in os.walk(directory,followlinks=False):
   assert all(not (Path(current)/x).is_symlink() for x in dirs)
   relative=Path(current).relative_to(source);destination=cache/relative
   if not destination.exists():mkdir(destination,0o755)
   for name in files:
    origin=Path(current)/name;rel=origin.relative_to(source)
    assert all(re.fullmatch(r'[a-z0-9-]+',part) for part in rel.parts)
    fd=os.open(origin,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
     info=os.fstat(f.fileno());assert stat.S_ISREG(info.st_mode) and info.st_size<=256*1024*1024
     raw=f.read(256*1024*1024+1);assert len(raw)==info.st_size
    if group=='content-v2':
     assert rel.parts[1]=='sha512' and len(rel.parts)==5 and ''.join(rel.parts[2:])==hashlib.sha512(raw).hexdigest()
    else:
     raw=index_public(raw)
     if not raw:continue
    total+=len(raw);count+=1;assert total<=1024*1024*1024 and count<=30000
    public_file(cache/rel,raw,0o644)
    inventory.append({'path':str(rel),'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
 extra=Path('/home/poi/.sxy-deploy-private/sxy-sheetjs-0.20.3.tgz')
 fd=os.open(extra,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  assert stat.S_ISREG(os.fstat(f.fileno()).st_mode);raw=f.read(5*1024*1024+1)
 assert len(raw)==2409319 and hashlib.sha256(raw).hexdigest()=='8dc73fc3b00203e72d176e85b50938627c7b086e607c682e8d3c22c02bb99fe8'
 digest=hashlib.sha512(raw).digest()
 assert 'sha512-'+base64.b64encode(digest).decode()==lock['packages']['node_modules/xlsx']['integrity']
 value=digest.hex();rel=Path('content-v2/sha512')/value[:2]/value[2:4]/value[4:]
 target=cache/rel
 if target.exists():assert target.read_bytes()==raw
 else:
  for directory in list(reversed(target.parent.parents)) + [target.parent]:
   if directory==cache or cache in directory.parents:
    if not directory.exists():mkdir(directory,0o755)
  public_file(target,raw,0o644);count+=1;total+=len(raw)
  inventory.append({'path':str(rel),'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
 assert count>100 and total>1024*1024
 # Archive only verified public cache bytes, outside the build context.
 fd=os.open(ARCHIVE,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:
  os.fchown(out.fileno(),1001,1001)
  with gzip.GzipFile(fileobj=out,mode='wb',compresslevel=1,mtime=0) as zipped:
   with tarfile.open(fileobj=zipped,mode='w|') as tar:
    for item in sorted(inventory,key=lambda x:x['path']):
     path=cache/item['path'];meta=tarfile.TarInfo('cache/'+item['path']);meta.size=item['size'];meta.mode=0o644;meta.mtime=0
     with path.open('rb') as f:tar.addfile(meta,f)
 with ARCHIVE.open('rb') as f:archive_hash=hashlib.file_digest(f,'sha256').hexdigest()
 public_file(CONTEXT/'Dockerfile',('FROM '+BASE+'\nCOPY --chown=0:0 cache/ /root/.npm/_cacache/\n').encode(),0o644)
 public_file(CONTEXT/'.dockerignore',b'*\n!Dockerfile\n!cache\n!cache/**\n',0o644)
 print(json.dumps({'phase':'public_npm_cache_verified','files':count,'bytes':total,'archive_sha256':archive_hash}),flush=True)
 r=subprocess.run(RUNNER+['docker','build','--pull=false','--tag',TAG,'--file',str(CONTEXT/'Dockerfile'),str(CONTEXT)],stdout=None,stderr=subprocess.STDOUT,check=False,timeout=600)
 assert r.returncode==0
 result=image(TAG);assert result['Architecture']=='amd64' and result['Os']=='linux'
 layers=base['RootFS']['Layers'];assert result['RootFS']['Layers'][:len(layers)]==layers and len(result['RootFS']['Layers'])==len(layers)+1
 for key in ['User','Env','Entrypoint','Cmd','WorkingDir','StopSignal']:assert result['Config'].get(key)==base['Config'].get(key)
 receipt={'format_version':1,'platform':'linux/amd64','image_tag':TAG,'image_id':result['Id'],'node_base_tag':BASE,'node_base_image_id':base['Id'],'cache_archive_sha256':archive_hash,'cache_bytes':total,'cache_file_count':count,'public_registry_only':True,'content_integrity_verified':True}
 raw=(json.dumps(receipt,sort_keys=True,indent=2)+'\n').encode();public_file(RECEIPT,raw)
 print(json.dumps({'phase':'public_npm_cache_image_ready','manifest_sha256':hashlib.sha256(raw).hexdigest(),'image_id':result['Id']}),flush=True)
try:main()
except BaseException:
 print(json.dumps({'phase':'cache_preparation_stopped_safely'}),flush=True);raise SystemExit(1) from None
