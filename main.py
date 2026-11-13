import base64
import hmac
import asyncio
import json
import logging
import traceback
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError, RateLimitError, AuthenticationError
from pydantic import BaseModel, Field

load_dotenv()
app = FastAPI(title='THE BOARD', version='1.0')
ROOT = Path(__file__).parent
SESSIONS: dict[str, dict] = {}
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('the_board')

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
        valid = scheme.lower() == 'basic' and hmac.compare_digest(username, 'board') and hmac.compare_digest(supplied, password)
    except (ValueError, UnicodeDecodeError):
        valid = False
    if not valid:
        return Response('Authentication required', status_code=401, headers={'WWW-Authenticate': 'Basic realm="THE BOARD"'})
    return await call_next(request)


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
    vote: Literal['YES', 'NO', 'CONDITIONAL']
    confidence: int = Field(ge=0, le=100)
    reasoning: str
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

async def ask_vote(client, role, specialty, focus, task):
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {'role':'system','content': SYSTEM + f'\nRole: {role} ({specialty}). Focus: {focus}. Return only valid JSON with keys vote (YES/NO/CONDITIONAL), confidence (integer 0-100), reasoning (string), conditions (array of strings).'},
            {'role':'user','content':task},
        ],
        response_format={'type':'json_object'},
        temperature=0.25,
        max_tokens=400,
    )
    raw = response.choices[0].message.content or '{}'
    return Vote.model_validate(json.loads(raw)).model_dump()

async def run_board(sid: str):
    s = SESSIONS[sid]
    try:
        client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'), timeout=40.0, max_retries=1)
        idea = s['idea']
        semaphore = asyncio.Semaphore(2)  # Limit parallel requests to reduce rate-limit pressure
        async def bounded(fn, *args):
            async with semaphore:
                return await fn(client, *args)
        logger.info('Session %s: starting independent analysis, model=%s', sid, MODEL)
        s['phase'] = 'Analiză independentă'
        tasks = [bounded(ask, name, spec, focus,
            f'IDEA: {idea_text(idea)}\nProvide an independent assessment (150-220 words): strongest opportunity, biggest flaw, short-term (0-12 months), long-term (1-5 years), one key question and one test. Do not assume other agents agree.') for name,spec,focus in ROLES]
        first = await asyncio.gather(*tasks)
        s['analyses'] = [{'agent':r[0], 'text':t} for r,t in zip(ROLES,first)]
        logger.info('Session %s: independent analysis complete', sid)
        s['phase'] = 'Dezbatere'
        previous = first
        for round_number in range(1, idea['rounds'] + 1):
            # Shared summaries ensure every agent sees counterarguments, while keeping requests bounded.
            discussion = '\n'.join(f'{ROLES[i][0]}: {t[:650]}' for i,t in enumerate(previous))
            tasks = [bounded(ask, name, spec, focus,
                f'IDEA: {idea_text(idea)}\nROUND {round_number}. Other committee positions:\n{discussion}\nChallenge at least one named colleague with a concrete reason, acknowledge a valid objection, propose a measurable mitigation, and state what would change your mind. 100-160 words.', 500) for name,spec,focus in ROLES]
            previous = await asyncio.gather(*tasks)
            s['debates'].append({'round':round_number, 'messages':[{'agent':r[0], 'text':t} for r,t in zip(ROLES,previous)]})
        logger.info('Session %s: debate complete', sid)
        s['phase'] = 'Vot final'
        debate = '\n'.join(f'{ROLES[i][0]}: {t[:550]}' for i,t in enumerate(previous))
        tasks = [bounded(ask_vote, name, spec, focus,
            f'IDEA: {idea_text(idea)}\nFinal debate:\n{debate}\nVote independently YES, NO, or CONDITIONAL. Explain with short-term and long-term tradeoffs, cite conditions required for investment. Confidence means confidence in your assessment, NOT probability of business success.') for name,spec,focus in ROLES]
        votes = await asyncio.gather(*tasks)
        s['votes'] = [{'agent':r[0], **v} for r,v in zip(ROLES,votes)]
        tally = {v:sum(x['vote']==v for x in votes) for v in ['YES','NO','CONDITIONAL']}
        s['tally'] = tally
        logger.info('Session %s: votes complete', sid)
        s['phase'] = 'Raport strategic'
        summary = await bounded(ask, 'CEO', 'Chair and synthesis author', 'Transparent synthesis',
            f'IDEA: {idea_text(idea)}\nTALLY: {tally}\nVOTES: {json.dumps(s["votes"],ensure_ascii=False)}\nLATEST DEBATE: {debate}\nWrite a detailed structured Romanian executive report with sections: verdict (explicitly acknowledge vote counts and disagreement), 0-3 months, 3-12 months, 1-3 years, 3-5 years, unit economics and break-even formulas with inputs marked unknown, optimistic/base/pessimistic scenarios with illustrative assumptions only, top 5 risks, 30-day validation plan, kill criteria, what information is missing. Do not invent numerical certainty.', 2200)
        s['report'] = summary
        s['phase'] = 'Finalizat'
        s['status'] = 'completed'
        logger.info('Session %s: completed', sid)
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
            s['error'] = f'{type(e).__name__}: {str(e)[:250]}'


@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')

@app.post('/api/sessions')
async def create_session(idea: Idea):
    if not os.getenv('OPENAI_API_KEY'):
        raise HTTPException(503, 'OPENAI_API_KEY lipsește. Configurează variabila în Render → Environment.')
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {'id':sid, 'idea':idea.model_dump(), 'status':'running', 'phase':'Pornire', 'analyses':[], 'debates':[], 'votes':[], 'tally':{}, 'report':'', 'error':None, 'created_at':datetime.now(timezone.utc).isoformat()}
    asyncio.create_task(run_board(sid))
    return {'id':sid}

@app.get('/api/sessions/{sid}')
async def get_session(sid: str):
    if sid not in SESSIONS:
        raise HTTPException(404, 'Sesiune inexistentă sau server repornit.')
    return SESSIONS[sid]

@app.get('/health')
async def health():
    return {'ok':True, 'api_key_configured':bool(os.getenv('OPENAI_API_KEY')), 'model':MODEL}

