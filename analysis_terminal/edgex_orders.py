"""Restricted EdgeX V2 trading adapter. No withdrawal/transfer capability.

Pinned official SDK source handles HMAC and EIP-712; no credentials in SQLite.
Only known official paths are used. Mutating requests are never retried.
"""

from decimal import Decimal, InvalidOperation
import asyncio
import time

BASE_URL = "https://edgex-prod-v2.edgex.exchange"


class ExecutionError(Exception):
    """Safe machine code only; never include an exchange response or exception."""


def decimal(value):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ExecutionError("INVALID_DECIMAL") from None


def data(response):
    if not isinstance(response, dict) or response.get("code") != "SUCCESS":
        raise ExecutionError("EXCHANGE_REJECTED")
    return response.get("data")


class EdgeXOrders:
    def __init__(self, config, *, client=None):
        self.config = config
        if client is None:
            from edgex_sdk import Client

            client = Client(
                base_url=BASE_URL,
                account_id=int(config.account_id),
                api_key=config.api_key,
                api_secret=config.api_secret,
                api_passphrase=config.passphrase,
                trading_private_key=config.signer_key,
                timeout=5,
            )
        self.client = client

    async def call(self, operation, *args, **kwargs):
        try:
            return data(await asyncio.wait_for(operation(*args, **kwargs), timeout=6))
        except ExecutionError:
            raise
        except Exception:
            raise ExecutionError("TRANSPORT_OR_SIGNING_UNKNOWN") from None

    async def close(self):
        await self.client.close()

    async def metadata(self):
        self.client._metadata_cache = None
        return await self.call(self.client.get_metadata)

    async def account(self):
        started = int(time.time() * 1000)
        raw = await self.call(self.client.get_account_asset)
        if (
            not isinstance(raw, dict)
            or str((raw.get("account") or {}).get("id")) != self.config.account_id
        ):
            raise ExecutionError("ACCOUNT_ID_MISMATCH")
        # Reject absent position lists; missing is never interpreted as flat.
        if not isinstance(raw.get("positionList"), list) or not isinstance(
            raw.get("collateralAssetModelList"), list
        ):
            raise ExecutionError("INCOMPLETE_ACCOUNT")
        assets = [
            x for x in raw["collateralAssetModelList"] if str(x.get("coinId")) == "1000"
        ]
        if (
            len(assets) != 1
            or str(assets[0].get("accountId")) != self.config.account_id
        ):
            raise ExecutionError("COLLATERAL_MISMATCH")
        equity, available = decimal(assets[0].get("totalEquity")), decimal(
            assets[0].get("availableAmount")
        )
        if equity <= 0 or available < 0:
            raise ExecutionError("INVALID_ACCOUNT_BALANCE")
        positions = {}
        for p in raw["positionList"]:
            if str(p.get("accountId")) != self.config.account_id:
                raise ExecutionError("ACCOUNT_ID_MISMATCH")
            cid, size = str(p.get("contractId")), decimal(p.get("openSize"))
            if cid in positions or not cid.isdigit():
                raise ExecutionError("AMBIGUOUS_POSITION")
            positions[cid] = size
        return dict(
            equity=equity, available=available, positions=positions, observed_ms=started
        )

    async def active_orders(self):
        from edgex_sdk.order.types import GetActiveOrderParams

        result, seen, offset = [], set(), ""
        for _ in range(20):
            page = await self.call(
                self.client.get_active_orders,
                GetActiveOrderParams(size="200", offset_data=offset),
            )
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("dataList"), list)
                or "nextPageOffsetData" not in page
            ):
                raise ExecutionError("INCOMPLETE_ORDER_PAGE")
            result.extend(page["dataList"])
            offset = page["nextPageOffsetData"]
            if not isinstance(offset, str):
                raise ExecutionError("INCOMPLETE_ORDER_PAGE")
            if not offset:
                for order in result:
                    if str(order.get("accountId")) != self.config.account_id:
                        raise ExecutionError("ACCOUNT_ID_MISMATCH")
                return result
            if offset in seen:
                raise ExecutionError("ORDER_PAGINATION_LOOP")
            seen.add(offset)
        raise ExecutionError("ORDER_PAGE_LIMIT")

    async def order(self, client_id):
        orders = await self.call(
            self.client.async_client.make_authenticated_request,
            method="GET",
            path="/api/v2/private/order/getOrderByClientOrderId",
            params={
                "accountId": self.config.account_id,
                "clientOrderIdList": client_id,
            },
        )
        if not isinstance(orders, list):
            raise ExecutionError("INCOMPLETE_ORDER_QUERY")
        if not orders:
            return None
        if (
            len(orders) != 1
            or orders[0].get("clientOrderId") != client_id
            or str(orders[0].get("accountId")) != self.config.account_id
        ):
            raise ExecutionError("ORDER_ID_MISMATCH")
        return orders[0]

    async def quote(self, contract_id, price):
        raw = await self.call(
            self.client.get_max_order_size, contract_id, decimal(price)
        )
        if not isinstance(raw, dict):
            raise ExecutionError("INCOMPLETE_QUOTE")
        q = {
            k: decimal(raw.get(k))
            for k in ("maxBuySize", "maxSellSize", "ask1Price", "bid1Price")
        }
        if q["ask1Price"] < q["bid1Price"] or min(q.values()) <= 0:
            raise ExecutionError("INVALID_QUOTE")
        return q

    async def create(self, intent, kind, *, quantity=None):
        if self.config.mode != "LIVE" or self.config.errors():
            raise ExecutionError("LIVE_CONFIGURATION_REQUIRED")
        from edgex_sdk.order.types import CreateOrderParams

        entry_side = "BUY" if intent["side"] == "LONG" else "SELL"
        entry = kind == "entry"
        params = dict(
            contract_id=intent["contract_id"],
            size=quantity or intent["quantity"],
            side=entry_side if entry else ("SELL" if entry_side == "BUY" else "BUY"),
            client_order_id=intent[kind + "_client_id"],
            reduce_only=not entry,
        )
        if entry:
            params.update(
                type="LIMIT",
                price=intent["limit_price"],
                time_in_force="IMMEDIATE_OR_CANCEL",
                expire_time=intent["entry_deadline_ms"],
            )
        elif kind in ("sl", "tp"):
            params.update(
                type="STOP_MARKET" if kind == "sl" else "TAKE_PROFIT_MARKET",
                price="0",
                trigger_price=intent["stop_price" if kind == "sl" else "target_price"],
                trigger_price_type="LAST_PRICE",
                time_in_force="IMMEDIATE_OR_CANCEL",
                expire_time=intent["protection_deadline_ms"],
                is_position_tpsl=True,
            )
        elif kind == "close":
            params.update(type="MARKET", price="0", time_in_force="IMMEDIATE_OR_CANCEL")
        else:
            raise ExecutionError("INVALID_ORDER_KIND")
        answer = await self.call(self.client.create_order, CreateOrderParams(**params))
        if not isinstance(answer, dict) or not str(answer.get("orderId", "")).isdigit():
            raise ExecutionError("CREATE_ACK_UNKNOWN")
        return str(answer["orderId"])

    async def cancel(self, client_id):
        if self.config.mode != "LIVE" or self.config.errors():
            raise ExecutionError("LIVE_CONFIGURATION_REQUIRED")
        from edgex_sdk.order.types import CancelOrderParams

        # Cancellation acknowledgement alone is not proof of no fills.
        return await self.call(
            self.client.cancel_order, CancelOrderParams(client_order_id=client_id)
        )
