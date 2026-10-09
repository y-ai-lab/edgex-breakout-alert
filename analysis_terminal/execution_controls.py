"""Explicit owner-only new-entry controls; viewing authority never grants trading."""
import time
import uuid

from fastapi import HTTPException
from analysis_terminal import account_view as view, live_execution as live

TTL = 120


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS execution_web_controls (
        request_id TEXT PRIMARY KEY, action TEXT NOT NULL, expected_epoch INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, created_ms INTEGER NOT NULL,
        status TEXT NOT NULL, completed_ms INTEGER, code TEXT)''')


def identifier(value):
    try:
        u = uuid.UUID(value)
        if str(u) != value or u.version != 4:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'UUIDv4 request ID required') from None


class Controller:
    def __init__(self, clock=time.monotonic):
        # A separate random-token namespace: read-only account/Push tokens fail.
        self.sessions = view.Sessions(clock)
        self.pending_auth = {}  # Memory only. No token/proof is written to SQLite.
        self.worker_available = False

    def create(self, conn, config, supplied):
        device = view.authorized_device(conn, config, supplied=supplied)
        value = self.sessions.create(device, view.binding(config.account_id))
        token = value['view_token']
        self.sessions.items[token].update(expires=self.sessions.clock()+TTL,
                                         fingerprint=config.fingerprint())
        return dict(control_token=token, expires_in_seconds=TTL,
                    scope='NEW_ENTRY_CONTROL', read_only=False)

    def authorize(self, conn, config, authorization):
        session = self.sessions.authorize(conn, config, authorization)
        if session['fingerprint'] != config.fingerprint():
            raise HTTPException(401, 'Operation policy changed; authenticate again')
        return session

    def status(self, conn, config, *, now_ms):
        s = live.state(conn)
        runtime = live.report(conn, config, now_ms=now_ms)
        row = conn.execute('''SELECT request_id,action,status,created_ms,completed_ms,code
            FROM execution_web_controls ORDER BY rowid DESC LIMIT 1''').fetchone()
        records = [r for r in live.orders(conn) if r['status'] not in live.TERMINAL]
        active = len(records)
        # Read-only explanations for the existing ON gates, never an override.
        blockers = []
        if not self.worker_available:
            blockers.append('WORKER_UNAVAILABLE')
        if config.mode != 'LIVE':
            blockers.append('LIVE_MODE_REQUIRED')
        if config.errors():
            blockers.append('CONFIGURATION_REQUIRED')
        if s['armed']:
            blockers.append('ALREADY_ARMED')
        if active:
            blockers.append('OWNERSHIP_UNCERTAIN' if any(
                r.get('ownership_quarantined') or r['status'] in {'OWNERSHIP_CONFLICT','OWNERSHIP_UNVERIFIED'}
                for r in records) else 'UNRESOLVED_BOT_RECORDS')
        return dict(scope='NEW_ENTRY_CONTROL', mode=config.mode, armed=s['armed'],
            control_epoch=s.get('control_epoch', 0), worker_available=self.worker_available,
            new_entries_enabled=bool(self.worker_available and runtime['real_orders_enabled']),
            protective_management_enabled=config.mode=='LIVE' and not config.errors(),
            can_request_on=self.worker_available and config.mode=='LIVE' and not config.errors()
                and not s['armed'] and active==0,
            active_orders=active, on_blockers=blockers, observed_ms=now_ms,
            latest_request=dict(zip(('request_id','action','status','created_ms','completed_ms','code'),row)) if row else None)

    def submit(self, conn, config, authorization, *, action, request_id, expected_epoch, now_ms):
        self.authorize(conn, config, authorization)
        identifier(request_id)
        if action not in {'arm','pause'}:
            raise HTTPException(422, 'Only new-entry ON/OFF is permitted')
        conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute('SELECT action,expected_epoch,fingerprint FROM execution_web_controls WHERE request_id=?', (request_id,)).fetchone()
        if prior:
            if tuple(prior) != (action, expected_epoch, config.fingerprint()):
                raise HTTPException(409, 'Operation ID content mismatch')
            return self.status(conn, config, now_ms=now_ms)
        # IDs from CLI/environment controls cannot be re-used through the browser.
        if conn.execute('SELECT 1 FROM live_execution_controls WHERE request_id=?',(request_id,)).fetchone():
            raise HTTPException(409, 'Operation ID already consumed')
        s = live.state(conn)
        if action=='arm':
            if config.mode!='LIVE' or config.errors() or not self.worker_available:
                raise HTTPException(409, 'LIVE worker and configuration required')
            if s['armed'] or s.get('control_epoch',0)!=expected_epoch:
                raise HTTPException(409, 'State changed; refresh before ON')
            if any(r['status'] not in live.TERMINAL for r in live.orders(conn)):
                raise HTTPException(409, 'Unresolved managed position; keep protection active')
            if conn.execute("SELECT 1 FROM execution_web_controls WHERE status IN ('QUEUED','PROCESSING')").fetchone():
                raise HTTPException(409, 'An ON operation is already pending')
        conn.execute('INSERT INTO execution_web_controls VALUES(?,?,?,?,?,?,NULL,NULL)',
            (request_id,action,expected_epoch,config.fingerprint(),now_ms,'QUEUED' if action=='arm' else 'DONE'))
        if action=='pause':
            # Immediate local commit, even while an ON preflight awaits the SDK.
            live.pause(conn,'MANUAL_PAUSE')
            conn.execute("INSERT INTO live_execution_controls VALUES(?,?,'DONE',?,?,NULL)",
                (request_id,action,now_ms,now_ms))
            conn.execute('UPDATE execution_web_controls SET completed_ms=? WHERE request_id=?',(now_ms,request_id))
            conn.execute("UPDATE execution_web_controls SET status='REFUSED',completed_ms=?,code='SUPERSEDED_BY_OFF' WHERE status IN ('QUEUED','PROCESSING')",(now_ms,))
            self.pending_auth.clear()
        else:
            self.pending_auth.clear()
            self.pending_auth[request_id] = authorization
        return self.status(conn,config,now_ms=now_ms)

    def recover(self, conn, *, now_ms):
        # Startup never resumes a queued/interrupted ON without live authorization.
        if conn.execute("SELECT 1 FROM execution_web_controls WHERE status='PROCESSING'").fetchone():
            live.pause(conn,'WEB_CONTROL_INTERRUPTED')
        conn.execute("UPDATE live_execution_controls SET status='REFUSED',completed_ms=?,code='CONTROL_INTERRUPTED' WHERE status='PROCESSING' AND request_id IN (SELECT request_id FROM execution_web_controls WHERE status='PROCESSING')",(now_ms,))
        conn.execute("UPDATE execution_web_controls SET status='REFUSED',completed_ms=?,code='AUTHORIZATION_LOST' WHERE status IN ('QUEUED','PROCESSING')",(now_ms,))
        self.pending_auth.clear()

    async def process(self, engine):
        with engine.db_connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT request_id,expected_epoch,fingerprint,created_ms FROM execution_web_controls WHERE status='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return
            request_id, epoch, fingerprint, created_ms = row
            authorization = self.pending_auth.get(request_id)
            try:
                self.authorize(conn,engine.config,authorization)
                s = live.state(conn)
                if fingerprint!=engine.config.fingerprint() or s['armed'] or s.get('control_epoch',0)!=epoch:
                    raise HTTPException(409,'STALE_CONTROL_STATE')
                if not 0 <= engine.clock()-created_ms < 30000:
                    raise HTTPException(409,'CONTROL_EXPIRED')
                if any(r['status'] not in live.TERMINAL for r in live.orders(conn)):
                    raise HTTPException(409,'UNRESOLVED_EXECUTION_LEDGER')
            except HTTPException as exc:
                code = exc.detail if exc.detail in {'STALE_CONTROL_STATE','CONTROL_EXPIRED','UNRESOLVED_EXECUTION_LEDGER'} else 'AUTHORIZATION_LOST'
                conn.execute("UPDATE execution_web_controls SET status='REFUSED',completed_ms=?,code=? WHERE request_id=?",(engine.clock(),code,request_id))
                self.pending_auth.pop(request_id,None)
                return
            conn.execute("UPDATE execution_web_controls SET status='PROCESSING' WHERE request_id=?",(request_id,))

        def final_authorize(conn):
            if engine.config.mode != "LIVE" or engine.config.errors() or not self.worker_available:
                raise HTTPException(409, "LIVE worker and configuration required")
            self.authorize(conn,engine.config,authorization)
            current = conn.execute('SELECT status FROM execution_web_controls WHERE request_id=?',(request_id,)).fetchone()
            if not current or current[0]!='PROCESSING':
                raise HTTPException(409,'Operation no longer pending')

        try:
            await live.apply_control(engine, 'arm:'+request_id, authorize=final_authorize, expected_epoch=epoch)
            with engine.db_connect() as conn:
                control = conn.execute('SELECT status,code FROM live_execution_controls WHERE request_id=?',(request_id,)).fetchone()
                status,code = tuple(control) if control else ('REFUSED','CONTROL_INCOMPLETE')
                if status!='DONE':
                    status='REFUSED'
                conn.execute('UPDATE execution_web_controls SET status=?,completed_ms=?,code=? WHERE request_id=?',
                    (status,engine.clock(),code,request_id))
        finally:
            self.pending_auth.pop(request_id,None)


CONTROLLER = Controller()
