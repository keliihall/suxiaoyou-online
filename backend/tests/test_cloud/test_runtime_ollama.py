"""Cloud-only Ollama ownership, extraction and lifecycle contracts."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import tarfile

import httpx
import pytest

from app.cloud import runtime_ollama
from app.cloud.runtime_ollama import PrivateOllamaManager, extract_private_archive


@pytest.mark.parametrize("saved_value,expected", [(None, True), ("false", False), ("true", True)])
def test_cloud_runtime_preserves_ollama_startup_settings(tmp_path, saved_value, expected):
    """The actual cloud app must preserve default and persisted startup policy."""
    pytest.importorskip("mcp", reason="Requires original engine runtime dependencies")
    import os
    import subprocess
    import sys
    tenant = tmp_path / "tenant"
    state = tenant / "state"
    state.mkdir(parents=True)
    if saved_value is not None:
        (state / ".env").write_text("SUXIAOYOU_OLLAMA_AUTO_START=" + saved_value + "\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SUXIAOYOU_", "CLOUD_"))}
    env.update({"PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "CLOUD_ENGINE_TENANT_ROOT": str(tenant),
        "CLOUD_ENGINE_AUTH_TOKEN": "suxiaoyou_st_" + "a" * 64,
        "CLOUD_ENGINE_CAPABILITY": "c" * 64,
        "CLOUD_ENGINE_GATEWAY_URL": "http://central.test/api/cloud/internal/inference/v1"})
    result = subprocess.run([sys.executable, "-c", "from app.cloud.runtime_entry import create_app; "
        "app=create_app(); print(app.state.settings.ollama_auto_start)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == str(expected)


def package(path, members):
    with tarfile.open(path,'w:gz') as archive:
        for name,kind,value in members:
            info=tarfile.TarInfo(name)
            if kind=='file':
                content=value.encode();info.size=len(content);info.mode=0o755
                archive.addfile(info,BytesIO(content))
            else:
                info.type=tarfile.SYMTYPE;info.linkname=value
                archive.addfile(info)


def test_archive_rejects_paths_links_and_devices_before_any_extraction(tmp_path):
    for index,(name,kind,value) in enumerate([
        ('../outside','file','bad'),('/absolute','file','bad'),
        ('bin/link','link','../../outside'),('bin/link','link','/outside')]):
        archive=tmp_path/f'bad{index}.tgz';destination=tmp_path/f'out{index}';destination.mkdir()
        package(archive,[('bin/ollama','file','good'),(name,kind,value)])
        with pytest.raises(RuntimeError):extract_private_archive(archive,destination)
        assert not (destination/'bin/ollama').exists()
    archive=tmp_path/'good.tgz';destination=tmp_path/'good';destination.mkdir()
    package(archive,[('bin/ollama','file','good'),('lib/ollama/cpu.so','file','safe'),
                     ('lib/ollama/cpu-link.so','link','cpu.so')])
    extract_private_archive(archive,destination)
    assert (destination/'lib/ollama/cpu-link.so').read_text()=='safe'
    assert not (tmp_path/'outside').exists()


def test_download_origins_are_official_https_only():
    assert runtime_ollama.allowed_download_url('https://github.com/ollama/ollama/releases/latest/download/file')
    assert runtime_ollama.allowed_download_url('https://release-assets.githubusercontent.com/object?signature=public-asset')
    for url in ['http://github.com/file','https://evil.example/file','https://github.com.evil.test/x',
                'https://user:secret@github.com/x','https://github.com:444/x']:
        assert not runtime_ollama.allowed_download_url(url)


async def test_install_is_atomic_private_and_checks_redirect_origin(tmp_path,monkeypatch):
    monkeypatch.setenv('CLOUD_OLLAMA_ARCHIVE','tgz')
    monkeypatch.setattr(runtime_ollama,'_platform_key',lambda:'linux-arm64')
    archive=tmp_path/'fixture.tgz';package(archive,[('bin/ollama','file','binary'),('lib/ollama/cpu.so','file','lib')])
    calls=[]
    def reply(request):
        calls.append(request.url)
        return httpx.Response(200,content=archive.read_bytes())
    real=httpx.AsyncClient
    class Client(real):
        def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs,transport=httpx.MockTransport(reply))
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    manager=PrivateOllamaManager(tmp_path/'state')
    progress=[item async for item in manager.download_binary()]
    assert progress[-1]['status']=='done' and manager.is_binary_installed
    assert manager.binary_path==tmp_path/'state/ollama/bin/ollama'
    assert json.loads((manager.binary_dir/'release-provenance.json').read_text())['verified']=='official HTTPS origins'
    assert not list(manager.data_dir.glob('.ollama-install-*'))
    assert [item async for item in manager.download_binary()]==[{'status':'done'}]
    assert len(calls)==1
    def redirect(request):return httpx.Response(302,headers={'location':'https://evil.example/binary'})
    monkeypatch.setattr(Client,'__init__',lambda self,*a,**kw:real.__init__(self,*a,**kw,transport=httpx.MockTransport(redirect)))
    other=PrivateOllamaManager(tmp_path/'other')
    progress=[item async for item in other.download_binary()]
    assert progress[-1]['status']=='error' and not other.is_binary_installed


async def test_server_env_and_remove_are_owned_not_host_global(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    monkeypatch.setenv('CLOUD_ENGINE_CAPABILITY','not-for-ollama')
    monkeypatch.setenv('DEEPSEEK_API_KEY','not-for-ollama')
    manager=PrivateOllamaManager(tmp_path/'state')
    env=manager.environment()
    assert 'CLOUD_ENGINE_CAPABILITY' not in env and 'DEEPSEEK_API_KEY' not in env
    assert env['OLLAMA_MODELS']==str(tmp_path/'state/ollama-models') and env['OLLAMA_NO_CLOUD']=='1'
    assert env['OLLAMA_NUM_PARALLEL']=='1'
    foreign=tmp_path/'foreign';foreign.mkdir();(foreign/'keep').write_text('keep')
    manager.data_dir.mkdir();manager.binary_dir.symlink_to(foreign,target_is_directory=True)
    with pytest.raises(RuntimeError):await manager.uninstall(True)
    assert (foreign/'keep').read_text()=='keep'
    manager.binary_dir.unlink();manager.binary_path.parent.mkdir(parents=True)
    manager.binary_path.write_text('owned');manager.binary_path.chmod(0o755)
    manager.models_dir.mkdir();(manager.models_dir/'weights').write_text('model')
    result=await manager.uninstall(False)
    assert result['binary'] and not result['models'] and (manager.models_dir/'weights').exists()
    result=await manager.uninstall(True)
    assert result['models'] and not manager.models_dir.exists()


async def test_stop_uses_only_process_owned_by_manager(tmp_path):
    manager=PrivateOllamaManager(tmp_path)
    class Process:
        returncode=None
        stopped=False
        def terminate(self):self.stopped=True;self.returncode=0
        async def wait(self):return self.returncode
    process=Process();manager._process=process
    await manager.stop()
    assert process.stopped and manager._process is None
    # An already-stopped manager does not scan a port/PID or signal strangers.
    await manager.stop()


def test_extraction_never_removes_existing_data_and_enforces_expanded_budget(tmp_path,monkeypatch):
    archive=tmp_path/'ok.tgz';package(archive,[('bin/ollama','file','binary')])
    destination=tmp_path/'existing';destination.mkdir();(destination/'precious').write_text('keep')
    with pytest.raises(RuntimeError):extract_private_archive(archive,destination)
    assert (destination/'precious').read_text()=='keep'
    fresh=tmp_path/'fresh';fresh.mkdir();monkeypatch.setattr(runtime_ollama,'MAX_EXPANDED_BYTES',1)
    with pytest.raises(RuntimeError):extract_private_archive(archive,fresh)
    assert not list(fresh.iterdir())


def test_zstd_stream_uses_no_cloud_env_or_second_expanded_tar(tmp_path,monkeypatch):
    import gzip
    source=tmp_path/'source.tgz';package(source,[('bin/ollama','file','binary')])
    archive=tmp_path/'release.tar.zst';archive.write_bytes(b'protocol-fixture')
    class Process:
        stdout=BytesIO(gzip.decompress(source.read_bytes()))
        def wait(self,timeout=None):return 0
        def poll(self):return 0
    def spawn(args,**kwargs):
        assert kwargs['env']=={'PATH':'/usr/bin:/bin'}
        assert args[:3]==['/usr/bin/zstd','-d','-c']
        return Process()
    monkeypatch.setattr(runtime_ollama.shutil,'which',lambda _:'/usr/bin/zstd')
    monkeypatch.setattr(runtime_ollama.subprocess,'Popen',spawn)
    destination=tmp_path/'fresh';destination.mkdir()
    extract_private_archive(archive,destination)
    assert (destination/'bin/ollama').read_text()=='binary'
    assert not list(tmp_path.glob('*.expanded.tar'))
