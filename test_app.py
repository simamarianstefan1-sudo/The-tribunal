import asyncio
import copy
import io
import os
import time
import uuid

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import update

import main
from storage import SessionStore

AUTH = ('board', 'test-password')


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv('BOARD_PASSWORD', AUTH[1])
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setenv('DATABASE_URL', os.getenv('TEST_DATABASE_URL', 'sqlite:///' + (tmp_path / 'sessions.db').as_posix()))


def sample():
    return dict(id=str(uuid.uuid4()), idea=dict(title='Știință și țară', description='Validare cu diacritice și caractere <>& ' * 3, rounds=1),
        status='completed', phase='Finalizat', analyses=[dict(agent='Investor', text='Analiză: șțăîâ <test> & exemple')],
        debates=[dict(round=1, messages=[dict(agent='CEO', text='Dezbatere completă')])],
        votes=[dict(agent='CEO', vote='CONDITIONAL', confidence=60, reasoning='Condiții clare', conditions=['Cerere verificată'])],
        tally={'YES':0, 'NO':0, 'CONDITIONAL':1}, report='Raport strategic: Știință, țară, îndoială.\n' + ('Un paragraf lung cu diacritice șțăîâ.\n' * 200),
        error=None, created_at='2026-11-13T20:00:00+00:00')


@pytest.mark.parametrize('path,method', [('/', 'get'), ('/health', 'get'), ('/api/sessions', 'get'),
    ('/api/sessions/unknown', 'get'), ('/api/sessions/unknown/save', 'post'),
    ('/api/sessions/unknown/export/pdf', 'get'), ('/api/sessions/unknown/export/docx', 'get'), ('/api/sessions', 'post')])
def test_auth(configured, path, method):
    with TestClient(main.app) as client:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, auth=('board', 'wrong')).status_code == 401


def test_restart_save_exports_and_partial(configured):
    s = sample()
    with TestClient(main.app) as client:
        main.app.state.store.create(s)
        assert client.post(f'/api/sessions/{s["id"]}/save', auth=AUTH).json()['persistent']
        # Later worker checkpoints must preserve the manual save marker.
        main.app.state.store.checkpoint(s)
    with TestClient(main.app) as client:
        path = f'/api/sessions/{s["id"]}'
        restored = client.get(path, auth=AUTH).json()
        assert restored['saved_at'] and restored['report'] == s['report']
        assert any(x['id'] == s['id'] for x in client.get('/api/sessions', auth=AUTH).json())
        for fmt in ['pdf', 'docx', 'json']:
            response = client.get(path + '/export/' + fmt, auth=AUTH)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['content-disposition'].endswith(fmt + '"')
            if fmt == 'pdf':
                reader = PdfReader(io.BytesIO(response.content))
                assert len(reader.pages) > 1
                text = '\n'.join(p.extract_text() for p in reader.pages)
            elif fmt == 'docx':
                text = '\n'.join(p.text for p in Document(io.BytesIO(response.content)).paragraphs)
            else:
                assert response.json()['id'] == s['id']
                continue
            for word in ['Știință', 'țară', 'Analiză', 'Dezbatere completă', 'Cerere verificată']:
                assert word in text
        s.update(status='failed', error='Întrerupt', report='')
        main.app.state.store.checkpoint(s)
        assert client.get(path+'/export/pdf', auth=AUTH).status_code == 200
        assert client.get(path+'/export/docx', auth=AUTH).status_code == 200
        assert client.get('/api/sessions/missing', auth=AUTH).status_code == 404
        assert client.post('/api/sessions/missing/save', auth=AUTH).status_code == 404
        assert client.get(path+'/export/zip', auth=AUTH).status_code == 422
        assert client.post(path+'/save', auth=AUTH, headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.post(path+'/save', auth=AUTH, headers={'Origin':'https://testserver'}).status_code == 200


def test_interrupted_and_live_worker(configured):
    with TestClient(main.app):
        store = main.app.state.store
        s = sample()
        s['status'] = 'running'
        store.create(s)
        assert store.get(s['id'])['status'] == 'running'
        with store.engine.begin() as conn:
            conn.execute(update(store.table).where(store.table.c.id == s['id']).values(heartbeat=time.time()-180))
        assert store.get(s['id'])['status'] == 'interrupted'
        assert store.get(s['id'])['analyses'] == s['analyses']
        store.touch(s['id'])
        assert store.get(s['id'])['status'] == 'running'


def test_complete_board_checkpoints(configured, monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def close(self): pass
    async def fake_ask(*args): return 'Rezultat de test șț'
    async def fake_vote(*args): return dict(vote='NO', confidence=70, reasoning='Test', conditions=[])
    monkeypatch.setattr(main, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(main, 'ask', fake_ask)
    monkeypatch.setattr(main, 'ask_vote', fake_vote)
    with TestClient(main.app) as client:
        r = client.post('/api/sessions', auth=AUTH, json=dict(title='Test board', description='Descriere suficient de lungă pentru o sesiune.', rounds=1))
        assert r.status_code == 200
        sid = r.json()['id']
        for _ in range(100):
            result = client.get('/api/sessions/'+sid, auth=AUTH).json()
            if result['status'] != 'running': break
            time.sleep(.02)
        assert result['status'] == 'completed'
        assert len(result['analyses']) == len(result['votes']) == len(result['debates'][0]['messages']) == 12
        assert result['tally']['NO'] == 12
    with TestClient(main.app) as client:
        assert client.get('/api/sessions/'+sid, auth=AUTH).json()['report'] == 'Rezultat de test șț'


def test_fail_closed():
    with pytest.raises(RuntimeError): SessionStore(None)
    with pytest.raises(RuntimeError): SessionStore('sqlite:///:memory:', production=True)


def test_partial_results_survive_shutdown(configured, monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def close(self): pass
    async def slow_ask(client, name, *args):
        if name != 'Investor':
            await asyncio.sleep(60)
        return 'Rezultat salvat înainte de oprire'
    monkeypatch.setattr(main, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(main, 'ask', slow_ask)
    with TestClient(main.app) as client:
        sid = client.post('/api/sessions', auth=AUTH, json=dict(title='Oprire test', description='Descriere suficient de lungă pentru testarea opririi.')).json()['id']
        for _ in range(100):
            s = client.get('/api/sessions/'+sid, auth=AUTH).json()
            if s['analyses']: break
            time.sleep(.02)
        assert len(s['analyses']) == 1
    with TestClient(main.app) as client:
        s = client.get('/api/sessions/'+sid, auth=AUTH).json()
        assert s['status'] == 'interrupted'
        assert s['analyses'][0]['text'] == 'Rezultat salvat înainte de oprire'


def test_unicode_password_and_storage_failure(configured, monkeypatch):
    from sqlalchemy.exc import OperationalError
    monkeypatch.setenv('BOARD_PASSWORD', 'parolă-șț')
    with TestClient(main.app) as client:
        import base64
        auth = {'Authorization':'Basic '+base64.b64encode('board:parolă-șț'.encode()).decode()}
        assert client.get('/health', headers=auth).status_code == 200
        def fail(*args): raise OperationalError('query', {}, Exception('unavailable'))
        monkeypatch.setattr(main.app.state.store, 'save', fail)
        assert client.post('/api/sessions/unknown/save', headers=auth).status_code == 503
