"""Private, read-only sizing from a fresh account snapshot; never an order quote."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext

from fastapi import HTTPException


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(503, "Account capital or risk policy unavailable") from None


def plan(req, snapshot, config):
    """Uses the engine's equity/notional budgets and assumed round-trip costs.

    Structural levels are unchanged. Tick/quote liquidity, Funding and actual fee
    tiers are unverified; this is a reference size, not execution eligibility.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        equity = number(snapshot["balance"].get("equity_usdc"))
        available = number(snapshot["balance"].get("available_usdc"))
        policy_risk, fee, slip = map(number, (config.risk_pct, config.fee_bps, config.slippage_bps))
        if equity <= 0 or available < 0 or not 0 < policy_risk <= 3 or not 0 < fee <= 100 or not 0 <= slip <= 25:
            raise HTTPException(503, "Account capital or risk policy unavailable")
        try:
            budget = min(equity * number(req.risk_pct) / 100, config.risk_budget(equity))
            limit = config.notional_limit(equity, available)
        except Exception:
            raise HTTPException(503, "Account capital or risk policy unavailable") from None
        if not budget.is_finite() or budget <= 0 or not limit.is_finite() or limit < 0:
            raise HTTPException(503, "Account capital or risk policy unavailable")
        entry, stop = number(req.entry), number(req.stop)
        if entry == stop:
            raise HTTPException(400, "Entry and stop must differ")
        side = "LONG" if stop < entry else "SHORT"
        sign = Decimal(1 if side == "LONG" else -1)
        target = number(req.target) if req.target is not None else None
        if target is not None and sign * (target-entry) <= 0:
            raise HTTPException(400, "Target must be beyond Entry in trade direction")
        fee /= 10000
        slip /= 10000
        adverse_entry = entry * (1 + sign*slip)
        adverse_stop = stop * (1 - sign*slip)
        risk_unit = abs(adverse_entry-adverse_stop) + (adverse_entry+adverse_stop)*fee
        theoretical = budget / risk_unit
        cap = limit / (adverse_entry*(1+fee))
        # A smaller manual leverage input may tighten, never expand the policy.
        if req.leverage is not None:
            cap = min(cap, available*number(req.leverage)/(adverse_entry*(1+fee)))
        size = min(theoretical, cap)
        max_capped = req.max_order_size is not None and size > number(req.max_order_size)
        if req.max_order_size is not None:
            size = min(size, number(req.max_order_size))
        if req.step_size is not None:
            step = number(req.step_size)
            size = (size/step).to_integral_value(rounding=ROUND_FLOOR)*step
        below_min = req.min_order_size is not None and size < number(req.min_order_size)
        if below_min:
            size = Decimal(0)
        reward_unit = None
        if target is not None:
            adverse_target = target*(1-sign*slip)
            reward_unit = sign*(adverse_target-adverse_entry)-(adverse_entry+adverse_target)*fee
        loss = size*risk_unit
        result = dict(side=side, capital_source="EDGEX_USDC_EQUITY", read_only=True,
            calculation_scope="ANALYSIS_REFERENCE_ONLY", equity_usdc=equity, available_usdc=available,
            risk_budget=budget, applied_risk_pct=budget/equity*100, theoretical_size=theoretical,
            size=size, notional=size*adverse_entry, max_loss=loss, actual_risk_pct=loss/equity*100,
            target_profit=size*reward_unit if reward_unit is not None else None,
            rr=abs(target-entry)/abs(entry-stop) if target is not None else None,
            cost_adjusted_rr=reward_unit/risk_unit if reward_unit is not None else None,
            max_order_capped=max_capped, margin_capped=cap < theoretical, below_min_order=below_min,
            min_order_size=req.min_order_size, notional_limit_usdc=limit,
            fee_bps=config.fee_bps, slippage_bps=config.slippage_bps,
            funding_included=False, quote_verified=False, loss_cap_guaranteed=False,
            observed_ms=snapshot["observed_ms"], snapshot_age_seconds=snapshot["snapshot_age_seconds"])
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in result.items()}
