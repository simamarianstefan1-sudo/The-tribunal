"""Transactional session snapshots. PostgreSQL in production; explicit SQLite for tests/dev."""
import json
import time
from datetime import datetime, timezone

from sqlalchemy import Column, Float, MetaData, String, Table, Text, create_engine, select


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, url, *, production=False):
        if not url:
            raise RuntimeError('DATABASE_URL is required. Configure persistent PostgreSQL before deploying.')
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+psycopg://' + url.split('://', 1)[1]
        if production and not url.startswith('postgresql+psycopg://'):
            raise RuntimeError('Render requires persistent PostgreSQL, not a local SQLite file.')
        options = {'connect_args': {'connect_timeout': 10}, 'pool_size': 3, 'max_overflow': 2} if url.startswith('postgresql+') else {}
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, **options)
        metadata = MetaData()
        self.table = Table('board_sessions', metadata,
            Column('id', String(36), primary_key=True),
            Column('payload', Text, nullable=False),
            Column('created_at', String(40), nullable=False, index=True),
            Column('heartbeat', Float, nullable=False),
            Column('saved_at', String(40)))
        metadata.create_all(self.engine)

    def create(self, session):
        with self.engine.begin() as conn:
            conn.execute(self.table.insert().values(id=session['id'],
                payload=json.dumps(session, ensure_ascii=False), created_at=session['created_at'],
                heartbeat=time.time()))

    def checkpoint(self, session):
        # saved_at is separate so an in-flight snapshot cannot overwrite a manual save.
        with self.engine.begin() as conn:
            conn.execute(self.table.update().where(self.table.c.id == session['id']).values(
                payload=json.dumps(session, ensure_ascii=False), heartbeat=time.time()))

    def touch(self, sid):
        with self.engine.begin() as conn:
            conn.execute(self.table.update().where(self.table.c.id == sid).values(heartbeat=time.time()))

    @staticmethod
    def decode(row):
        session = json.loads(row.payload)
        session['saved_at'] = row.saved_at
        if session['status'] == 'running' and time.time() - row.heartbeat > 120:
            session.update(status='interrupted', phase='Întrerupt',
                error='Procesarea a fost întreruptă. Rezultatele salvate sunt disponibile; pornește o sesiune nouă pentru o analiză completă.')
        return session

    def get(self, sid):
        with self.engine.connect() as conn:
            row = conn.execute(select(self.table).where(self.table.c.id == sid)).first()
        return self.decode(row) if row else None

    def list(self, limit=50, offset=0):
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.table).order_by(self.table.c.created_at.desc(), self.table.c.id).limit(limit).offset(offset)).all()
        return [{k: s[k] for k in ('id', 'idea', 'status', 'phase', 'created_at', 'saved_at')}
                for s in map(self.decode, rows)]

    def save(self, sid):
        stamp = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(self.table.update().where(self.table.c.id == sid).values(saved_at=stamp))
        return stamp if result.rowcount else None

    def close(self):
        self.engine.dispose()
