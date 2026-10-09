"""Private, read-only EdgeX account view; no trading signer or account controls."""
import asyncio
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from analysis_terminal import push_security
from analysis_terminal.edgex_orders import BASE_URL, data

READ_PATHS = {
    'asset': '/api/v2/private/account/getAccountAsset',
    'positions': '/api/v2/private/account/getPositionTransactionPage',
    'collateral': '/api/v2/private/account/getCollateralTransactionPage',
    'orders': '/api/v2/private/order/getActiveOrderPage',
}
DAY = 86400000
PAGE_SIZE = 50

def binding(account_id):
    return hashlib.sha256(('edgex-account-view-account-v1\0'+account_id).encode()).hexdigest()

def identity(subscription):
    return hashlib.sha256(('edgex-account-view-owner-v1\0'+push_security.management_token(subscription)).encode()).hexdigest()

def initialize(conn, config, *, now_ms):
    """Seal the sole pre-existing device once; public new registrations cannot enroll."""
    conn.execute('''CREATE TABLE IF NOT EXISTS account_view_owner (
        id INTEGER PRIMARY KEY CHECK(id=1), device_hash TEXT, account_hash TEXT,
        sealed_ms INTEGER NOT NULL)''')
    if conn.execute('SELECT 1 FROM account_view_owner WHERE id=1').fetchone():
        return
    rows=conn.execute('SELECT payload,created_ms FROM push_subscriptions').fetchall()
    owner=None
    if len(rows)==1 and 0 < rows[0]['created_ms'] < now_ms and config.account_id.isdigit() and int(config.account_id)>0:
        try:
            owner=identity(json.loads(rows[0]['payload']))
        except Exception:
            pass  # Never print subscription material.
    conn.execute('INSERT INTO account_view_owner VALUES(1,?,?,?)',
        (owner,binding(config.account_id) if owner else None,now_ms))

def owner_exists(conn, config):
    row=conn.execute('SELECT device_hash,account_hash FROM account_view_owner WHERE id=1').fetchone()
    return bool(row and row['device_hash'] and row['account_hash']==binding(config.account_id))

def authorized_device(conn, config, supplied=None, device_hash=None):
    if not owner_exists(conn,config):
        raise HTTPException(403,'Registered owner device required')
    owner=conn.execute('SELECT device_hash FROM account_view_owner WHERE id=1').fetchone()[0]
    if supplied is not None:
        try:
            endpoint,_,_=push_security.subscription_identity(supplied)
            row=conn.execute('SELECT payload FROM push_subscriptions WHERE endpoint=?',(endpoint,)).fetchone()
            if not row:
                raise ValueError()
            stored=json.loads(row['payload'])
            push_security.prove_subscription(stored,supplied)
            actual=identity(stored)
        except Exception:
            raise HTTPException(403,'Registered owner device required') from None
    else:
        actual=device_hash
        # Deletion revokes access; no automatic replacement or re-enrollment.
        try:
            present=any(identity(json.loads(r[0]))==owner for r in conn.execute('SELECT payload FROM push_subscriptions'))
        except Exception:
            present=False
        if not present:
            raise HTTPException(403,'Registered owner device required')
    if not isinstance(actual,str) or not hmac.compare_digest(owner,actual):
        raise HTTPException(403,'Registered owner device required')
    return actual

class Sessions:
    def __init__(self, clock=time.monotonic):
        self.clock=clock
        self.items={}

    def create(self,device_hash,account_hash):
        now=self.clock()
        self.items={k:v for k,v in self.items.items() if v['expires']>now}
        if len(self.items)>=8:
            self.items.pop(next(iter(self.items)))
        token=secrets.token_urlsafe(32)
        self.items[token]={'device_hash':device_hash,'account_hash':account_hash,'expires':now+600,'cursors':{}}
        return {'view_token':token,'expires_in_seconds':600,'read_only':True}

    def authorize(self,conn,config,authorization):
        if not isinstance(authorization,str) or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}',authorization):
            raise HTTPException(401,'Account view authentication required')
        session=self.items.get(authorization[7:])
        if not session or session['expires']<=self.clock() or session['account_hash']!=binding(config.account_id):
            raise HTTPException(401,'Account view session expired')
        authorized_device(conn,config,device_hash=session['device_hash'])
        return session

    def page(self,session,kind,page,start_ms,end_ms):
        value={'items':copy.deepcopy(page['items']),'observed_ms':page['observed_ms'],
            'window_start_ms':start_ms,'window_end_ms':end_ms,'complete':not page['offset'],'next_cursor':None}
        if page['offset']:
            # Never put upstream cursors or private identifiers into URLs.
            if len(session['cursors'])>=20:
                session['cursors'].pop(next(iter(session['cursors'])))
            cursor=secrets.token_urlsafe(24)
            session['cursors'][cursor]={'kind':kind,'offset':page['offset'],'start':start_ms,'end':end_ms,
                'seen_ids':set(page['ids']),'seen_offsets':{page['offset']},'pages':1}
            value['next_cursor']=cursor
        return value

SESSIONS=Sessions()

SAFE_CODES={'DATA_UNAVAILABLE','INVALID_NUMBER','HISTORY_PAGE_INVALID','HISTORY_ID_INVALID','HISTORY_TIME_INVALID','HISTORY_COIN_MISMATCH','ACCOUNT_ROWS_INVALID','ACCOUNT_ROW_MISMATCH','TRANSPORT_READ_UNAVAILABLE'}

class AccountDataError(Exception):
    def __init__(self,code='DATA_UNAVAILABLE'):
        self.code=code if code in SAFE_CODES else 'DATA_UNAVAILABLE'
        super().__init__(self.code)

def number(value, *, required=False):
    if not required and (value is None or isinstance(value,str) and not value.strip()):
        return None
    try:
        if isinstance(value,bool) or value is None or len(str(value))>100:
            raise ValueError()
        n=Decimal(str(value))
        if not n.is_finite() or abs(n)>Decimal('1e50'):
            raise ValueError()
        return str(n)
    except (InvalidOperation,ValueError,TypeError):
        raise AccountDataError('INVALID_NUMBER') from None

def rows(raw,key,account_id, *, required=True):
    result=raw.get(key)
    if result is None and not required:
        return []
    if not isinstance(result,list) or len(result)>5000:
        raise AccountDataError('ACCOUNT_ROWS_INVALID')
    if any(not isinstance(r,dict) or str(r.get('accountId'))!=account_id for r in result):
        raise AccountDataError('ACCOUNT_ROW_MISMATCH')
    return result

def contract_name(cid,names):
    value=names.get(cid)
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}',value) else '銘柄名未確認'

def asset(raw,account_id,names,*,observed_ms):
    if not isinstance(raw,dict) or not isinstance(raw.get('account'),dict) or str(raw['account'].get('id'))!=account_id:
        raise AccountDataError()
    assets=rows(raw,'collateralAssetModelList',account_id)
    usdc=[r for r in assets if str(r.get('coinId'))=='1000']
    if len(usdc)!=1:
        raise AccountDataError()
    a=usdc[0]
    balance={k:number(a.get(field),required=k in {'equity_usdc','available_usdc'}) for k,field in {
        'equity_usdc':'totalEquity','available_usdc':'availableAmount','initial_margin_usdc':'initialMarginRequirement',
        'order_frozen_usdc':'orderFrozenAmount','pending_withdraw_usdc':'pendingWithdrawAmount','pending_transfer_usdc':'pendingTransferOutAmount'}.items()}
    cash=[r for r in rows(raw,'collateralList',account_id,required=False) if str(r.get('coinId'))=='1000']
    if len(cash)>1:
        raise AccountDataError()
    balance['cash_usdc']=number(cash[0].get('amount')) if cash else None
    balance['legacy_cash_usdc']=number(cash[0].get('legacyAmount')) if cash else None
    details={}
    for r in rows(raw,'positionAssetList',account_id,required=False):
        cid=str(r.get('contractId'))
        if not cid.isdigit() or int(cid)<=0 or cid in details:
            raise AccountDataError()
        details[cid]=r
    positions=[];seen=set()
    for r in rows(raw,'positionList',account_id):
        cid=str(r.get('contractId'))
        if not cid.isdigit() or int(cid)<=0 or cid in seen:
            raise AccountDataError()
        seen.add(cid)
        size=Decimal(number(r.get('openSize'),required=True))
        if not size:
            continue
        d=details.get(cid,{})
        positions.append(dict(ticker=contract_name(cid,names),direction='LONG' if size>0 else 'SHORT',
            quantity=str(abs(size)),entry_price=number(d.get('avgEntryPrice')),liquidation_price=number(d.get('liquidatePrice')),
            unrealized_pnl_usdc=number(d.get('unrealizePnl')),margin_usdc=number(d.get('initialMarginRequirement')),
            position_value_usdc=number(d.get('positionValue'))))
    pnl=[p['unrealized_pnl_usdc'] for p in positions]
    balance['unrealized_pnl_usdc']=str(sum((Decimal(v) for v in pnl),Decimal(0))) if all(v is not None for v in pnl) else None
    return {'source':'EDGEX_TRADING_ACCOUNT','read_only':True,'observed_ms':observed_ms,
        'balance':balance,'positions':positions,'positions_scope':'ALL_ACCOUNT_POSITIONS',
        'arc_wallet_included':False,'portfolio_roi_pct':None}

def history_page(raw,account_id,names,kind,*,observed_ms,start_ms,end_ms):
    if not isinstance(raw,dict) or not isinstance(raw.get('nextPageOffsetData'),str) or len(raw['nextPageOffsetData'])>8192:
        raise AccountDataError('HISTORY_PAGE_INVALID')
    records=rows(raw,'dataList',account_id)
    if len(records)>PAGE_SIZE:
        raise AccountDataError('HISTORY_PAGE_INVALID')
    items=[];ids=set()
    for r in records:
        rid=str(r.get('id'))
        if not rid.isdigit() or int(rid)<=0 or rid in ids:
            raise AccountDataError('HISTORY_ID_INVALID')
        ids.add(rid)
        try:
            created=int(r['createdTime'])
            if str(created)!=str(r['createdTime']) or not start_ms<=created<end_ms:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise AccountDataError('HISTORY_TIME_INVALID') from None
        type_value=r.get('type')
        type_value=type_value if isinstance(type_value,str) and re.fullmatch(r'[A-Z_]{1,64}',type_value) else 'UNRECOGNIZED'
        status=r.get('censorStatus')
        status=status if isinstance(status,str) and status in {'INIT','CENSOR_SUCCESS','CENSOR_FAILURE','L2_APPROVED','L2_REJECT','L2_REJECT_APPROVED'} else 'UNRECOGNIZED'
        item=dict(time_ms=created,type=type_value,status=status,record_key=hashlib.sha256((kind+':'+rid).encode()).hexdigest())
        if str(r.get('coinId'))!='1000':
            raise AccountDataError('HISTORY_COIN_MISMATCH')
        if kind=='positions':
            item.update(ticker=contract_name(str(r.get('contractId')),names),
                price=number(r.get('fillPrice')),open_quantity=number(r.get('fillOpenSize')),close_quantity=number(r.get('fillCloseSize')),
                realized_pnl_usdc=number(r.get('realizePnl')),open_fee_usdc=number(r.get('fillOpenFee')),
                close_fee_usdc=number(r.get('fillCloseFee')),funding_delta_usdc=number(r.get('deltaFundingFee')))
        else:
            if str(r.get('coinId'))!='1000':
                raise AccountDataError()
            item.update(delta_usdc=number(r.get('deltaAmount'),required=True),before_usdc=number(r.get('beforeAmount')))
        items.append(item)
    return dict(items=items,ids=ids,offset=raw['nextPageOffsetData'],observed_ms=observed_ms)

class Reader:
    def __init__(self,config,client=None):
        self.config=config
        if client is None:
            from edgex_sdk import Client
            client=Client(base_url=BASE_URL,account_id=int(config.account_id),api_key=config.api_key,
                api_secret=config.api_secret,api_passphrase=config.passphrase,timeout=5,
                trading_private_key='',wallet_private_key='')
        self.client=client
        self.names={};self.names_ms=0

    async def get(self,kind,params=None):
        if kind not in READ_PATHS:
            raise AccountDataError()
        try:
            return data(await asyncio.wait_for(self.client.async_client.make_authenticated_request(
                method='GET',path=READ_PATHS[kind],params=dict(params or {},accountId=self.config.account_id)),6))
        except Exception:
            raise AccountDataError('TRANSPORT_READ_UNAVAILABLE') from None

    async def metadata(self,now_ms):
        if self.names_ms and 0<=now_ms-self.names_ms<3600000:
            return
        try:
            raw=data(await asyncio.wait_for(self.client.get_metadata(),6))
            contracts=raw['contractList']
            if not isinstance(contracts,list):
                raise ValueError()
            self.names={str(r['contractId']):r['contractName'] for r in contracts}
            self.names_ms=now_ms
        except Exception:
            pass  # Positions remain known but names may be unknown; never guess.

    async def history(self,kind,start_ms,end_ms,offset=''):
        if kind not in {'positions','collateral'}:
            raise AccountDataError()
        observed=int(time.time()*1000)
        raw=await self.get(kind,dict(size=str(PAGE_SIZE),offsetData=offset,filterCoinIdList='1000',
            filterStartCreatedTimeInclusive=str(start_ms),filterEndCreatedTimeExclusive=str(end_ms)))
        return history_page(raw,self.config.account_id,self.names,kind,observed_ms=observed,start_ms=start_ms,end_ms=end_ms)

    async def conditional_orders(self):
        """Fresh, complete GET-only list; unknown fields never mean protection."""
        observed=int(time.time()*1000)
        result=[]; seen_ids=set(); seen_offsets=set(); offset=''
        types={'STOP_MARKET','STOP_LIMIT','TAKE_PROFIT_MARKET','TAKE_PROFIT_LIMIT'}
        for _ in range(20):
            raw=await self.get('orders',dict(size='200',offsetData=offset))
            if (not isinstance(raw,dict) or not isinstance(raw.get('dataList'),list)
                    or len(raw['dataList'])>200 or not isinstance(raw.get('nextPageOffsetData'),str)):
                raise AccountDataError()
            for o in raw['dataList']:
                if not isinstance(o,dict) or str(o.get('accountId'))!=self.config.account_id:
                    raise AccountDataError()
                oid=str(o.get('id',''))
                if not oid.isdigit() or int(oid)<=0 or oid in seen_ids:
                    raise AccountDataError()
                seen_ids.add(oid)
                if o.get('type') not in types:
                    continue
                if (o.get('side') not in {'BUY','SELL'} or o.get('status') not in {'UNTRIGGERED','OPEN','PENDING','CANCELING'}
                        or not str(o.get('contractId','')).isdigit() or int(o['contractId'])<=0):
                    raise AccountDataError()
                flags={k:o.get(k) for k in ('reduceOnly','isPositionTpsl')}
                if any(v is not None and type(v) is not bool for v in flags.values()):
                    raise AccountDataError()
                size=number(o.get('size'),required=True)
                trigger=number(o.get('triggerPrice'))
                if Decimal(size)<0 or trigger is not None and Decimal(trigger)<=0:
                    raise AccountDataError()
                result.append(dict(ticker=contract_name(str(o['contractId']),self.names),
                    kind='SL' if o['type'].startswith('STOP_') else 'TP',order_type=o['type'],
                    side=o['side'],status=o['status'],quantity=size,trigger_price=trigger,
                    reduce_only=flags['reduceOnly'],position_tpsl=flags['isPositionTpsl']))
            offset=raw['nextPageOffsetData']
            if not offset:
                return dict(source='EDGEX_ACTIVE_CONDITIONAL_ORDERS',read_only=True,complete=True,
                    observed_ms=observed,items=result,protection_guaranteed=False)
            if offset in seen_offsets:
                raise AccountDataError()
            seen_offsets.add(offset)
        raise AccountDataError()

    async def close(self):
        await self.client.close()

class Store:
    def __init__(self):
        self.asset=None;self.histories={};self.window=None;self.last_cycle_ms=None;self.errors={}
        self.reader=None

    async def collect(self,reader,*,now_ms):
        await reader.metadata(now_ms)
        async def load_asset():
            observed=int(time.time()*1000)
            try:
                return asset(await reader.get('asset'),reader.config.account_id,reader.names,observed_ms=observed)
            except Exception as exc:
                return exc.code if isinstance(exc,AccountDataError) else 'DATA_UNAVAILABLE'
        async def load_history(kind):
            try:
                return await reader.history(kind,now_ms-30*DAY,now_ms)
            except Exception as exc:
                return exc.code if isinstance(exc,AccountDataError) else 'DATA_UNAVAILABLE'
        values=await asyncio.gather(load_asset(),*(load_history(k) for k in ('positions','collateral')))
        # Atomic publication: never pair a new page with the preceding window.
        self.asset=values[0] if isinstance(values[0],dict) else None
        self.histories={k:v for k,v in zip(('positions','collateral'),values[1:]) if isinstance(v,dict)}
        self.errors={k:v if v in SAFE_CODES else 'DATA_UNAVAILABLE' for k,v in zip(('asset','positions','collateral'),values) if not isinstance(v,dict)}
        self.window=(now_ms-30*DAY,now_ms);self.last_cycle_ms=int(time.time()*1000)
        self.reader=reader

    def config_report(self,*,owner_bound,now_ms):
        age=(now_ms-self.asset['observed_ms'])/1000 if self.asset else None
        return dict(read_only=True,owner_device_bound=owner_bound,authentication='SEALED_EXISTING_DEVICE',
            source='EDGEX_TRADING_ACCOUNT',collection_state='AVAILABLE' if self.asset and not self.errors else 'PARTIAL' if self.asset else 'UNAVAILABLE' if self.last_cycle_ms else 'NOT_READY',
            asset_snapshot_age_seconds=age,last_cycle_ms=self.last_cycle_ms,
            validation_errors=dict(self.errors),sections_available={k:(self.asset is not None if k=='asset' else k in self.histories) for k in ('asset','positions','collateral')})

    def positions(self,*,now_ms):
        if self.asset is None or not 0<=now_ms-self.asset['observed_ms']<30000:
            raise HTTPException(503,'Fresh account data unavailable')
        result=copy.deepcopy(self.asset)
        result["snapshot_age_seconds"]=(now_ms-self.asset["observed_ms"])/1000
        return result

STORE=Store()
