import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError
import httpx

import main
from test_app import AUTH, configured, sample


VALID = dict(vote='CONDITIONAL', confidence=75, reasoning='Validare cerere șț', conditions=['Test plătit'])


def response(raw=None, finish='stop', refusal=None):
    return NS(model='gpt-4.1-mini', usage=NS(completion_tokens=400), choices=[NS(
        finish_reason=finish, message=NS(content=json.dumps(VALID) if raw is None else raw, refusal=refusal))])


def client_for(*responses):
    return NS(chat=NS(completions=NS(create=AsyncMock(side_effect=responses))))


@pytest.mark.parametrize('bad', [response('{"reasoning":"unfinished', 'length'),
    response('{bad'), response(''), response('{}'), response(json.dumps({**VALID, 'confidence':101})),
    response(json.dumps({**VALID, 'confidence':'75'})), response(json.dumps({**VALID, 'confidence':True})),
    response(json.dumps({**VALID, 'vote':'MAYBE'})), response(json.dumps({**VALID, 'conditions':'test'})),
    response(json.dumps({**VALID, 'reasoning':''})), response(json.dumps({**VALID, 'extra':1})),
    response(finish='length'), NS(model='test', usage=None, choices=[])])
def test_retry_invalid_or_incomplete(bad, monkeypatch, caplog):
    monkeypatch.setattr(main.asyncio, 'sleep', AsyncMock())
    client = client_for(bad, response())
    assert asyncio.run(main.ask_vote(client, 'Investor', 'spec', 'focus', 'private idea')) == VALID
    calls = client.chat.completions.create.call_args_list
    assert [c.kwargs['max_tokens'] for c in calls] == [1000, 2000]
    schema = calls[0].kwargs['response_format']['json_schema']
    assert schema['strict'] and schema['schema']['additionalProperties'] is False
    assert 'private idea' not in caplog.text and 'unfinished' not in caplog.text


def test_exhausted_retry_and_refusal(monkeypatch):
    monkeypatch.setattr(main.asyncio, 'sleep', AsyncMock())
    client = client_for(*[response('{', 'length')] * 3)
    with pytest.raises(main.VoteUnavailable) as caught:
        asyncio.run(main.ask_vote(client, 'Investor', 'spec', 'focus', 'task'))
    assert caught.value.reason == 'token_limit' and caught.value.attempts == 3
    assert [c.kwargs['max_tokens'] for c in client.chat.completions.create.call_args_list] == [1000,2000,3000]
    for item in [response(refusal='private refusal'), response(finish='content_filter')]:
        client = client_for(item)
        with pytest.raises(main.VoteUnavailable):
            asyncio.run(main.ask_vote(client, 'Investor', 'spec', 'focus', 'task'))
        assert client.chat.completions.create.call_count == 1


def test_cancellation_propagates():
    client = client_for(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.ask_vote(client, 'Investor', 'spec', 'focus', 'task'))


@pytest.mark.parametrize('failures', [1, 12])
def test_failed_votes_preserve_session_and_exports(configured, monkeypatch, failures):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def close(self): pass
    prompts = []
    async def ask(*args):
        prompts.append(args[4])
        return 'Analiză și raport păstrate'
    async def vote(client, role, *args):
        if role in [r[0] for r in main.ROLES[:failures]]:
            if role == 'Investor':
                raise main.VoteUnavailable('token_limit', 3)
            raise APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com'))
        await asyncio.sleep(.01)
        return VALID.copy()
    monkeypatch.setattr(main, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(main, 'ask', ask)
    monkeypatch.setattr(main, 'ask_vote', vote)
    with TestClient(main.app) as client:
        sid = client.post('/api/sessions', auth=AUTH, json=dict(title='Vot parțial', description='Descriere suficient de lungă pentru testare.', rounds=1)).json()['id']
        for _ in range(200):
            s = client.get('/api/sessions/'+sid, auth=AUTH).json()
            if s['status'] != 'running': break
            time.sleep(.02)
        assert s['status'] == 'completed' and 'indisponibile' in s['phase']
        assert len(s['analyses']) == len(s['debates'][0]['messages']) == 12
        assert len(s['votes']) == 12-failures
        assert len(s['vote_errors']) == failures
        assert s['tally'] == dict(YES=0, NO=0, CONDITIONAL=12-failures)
        assert 'UNAVAILABLE VOTES' in prompts[-1]
        assert f'Only {12-failures} of 12 votes are valid' in prompts[-1]
        assert client.post(f'/api/sessions/{sid}/save', auth=AUTH).status_code == 200
        for fmt in ('pdf', 'docx', 'json'):
            assert client.get(f'/api/sessions/{sid}/export/{fmt}', auth=AUTH).status_code == 200
    with TestClient(main.app) as client:
        restored = client.get('/api/sessions/'+sid, auth=AUTH).json()
        assert restored['saved_at'] and restored['vote_errors'] == s['vote_errors']
        assert restored['report'] == 'Analiză și raport păstrate'
