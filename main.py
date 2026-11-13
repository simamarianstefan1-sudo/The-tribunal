import base64
import hmac
import asyncio
import json
import logging
import traceback
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, Response
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError, AuthenticationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from storage import SessionStore
from exports import pdf_bytes, word_bytes

load_dotenv()
@asynccontextmanager
async def lifespan(app):
    app.state.store = await asyncio.to_thread(SessionStore, os.getenv('DATABASE_URL'), production=os.getenv('RENDER') == 'true')
    app.state.tasks = set()
    yield
    for task in list(app.state.tasks):
        task.cancel()
    await asyncio.gather(*app.state.tasks, return_exceptions=True)
    await asyncio.to_thread(app.state.store.close)


app = FastAPI(title='THE BOARD', version='1.2', lifespan=lifespan)
ROOT = Path(__file__).parent
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('the_board')


@app.exception_handler(SQLAlchemyError)
async def storage_unavailable(request, exc):
    logger.error('Session storage unavailable (%s)', type(exc).__name__)
    return Response('Baza de date nu este disponibilă. Reîncearcă salvarea.', status_code=503,
                    headers={'Cache-Control': 'no-store'})

@app.middleware('http')
async def require_password(request: Request, call_next):
    password = os.getenv('BOARD_PASSWORD', '')
    if not password:
        return Response('BOARD_PASSWORD must be configured before using the app.', status_code=503)
    auth = request.headers.get('authorization', '')
    try:
        scheme, encoded = auth.split(' ', 1)
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        username, supplied = decoded.split(':', 1)
        valid = scheme.lower() == 'basic' and hmac.compare_digest(username.encode(), b'board') and hmac.compare_digest(supplied.encode(), password.encode())
    except (ValueError, UnicodeDecodeError):
        valid = False
    if not valid:
        return Response('Authentication required', status_code=401, headers={'WWW-Authenticate': 'Basic realm="THE BOARD"'})
    # Basic authentication is shared by all routes, including exports and history.
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin:
            # Render terminates HTTPS upstream; the ASGI connection may be HTTP.
            # Compare the public host without trusting forwarded scheme headers.
            source = urlsplit(origin)
            if source.scheme not in ('http', 'https') or source.netloc != request.url.netloc or source.path:
                return Response('Cross-origin requests are not allowed', status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


async def checkpoint(session):
    # Copy on the event loop, before handing a snapshot to the DB thread.
    snapshot = json.loads(json.dumps(session))
    await asyncio.to_thread(app.state.store.checkpoint, snapshot)


async def heartbeat(sid):
    while True:
        await asyncio.sleep(15)
        await asyncio.to_thread(app.state.store.touch, sid)


async def load_session(sid):
    session = await asyncio.to_thread(app.state.store.get, sid)
    if session is None:
        raise HTTPException(404, 'Sesiune inexistentă.')
    return session


ROLES = [
 ('Investor', 'Venture investor', 'Investment returns, capital allocation, exit potential'),
 ('Accountant', 'CFO / accountant', 'Unit economics, costs, cash flow, taxes as jurisdiction-dependent'),
 ('Market Analyst', 'Market researcher', 'TAM/SAM/SOM, competitors, demand validation'),
 ('Marketing', 'Growth strategist', 'Acquisition channels, CAC, brand, conversion'),
 ('Legal', 'Legal and compliance analyst', 'Regulatory hurdles, contracts, privacy; not legal advice'),
 ('Customer', 'Skeptical target customer', 'Willingness to pay, friction, real customer jobs'),
 ('Technology', 'CTO', 'Technical feasibility, security, implementation cost'),
 ('Risk', 'Risk officer / devil’s advocate', 'Failure modes, hidden assumptions, downside scenarios'),
 ('Strategy', 'Business strategist', 'Differentiation, positioning, defensibility'),
 ('Operations', 'COO', 'Fulfillment, staffing, suppliers, operational bottlenecks'),
 ('Innovation', 'Product innovator', 'Experiments, product iteration, alternative approaches'),
 ('CEO', 'CEO / chair', 'Execution priorities, governance, balanced decision-making'),
]

class Idea(BaseModel):
    title: str = Field(min_length=3, max_length=150)
    description: str = Field(min_length=30, max_length=6000)
    budget: str = Field(default='Unknown', max_length=120)
    location: str = Field(default='Romania', max_length=120)
    target_customer: str = Field(default='Unknown', max_length=250)
    rounds: int = Field(default=2, ge=1, le=3)

class Vote(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    vote: Literal['YES', 'NO', 'CONDITIONAL']
    confidence: int = Field(ge=0, le=100)
    reasoning: str = Field(min_length=1)
    conditions: list[str]

SYSTEM = '''You are one distinct member of a 12-person business investment committee. Stay faithful to your assigned expertise. Respond in Romanian. Be incisive, specific, and willing to disagree. Never invent market facts, research, regulations, pricing, or sources. If not supplied or verified, mark numbers as illustrative assumptions. Distinguish facts, assumptions, and unknowns. Provide practical and falsifiable next steps. No false consensus. Do not claim to have browsed or performed independent research. You are an AI simulation of an expert role, not a licensed professional.'''

def idea_text(idea):
    return json.dumps(idea, ensure_ascii=False)

async def ask(client, role, specialty, focus, task, max_tokens=650):
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM + f'\nRole: {role} ({specialty}). Focus: {focus}.'},
            {'role': 'user', 'content': task},
        ],
        temperature=0.45,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or '').strip()

class VoteUnavailable(Exception):
    """Expected member failure, safe to persist without provider response text."""
    def __init__(self, reason, attempts):
        self.reason = reason
        self.attempts = attempts
        super().__init__(reason)


async def ask_vote(client, role, specialty, focus, task):
    # Fresh generations, never repair or count a partial JSON document as a vote.
    for attempt, budget in enumerate((1000, 2000, 3000), 1):
        response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {'role':'system','content': SYSTEM + f'\nRole: {role} ({specialty}). Focus: {focus}. Return JSON matching the supplied schema. Keep reasoning under 120 Romanian words and at most 4 concise conditions.'},
            {'role':'user','content':task + ('\nThe previous generation was incomplete or invalid. Produce a fresh, concise, complete JSON vote.' if attempt > 1 else '')},
        ],
        response_format={'type':'json_schema', 'json_schema': {
            'name':'board_vote', 'strict':True, 'schema':Vote.model_json_schema()}},
        temperature=0.25,
        max_tokens=budget,
        )
        choice = response.choices[0] if response.choices else None
        finish = choice.finish_reason if choice else None
        usage = getattr(response, 'usage', None)
        logger.info('Vote role=%s attempt=%s model=%s max_tokens=%s finish_reason=%s completion_tokens=%s',
                    role, attempt, response.model, budget, finish, getattr(usage, 'completion_tokens', None))
        if choice and getattr(choice.message, 'refusal', None):
            raise VoteUnavailable('refusal', attempt)
        if finish == 'content_filter':
            raise VoteUnavailable('content_filter', attempt)
        reason = 'incomplete_response'
        if choice and finish == 'stop':
            try:
                return Vote.model_validate_json(choice.message.content or '').model_dump()
            except ValidationError:
                reason = 'invalid_json_or_schema'
        elif finish == 'length':
            reason = 'token_limit'
        logger.warning('Vote rejected role=%s attempt=%s reason=%s', role, attempt, reason)
        if attempt < 3:
            await asyncio.sleep(0.5 * attempt)
    raise VoteUnavailable(reason, attempt)

async def run_board(sid: str):
    s = await load_session(sid)
    pulse = asyncio.create_task(heartbeat(sid))
    client = None
    try:
        client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'), timeout=40.0, max_retries=1)
        idea = s['idea']
        semaphore = asyncio.Semaphore(2)  # Limit parallel requests to reduce rate-limit pressure
        async def bounded(fn, *args):
            async with semaphore:
                return await fn(client, *args)
        async def collect(tasks, target, *, votes=False):
            """Persist each completed member, including partial rounds on interruption."""
            completed = {}
            failures = {}
            lock = asyncio.Lock()
            async def member(i, work):
                try:
                    result = await work
                except (VoteUnavailable, APIConnectionError, APIStatusError) as exc:
                    if not votes:
                        raise
                    result = None
                    failures[i] = {'agent': ROLES[i][0],
                        'reason': exc.reason if isinstance(exc, VoteUnavailable) else type(exc).__name__,
                        'attempts': exc.attempts if isinstance(exc, VoteUnavailable) else None}
                    logger.warning('Session %s vote unavailable role=%s reason=%s', sid, ROLES[i][0], failures[i]['reason'])
                async with lock:
                    if result is not None:
                        completed[i] = result
                    target[:] = [({'agent': ROLES[j][0], **completed[j]} if votes else
                                  {'agent': ROLES[j][0], 'text': completed[j]}) for j in sorted(completed)]
                    if votes:
                        s['vote_errors'] = [failures[j] for j in sorted(failures)]
                        s['tally'] = {v:sum(x['vote'] == v for x in target) for v in ['YES','NO','CONDITIONAL']}
                    await checkpoint(s)
                return result
            pending = [asyncio.create_task(member(i, work)) for i, work in enumerate(tasks)]
            try:
                return await asyncio.gather(*pending)
            finally:
                for work in pending:
                    if not work.done():
                        work.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        logger.info('Session %s: starting independent analysis, model=%s', sid, MODEL)
        s['phase'] = 'Analiză independentă'
        await checkpoint(s)
        tasks = [bounded(ask, name, spec, focus,
            f'IDEA: {idea_text(idea)}\nProvide an independent assessment (150-220 words): strongest opportunity, biggest flaw, short-term (0-12 months), long-term (1-5 years), one key question and one test. Do not assume other agents agree.') for name,spec,focus in ROLES]
        first = await collect(tasks, s['analyses'])
        s['analyses'] = [{'agent':r[0], 'text':t} for r,t in zip(ROLES,first)]
        logger.info('Session %s: independent analysis complete', sid)
        s['phase'] = 'Dezbatere'
        await checkpoint(s)
        previous = first
        for round_number in range(1, idea['rounds'] + 1):
            # Shared summaries ensure every agent sees counterarguments, while keeping requests bounded.
            discussion = '\n'.join(f'{ROLES[i][0]}: {t[:650]}' for i,t in enumerate(previous))
            tasks = [bounded(ask, name, spec, focus,
                f'IDEA: {idea_text(idea)}\nROUND {round_number}. Other committee positions:\n{discussion}\nChallenge at least one named colleague with a concrete reason, acknowledge a valid objection, propose a measurable mitigation, and state what would change your mind. 100-160 words.', 500) for name,spec,focus in ROLES]
            current_round = {'round':round_number, 'messages':[]}
            s['debates'].append(current_round)
            previous = await collect(tasks, current_round['messages'])
            await checkpoint(s)
        logger.info('Session %s: debate complete', sid)
        s['phase'] = 'Vot final'
        await checkpoint(s)
        debate = '\n'.join(f'{ROLES[i][0]}: {t[:550]}' for i,t in enumerate(previous))
        tasks = [bounded(ask_vote, name, spec, focus,
            f'IDEA: {idea_text(idea)}\nFinal debate:\n{debate}\nVote independently YES, NO, or CONDITIONAL. Explain with short-term and long-term tradeoffs, cite conditions required for investment. Confidence means confidence in your assessment, NOT probability of business success.') for name,spec,focus in ROLES]
        votes = await collect(tasks, s['votes'], votes=True)
        votes = [v for v in votes if v is not None]
        tally = {v:sum(x['vote']==v for x in votes) for v in ['YES','NO','CONDITIONAL']}
        s['tally'] = tally
        logger.info('Session %s: votes complete', sid)
        s['phase'] = 'Raport strategic'
        await checkpoint(s)
        summary = await bounded(ask, 'CEO', 'Chair and synthesis author', 'Transparent synthesis',
            f'IDEA: {idea_text(idea)}\nTALLY: {tally}\nVOTES: {json.dumps(s["votes"],ensure_ascii=False)}\nUNAVAILABLE VOTES: {json.dumps(s.get("vote_errors", []))}. Only {len(votes)} of {len(ROLES)} votes are valid. Missing votes are NOT NO or abstentions. Explicitly disclose incomplete voting; if none are valid, give no committee verdict.\nLATEST DEBATE: {debate}\nWrite a detailed structured Romanian executive report with sections: verdict (explicitly acknowledge vote counts and disagreement), 0-3 months, 3-12 months, 1-3 years, 3-5 years, unit economics and break-even formulas with inputs marked unknown, optimistic/base/pessimistic scenarios with illustrative assumptions only, top 5 risks, 30-day validation plan, kill criteria, what information is missing. Do not invent numerical certainty.', 2200)
        s['report'] = summary
        s['phase'] = 'Finalizat cu voturi indisponibile' if s.get('vote_errors') else 'Finalizat'
        s['status'] = 'completed'
        logger.info('Session %s: completed', sid)
        await checkpoint(s)
    except asyncio.CancelledError:
        s.update(status='interrupted', phase='Întrerupt', error='Serverul s-a oprit. Rezultatele salvate rămân disponibile.')
        await checkpoint(s)
        raise
    except Exception as e:
        # Logs show the root cause, including chained network/DNS/TLS exceptions.
        # Never log request headers, API keys, or the user's business idea.
        logger.exception('Session %s failed at phase %s (%s)', sid, s.get('phase'), type(e).__name__)
        s['status'] = 'failed'
        s['phase'] = 'Eroare'
        if isinstance(e, AuthenticationError):
            s['error'] = 'OpenAI a respins cheia API (401). Verifică OPENAI_API_KEY în Render.'
        elif isinstance(e, RateLimitError):
            s['error'] = 'OpenAI a returnat 429: limită de cereri sau credite insuficiente. Verifică Usage/Billing în OpenAI Platform.'
        elif isinstance(e, APITimeoutError):
            s['error'] = 'Conexiunea OpenAI a expirat. Verifică logurile Render pentru cauza exactă.'
        elif isinstance(e, APIConnectionError):
            s['error'] = 'Nu se poate stabili conexiunea cu OpenAI. Verifică logurile Render (DNS/TLS/rețea).'
        elif isinstance(e, APIStatusError):
            s['error'] = f'OpenAI a returnat HTTP {e.status_code}. Verifică modelul, permisiunile și logurile Render.'
        else:
            s['error'] = 'Procesarea a eșuat. Rezultatele salvate rămân disponibile. Verifică logurile serverului.'
        await checkpoint(s)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            try:
                await pulse
            except Exception as exc:
                logger.error('Session heartbeat stopped (%s)', type(exc).__name__)
        if client is not None:
            await client.close()


@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')

@app.post('/api/sessions')
async def create_session(idea: Idea):
    if not os.getenv('OPENAI_API_KEY'):
        raise HTTPException(503, 'OPENAI_API_KEY lipsește. Configurează variabila în Render → Environment.')
    sid = str(uuid.uuid4())
    session = {'id':sid, 'idea':idea.model_dump(), 'status':'running', 'phase':'Pornire', 'analyses':[], 'debates':[], 'votes':[], 'tally':{}, 'report':'', 'error':None, 'created_at':datetime.now(timezone.utc).isoformat()}
    await asyncio.to_thread(app.state.store.create, session)
    task = asyncio.create_task(run_board(sid))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    return {'id':sid}

@app.get('/api/sessions/{sid}')
async def get_session(sid: str):
    return await load_session(sid)


@app.get('/api/sessions')
async def list_sessions(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await asyncio.to_thread(app.state.store.list, limit, offset)


@app.post('/api/sessions/{sid}/save')
async def save_session(sid: str):
    stamp = await asyncio.to_thread(app.state.store.save, sid)
    if stamp is None:
        raise HTTPException(404, 'Sesiune inexistentă.')
    return {'id': sid, 'saved_at': stamp, 'persistent': True}


@app.get('/api/sessions/{sid}/export/{format}')
async def export_session(sid: str, format: Literal['pdf', 'docx', 'json']):
    session = await load_session(sid)
    if format == 'json':
        data = json.dumps(session, ensure_ascii=False, indent=2).encode('utf-8')
        media = 'application/json'
    else:
        data = await asyncio.to_thread(pdf_bytes if format == 'pdf' else word_bytes, session)
        media = 'application/pdf' if format == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    # Never interpolate a user-controlled title or raw path into the header.
    safe_id = str(uuid.UUID(session['id']))
    return Response(data, media_type=media, headers={'Content-Disposition': f'attachment; filename="the-board-{safe_id}.{format}"'})

@app.get('/health')
async def health():
    await asyncio.to_thread(app.state.store.list, 1, 0)
    return {'ok':True, 'storage':'postgresql' if app.state.store.engine.dialect.name == 'postgresql' else 'sqlite-development', 'api_key_configured':bool(os.getenv('OPENAI_API_KEY')), 'model':MODEL, 'version':'1.2'}

