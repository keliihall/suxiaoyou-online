"""Mac node ownership/TLS/control-plane contracts without model downloads."""
from datetime import timedelta
import hashlib
import socket

import httpx
import pytest
from sqlalchemy import update

from app.cloud import mlx_nodes
from app.cloud.mlx_node_server import ExclusiveNode, NodeSettings, StartBody, create_node_app, initialize_node
from app.cloud.mlx_node_server import parse_model_repos, approved_model_catalog
from app.cloud.models import AuthSession, MlxNodeConfig, User, utcnow
from app.cloud.secrets import ModelSecretVault
from tests.test_cloud.test_inference_gateway import gateway, records

TOKEN = "test-node-credential-" + "x" * 40


def credentials(tmp_path, name="node"):
    root = tmp_path / name
    initialize_node(root, "localhost")
    return root, (root / "tls.crt").read_text()


async def prepare(gateway, tmp_path):
    app, database, client, _ = gateway
    app.include_router(mlx_nodes.router)
    _, pem = credentials(tmp_path)
    async with database.sessions() as db:
        await db.execute(update(User).where(User.id == "owner").values(role="admin"))
        db.add(User(id="second", email="second@test.local", display_name="Second", password_hash="unused"))
        db.add(AuthSession(id=hashlib.sha256(b"owner-cookie").hexdigest(), user_id="owner", csrf_token="csrf",
            expires_at=utcnow() + timedelta(hours=1)))
        await db.commit()
    client.cookies.set("cloud_session", "owner-cookie")
    client.headers["X-CSRF-Token"] = "csrf"
    body = {"url": "https://localhost:18443", "token": TOKEN, "certificate_pem": pem, "enabled": True}
    return database, client, body


async def test_node_config_is_encrypted_admin_only_and_exclusive(gateway, tmp_path):
    database, client, body = await prepare(gateway, tmp_path)
    endpoint = "/api/cloud/admin/users/owner/mlx-node"
    response = await client.put(endpoint, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["configured"] and data["token_configured"] and len(data["certificate_fingerprint"]) == 64
    assert TOKEN not in response.text and "BEGIN CERTIFICATE" not in response.text
    async with database.sessions() as db:
        row = await db.get(MlxNodeConfig, "owner")
        assert TOKEN not in row.token_encrypted
        assert ModelSecretVault(gateway[0].state.settings).unseal(row.token_encrypted) == TOKEN
    response = await client.put(endpoint, json={"url": body["url"], "enabled": False})
    assert response.status_code == 200 and not response.json()["enabled"]
    assert (await client.put("/api/cloud/admin/users/second/mlx-node", json=body)).status_code == 409
    client.headers.pop("X-CSRF-Token")
    assert (await client.put(endpoint, json=body)).status_code == 403
    async with database.sessions() as db:
        await db.execute(update(User).where(User.id == "owner").values(role="user"))
        await db.commit()
    assert (await client.get(endpoint)).status_code == 403


async def test_unconfigured_node_reports_unavailable_no_fake_local_mlx(gateway):
    app, _, client, _ = gateway
    app.include_router(mlx_nodes.router)
    response = await client.get("/api/cloud/internal/mlx-node/rapid-mlx/status")
    assert response.status_code == 200
    assert not response.json()["configured"] and not response.json()["running"]
    assert (await client.post("/api/cloud/internal/mlx-node/rapid-mlx/start", json={})).status_code == 503
    assert (await client.get("/api/cloud/internal/mlx-node/v1/models")).json()["data"] == []
    client.headers.pop("Authorization")
    assert (await client.get("/api/cloud/internal/mlx-node/rapid-mlx/status")).status_code == 401


def test_certificate_is_leaf_pinned_and_url_is_fixed_https(tmp_path):
    _, pem = credentials(tmp_path)
    context, fingerprint = mlx_nodes.certificate_context(pem)
    assert len(fingerprint) == 64 and context.check_hostname
    assert context.cert_store_stats()["x509"] == 1
    for url in ["http://localhost:18443", "https://user:secret@localhost", "https://localhost/v1",
                "https://169.254.169.254:18443", "https://localhost?url=x", "https://localhost:80"]:
        with pytest.raises(ValueError):
            mlx_nodes.validate_url(url)
    assert mlx_nodes.validate_url("https://localhost:18443/") == "https://localhost:18443"
    with pytest.raises(ValueError):
        mlx_nodes.certificate_context(pem + pem)


async def test_node_bearer_allowlist_private_environment_and_no_process_adoption(tmp_path, monkeypatch):
    root = tmp_path / "exclusive"
    node = ExclusiveNode(NodeSettings(root=root, token=TOKEN, inference_port=18081, binary="/usr/bin/true"))
    assert node.environment()["HOME"] == str(root / "home")
    assert node.environment()["HF_HOME"] == str(root / "huggingface")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-for-node")
    assert "DEEPSEEK_API_KEY" not in node.environment()
    for alias in ["../../host", "arbitrary/repo"]:
        with pytest.raises(Exception):
            node.alias(alias)
    assert node.alias("mlx-community/Qwen3.5-4B-MLX-4bit") == "qwen3.5-4b"
    monkeypatch.setattr(type(node), "supported", property(lambda _: True))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    occupied = sock.getsockname()[1]
    other = ExclusiveNode(NodeSettings(root=tmp_path / "other", token=TOKEN, inference_port=occupied, binary="/usr/bin/true"))
    try:
        with pytest.raises(Exception) as error:
            await other.start(StartBody(port=occupied))
        assert error.value.status_code == 409 and other.process is None
    finally:
        sock.close()
    await node.stop()
    app = create_node_app(NodeSettings(root=tmp_path / "api", token=TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://node") as client:
        assert (await client.get("/rapid-mlx/status")).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        assert (await client.post("/rapid-mlx/start", json={"model": "../../host"})).status_code == 400
        assert (await client.post("/rapid-mlx/start", json={"port": 22222})).status_code == 400


async def test_node_cache_remove_and_uninstall_only_private_approved_paths(tmp_path):
    node = ExclusiveNode(NodeSettings(root=tmp_path / "node", token=TOKEN))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "precious").write_bytes(b"remain")
    path = node.cache_path("qwen3.5-4b")
    path.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(Exception):
        await node.remove("qwen3.5-4b")
    assert (foreign / "precious").exists()
    path.unlink()
    (path / "snapshots").mkdir(parents=True)
    (path / "blobs").mkdir()
    (path / "blobs" / "owned").write_bytes(b"weights")
    (path / "snapshots" / "link").symlink_to(foreign, target_is_directory=True)
    assert node.cached(["qwen3.5-4b"])["qwen3.5-4b"]
    result = await node.uninstall(True)
    assert result["freed_bytes"] == 7 and result["removed_models"] == ["qwen3.5-4b"]
    assert (foreign / "precious").read_bytes() == b"remain"
    node.acquire()
    twin = ExclusiveNode(NodeSettings(root=tmp_path / "node", token=TOKEN))
    try:
        with pytest.raises(RuntimeError):
            twin.acquire()
    finally:
        node.release()


def test_operator_model_mappings_reject_paths_flags_revisions_and_catalog_overrides():
    assert parse_model_repos(["new-model=operator/New-Model_4bit"]) == {"new-model": "operator/New-Model_4bit"}
    for entry in ["../bad=operator/Model", "--flag=operator/Model", "UPPER=operator/Model",
                  "new=../Model", "new=operator/../Model", "new=/tmp/model", "new=operator/Model@main",
                  "new=https://host/model", "new=operator/Model;command", "new=operator/Model\n",
                  "new=operator/Model=other", "qwen3.5-4b=operator/Replacement"]:
        with pytest.raises(ValueError):
            parse_model_repos([entry])
    with pytest.raises(ValueError):
        parse_model_repos(["new=operator/First", "new=operator/Second"])
    with pytest.raises(ValueError):
        approved_model_catalog({"models": [{"alias": "new", "repo": "operator/Model", "path": "/host"}]})


async def test_custom_model_starts_exact_operator_repo_and_owns_only_its_cache(tmp_path, monkeypatch):
    import asyncio
    repo = "operator/New-Approved-Model"
    models = {"operator-custom": repo, "same-repo-alias": repo}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    node = ExclusiveNode(NodeSettings(root=tmp_path / "node", token=TOKEN,
        binary="/usr/bin/true", inference_port=port, custom_models=models))
    # Mutating the caller's dictionary must not change the running allowlist.
    models["operator-custom"] = "other/Unapproved"
    assert node.alias(repo) == "operator-custom"
    with pytest.raises(Exception):
        node.alias("other/Unapproved")
    calls = []
    class Process:
        returncode = None
        def terminate(self):
            self.returncode = 0
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    async def unhealthy():
        return False
    monkeypatch.setattr(type(node), "supported", property(lambda _: True))
    monkeypatch.setattr(node, "healthy", unhealthy)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await node.start(StartBody(model="operator-custom"))
    assert calls[0][0][1:3] == ("serve", repo)
    assert calls[0][1]["env"]["HF_HOME"] == str(node.root / "huggingface")
    with pytest.raises(Exception) as error:
        await node.remove("same-repo-alias")
    assert error.value.status_code == 409
    await node.stop()
    (node.cache_path("operator-custom") / "snapshots").mkdir(parents=True)
    result = await node.remove("operator-custom")
    assert not result["cached"]["operator-custom"]


async def test_node_catalog_is_authenticated_and_models_exclude_unapproved_rows(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    settings = NodeSettings(root=tmp_path / "node", token=TOKEN,
        custom_models={"new-model": "operator/New-Model"})
    app = create_node_app(settings)
    node = app.state.node
    node.process = SimpleNamespace(returncode=None)
    node.current_model = "new-model"
    real_client = httpx.AsyncClient
    def native(request):
        if request.url.path == '/v1/models':
            return httpx.Response(200,json={'data':[{'id':'operator/New-Model'},{'id':'rogue/Model'}]})
        raw = json.loads(request.content)
        assert raw['model'] == 'operator/New-Model'
        return httpx.Response(200,json={'choices':[{'message':{'content':'protocol-only'}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(native), **kwargs))
    async with real_client(transport=httpx.ASGITransport(app=app),base_url='https://node') as client:
        assert (await client.get('/rapid-mlx/catalog')).status_code == 401
        client.headers['Authorization'] = 'Bearer '+TOKEN
        catalog = (await client.get('/rapid-mlx/catalog')).json()
        assert approved_model_catalog(catalog)['new-model'] == 'operator/New-Model'
        data = (await client.get('/v1/models')).json()['data']
        assert [row['id'] for row in data] == ['operator/New-Model']
        response = await client.post('/v1/chat/completions',json={
            'model':'new-model','messages':[{'role':'user','content':'protocol-only'}]})
        assert response.status_code == 200
        response = await client.post('/v1/chat/completions',json={
            'model':'rogue/Model','messages':[{'role':'user','content':'protocol-only'}]})
        assert response.status_code == 400


async def test_central_model_catalog_accepts_operator_mapping_and_rejects_tenant_repo_before_admission(gateway, tmp_path, monkeypatch):
    database, client, config = await prepare(gateway, tmp_path)
    assert (await client.put('/api/cloud/admin/users/owner/mlx-node',json=config)).status_code == 200
    calls = []
    def transport(request):
        calls.append(request.url.path)
        assert request.headers['authorization'] == 'Bearer '+TOKEN
        if request.url.path == '/rapid-mlx/catalog':
            return httpx.Response(200,json={'models':[{'alias':'operator-custom','repo':'operator/New-Model'}]})
        assert request.url.path == '/v1/models'
        return httpx.Response(200,json={'data':[{'id':'operator/New-Model'},{'id':'rogue/Unapproved'}]})
    monkeypatch.setattr(mlx_nodes,'node_client',lambda *_args, **_kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(transport),trust_env=False,headers={'Authorization':'Bearer '+TOKEN}))
    response = await client.get('/api/cloud/internal/mlx-node/v1/models')
    assert [row['id'] for row in response.json()['data']] == ['operator/New-Model']
    response = await client.post('/api/cloud/internal/mlx-node/v1/chat/completions',json={
        'model':'rogue/Unapproved','messages':[{'role':'user','content':'not dispatched'}]})
    assert response.status_code == 400 and response.json()['error']['code'] == 'model_unavailable'
    rows, _, _ = await records(database)
    assert rows == [] and '/v1/chat/completions' not in calls


async def test_mac_inference_forces_central_quota(gateway, tmp_path, monkeypatch):
    database, client, body = await prepare(gateway, tmp_path)
    assert (await client.put("/api/cloud/admin/users/owner/mlx-node", json=body)).status_code == 200
    vendor = gateway[3]
    monkeypatch.setattr(mlx_nodes, "inference_provider", lambda *_: vendor)
    async def approved(*_):
        return [], {"qwen3.5-4b"}
    monkeypatch.setattr(mlx_nodes, "node_approved_models", approved)
    payload = {"model": "qwen3.5-4b", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 128}
    response = await client.post("/api/cloud/internal/mlx-node/v1/chat/completions", json=payload)
    assert response.status_code == 200, response.text
    rows, _, summary = await records(database)
    assert vendor.calls == 1 and rows[0].source == "mlx-node" and rows[0].state == "settled"
    assert summary["reserved_tokens"] == 0 and summary["used_tokens"] == 130
    async with database.sessions() as db:
        await db.execute(update(User).where(User.id == "owner").values(token_limit=0))
        await db.commit()
    response = await client.post("/api/cloud/internal/mlx-node/v1/chat/completions", json=payload)
    assert response.status_code == 429 and vendor.calls == 1


@pytest.mark.parametrize("node_alias,node_repo", [
    ("qwen2.5-0.5b", "mlx-community/Qwen2.5-0.5B-Instruct-4bit"),
    ("operator-custom", "operator/New-Approved-Model"),
])
async def test_actual_sdk_node_stream_receipt_and_tools(gateway, tmp_path, monkeypatch, node_alias, node_repo):
    import json
    database, client, config = await prepare(gateway, tmp_path)
    assert (await client.put('/api/cloud/admin/users/owner/mlx-node', json=config)).status_code == 200
    def transport(request):
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        if request.url.path == '/rapid-mlx/catalog':
            return httpx.Response(200,json={'models':[{'alias':node_alias,'repo':node_repo}]})
        if request.url.path == '/v1/models':
            return httpx.Response(200,json={'data':[{'id':node_repo}]})
        payload = json.loads(request.content)
        assert payload['model'] == node_alias
        assert payload['tools'][0]['function']['name'] == 'read'
        chunks = [
            {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call_1','type':'function',
                'function':{'name':'read','arguments':'{"path":"file.txt"}'}}]},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]},
            {'choices':[], 'usage':{'prompt_tokens':12,'completion_tokens':5,'total_tokens':17}},
        ]
        events = ''.join('data: '+json.dumps({'id':'chatcmpl-test','object':'chat.completion.chunk',
            'created':1,'model':'qwen2.5-0.5b',**chunk})+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
        return httpx.Response(200,content=events,headers={'Content-Type':'text/event-stream'})
    monkeypatch.setattr(mlx_nodes, 'node_client', lambda *_args, **_kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(transport),trust_env=False,headers={'Authorization':'Bearer '+TOKEN}))
    response = await client.post('/api/cloud/internal/mlx-node/v1/chat/completions', json={
        'model':node_alias,'messages':[{'role':'user','content':'Read the file'}],
        'tools':[{'type':'function','function':{'name':'read','parameters':{'type':'object'}}}], 'max_tokens':128})
    assert response.status_code == 200, response.text
    assert response.json()['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'read'
    rows, _, _ = await records(database)
    assert rows[0].total_tokens == 17 and rows[0].raw_usage['receipt_source'] == 'mac_node_proxy'


async def test_real_tls_pin_rejects_untrusted_leaf_before_sending_bearer(tmp_path):
    import asyncio
    import uvicorn
    from fastapi import FastAPI,Request
    root,pem = credentials(tmp_path)
    _,wrong_pem = credentials(tmp_path,'wrong')
    app = FastAPI()
    received=[]
    @app.get('/probe')
    async def probe(request:Request):
        received.append(request.headers.get('authorization'))
        return {'ok':True}
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    listener.listen()
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app, ssl_keyfile=str(root/'tls.key'),ssl_certfile=str(root/'tls.crt'),
        host='127.0.0.1',port=port,log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.01)
        context,_=mlx_nodes.certificate_context(pem)
        async with httpx.AsyncClient(verify=context,trust_env=False,headers={'Authorization':'Bearer '+TOKEN}) as client:
            assert (await client.get(f'https://localhost:{port}/probe')).status_code == 200
        wrong,_=mlx_nodes.certificate_context(wrong_pem)
        async with httpx.AsyncClient(verify=wrong,trust_env=False,headers={'Authorization':'Bearer secret-never-sent'}) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f'https://localhost:{port}/probe')
        assert received == ['Bearer '+TOKEN]
    finally:
        server.should_exit=True
        await task
        listener.close()


async def test_runtime_original_mlx_routes_use_capability_without_double_metering(monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.cloud.runtime_mlx import install_cloud_mlx
    app=FastAPI()
    class Registry:
        current=None
        def get_provider(self,_): return self.current
        def register(self,provider): self.current=provider
        def unregister(self,_): self.current=None
        async def refresh_provider(self,_): return await self.current.list_models()
    app.state.provider_registry=Registry()
    requests=[]
    def dispatch(request):
        requests.append(request)
        assert request.headers['authorization']=='Bearer runtime-capability'
        if request.url.path.endswith('/models'):
            return httpx.Response(200,json={'object':'list','data':[{'id':'qwen2.5-0.5b','object':'model','created':0,'owned_by':'node'}]})
        return httpx.Response(200,json={'configured':True,'running':True,'current_model':'qwen2.5-0.5b'})
    real_client=httpx.AsyncClient
    class InternalClient(real_client):
        def __init__(self,*args,**kwargs):
            kwargs['transport']=httpx.MockTransport(dispatch)
            super().__init__(*args,**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',InternalClient)
    install_cloud_mlx(app,gateway='http://platform/api/cloud/internal/inference/v1',
        capability='runtime-capability',engine_token='engine-token')
    async with real_client(transport=httpx.ASGITransport(app=app),base_url='http://engine') as client:
        assert (await client.get('/api/rapid-mlx/status')).status_code==401
        client.headers['Authorization']='Bearer engine-token'
        response=await client.get('/api/rapid-mlx/status')
        assert response.status_code==200
        provider=app.state.provider_registry.current
        assert provider.id=='rapid-mlx' and provider._cloud_metered is True
        assert provider._base_url=='http://platform/api/cloud/internal/mlx-node/v1'
        assert (await provider.health_check()).status=='connected'
        assert requests[0].url.path=='/api/cloud/internal/mlx-node/rapid-mlx/status'
        await provider._client.close()
