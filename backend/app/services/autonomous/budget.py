"""Job-level hard budgets: reserve the worst case before a provider call, reconcile after.

Limits live in ``AutonomousNovelJob.budget`` (0 or missing = unlimited):
``max_calls``, ``max_input_tokens``, ``max_output_tokens``, ``max_total_tokens``,
``max_repair_calls``, ``max_cost_usd``, optional ``stage_limits: {stage:
{max_calls, max_total_tokens}}`` and ``chapter_limits: {max_calls_per_chapter,
max_total_tokens_per_chapter}``. Pricing comes from ``price_per_million:
{input, output}`` (job default) and ``prices: {llm_config_id: {input, output}}``
(per configuration, so a fallback model may cost differently).

Hard-limit contract:

- Every provider *attempt* (retry, schema repair, fallback) reserves before the
  network call: the estimated input tokens, the **full** output allowance it may
  consume (``max_tokens`` after clamping to what the limits still allow) and,
  when pricing is configured, the worst-case cost of both. A request whose
  worst case does not fit is refused before any network traffic.
- Reservation is one conditional UPDATE on the job row (compare-and-set on the
  reserved counters) plus a ``BudgetReservation`` ledger row in the same
  transaction, so concurrent workers cannot jointly slip under a ceiling and
  per-stage / per-chapter counts include in-flight attempts.
- ``reconcile`` charges actual usage and releases only what was genuinely
  unused. When the output of a failed attempt is unknown (timeout) the whole
  reserved output stays charged. When the provider reports no usage the
  estimates are charged and the call is counted in ``usage_estimated_calls``;
  when no price is known for the configuration the cost is counted in
  ``cost_unknown_calls`` and reported as unknown, never as zero.
- Ledger rows are ``open`` -> ``closed`` | ``abandoned``. Startup recovery and
  resume abandon the open rows of a dead worker and rebuild the job counters
  from the ledger, so a crash can never leave phantom reservations behind or
  release a reservation twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.core.config import settings
from app.db.models import AutonomousNovelJob, BudgetReservation
from app.services.autonomous import failures as fail

REPAIR_ROLES = ("repair_editor", "whole_novel_editor")
MIN_OUTPUT_TOKENS = 16
CAS_ATTEMPTS = 200
COST_PRECISION = 8


class BudgetExceeded(fail.StageFailure):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(fail.BUDGET_EXCEEDED, message, detail=detail)


class BudgetAccountingError(fail.StageFailure):
    """Reconciliation could not be persisted; the reservation stays open (conservative) until recovery."""

    def __init__(self, message: str):
        super().__init__(fail.INTERNAL_ERROR, message)


@dataclass
class Reservation:
    id: int
    job_id: int
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    role: str
    stage: str
    llm_config_id: Optional[int]
    released: bool = False
    dispatched: bool = False

    @property
    def max_output_tokens(self) -> int:
        """The provider ``max_tokens`` this attempt may use (already clamped to the limits)."""
        return self.output_tokens

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def effective_budget(job: AutonomousNovelJob) -> Dict[str, Any]:
    b = dict(job.budget or {})
    d = settings.autonomous
    b.setdefault("max_calls", d.default_max_calls)
    b.setdefault("max_total_tokens", d.default_max_total_tokens)
    b.setdefault("max_repair_calls", d.default_max_repair_calls)
    return b


def _limit(b: Dict[str, Any], key: str) -> int:
    try:
        return max(0, int(b.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def price_for(job: AutonomousNovelJob, llm_config_id: Optional[int]) -> Optional[Dict[str, float]]:
    """``{'input': usd_per_million, 'output': usd_per_million}`` for a configuration, or None when unknown."""
    b = job.budget or {}
    per_config = b.get("prices") or {}
    table = None
    if llm_config_id is not None and str(int(llm_config_id)) in per_config:
        table = per_config[str(int(llm_config_id))]
    elif b.get("price_per_million"):
        table = b.get("price_per_million")
    if not isinstance(table, dict) or not table:
        return None
    try:
        return {"input": float(table.get("input") or 0.0), "output": float(table.get("output") or 0.0)}
    except (TypeError, ValueError):
        return None


def cost_of(price: Optional[Dict[str, float]], input_tokens: int, output_tokens: int) -> Optional[float]:
    if price is None:
        return None
    return round(max(0, int(input_tokens)) / 1e6 * price["input"] + max(0, int(output_tokens)) / 1e6 * price["output"], COST_PRECISION)


def estimate_cost(job: AutonomousNovelJob) -> Optional[float]:
    """Known accumulated cost, or None when any charged call had no price (never a false zero)."""
    if int(getattr(job, "cost_unknown_calls", 0) or 0) > 0:
        return None
    if price_for(job, None) is None and not (job.budget or {}).get("prices"):
        return None
    return round(float(job.cost_usd or 0.0), COST_PRECISION)


def usage_snapshot(session: Session, job: AutonomousNovelJob) -> Dict[str, Any]:
    b = effective_budget(job)
    used_in, used_out = int(job.input_tokens), int(job.output_tokens)
    cost = estimate_cost(job)
    return {
        "calls": {"used": int(job.model_calls), "reserved": int(job.reserved_calls), "limit": _limit(b, "max_calls")},
        "input_tokens": {"used": used_in, "reserved": int(job.reserved_input_tokens), "limit": _limit(b, "max_input_tokens")},
        "output_tokens": {"used": used_out, "reserved": int(job.reserved_output_tokens), "limit": _limit(b, "max_output_tokens")},
        "total_tokens": {"used": used_in + used_out, "reserved": int(job.reserved_tokens), "limit": _limit(b, "max_total_tokens")},
        "repair_calls": {"used": int(job.repair_calls), "reserved": int(getattr(job, "reserved_repair_calls", 0) or 0), "limit": _limit(b, "max_repair_calls")},
        "cost_usd": {"known": cost, "reserved": round(float(job.reserved_cost_usd or 0.0), COST_PRECISION), "limit": float(b.get("max_cost_usd") or 0.0), "unknown_calls": int(job.cost_unknown_calls), "status": "unknown" if cost is None else ("estimated" if int(job.usage_estimated_calls) > 0 else "reported")},
        "usage_estimated_calls": int(job.usage_estimated_calls),
        # Backward compatible alias: None means unknown, never zero.
        "estimated_cost_usd": cost,
    }


def _stage_key(stage: str) -> str:
    return (stage or "").split(":")[0]


def _ledger_totals(session: Session, job_id: int, *, stage_key: Optional[str] = None, stage: Optional[str] = None) -> Dict[str, int]:
    """Calls and tokens already used *or in flight* for a stage (or exact chapter stage), from the ledger."""
    q = select(func.count(BudgetReservation.id), func.coalesce(func.sum(BudgetReservation.charged_input_tokens + BudgetReservation.charged_output_tokens), 0), func.coalesce(func.sum(BudgetReservation.reserved_input_tokens + BudgetReservation.reserved_output_tokens), 0)).where(BudgetReservation.job_id == job_id, BudgetReservation.status.not_in(["abandoned", "released"]))
    if stage is not None:
        q = q.where(BudgetReservation.stage == stage)
    elif stage_key is not None:
        q = q.where(BudgetReservation.stage_key == stage_key)
    count, charged, reserved_open = session.exec(q).one()
    open_q = select(func.coalesce(func.sum(BudgetReservation.reserved_input_tokens + BudgetReservation.reserved_output_tokens), 0)).where(BudgetReservation.job_id == job_id, BudgetReservation.status.in_(["open", "dispatched"]))
    if stage is not None:
        open_q = open_q.where(BudgetReservation.stage == stage)
    elif stage_key is not None:
        open_q = open_q.where(BudgetReservation.stage_key == stage_key)
    open_tokens = session.exec(open_q).one()
    return {"calls": int(count or 0), "tokens": int(charged or 0) + int(open_tokens or 0)}


def reserve(session: Session, job_id: int, *, role: str, stage: str, estimated_input_tokens: int, requested_output_tokens: int, llm_config_id: Optional[int] = None, min_output_tokens: int = MIN_OUTPUT_TOKENS) -> Reservation:
    """Atomically reserve one attempt's worst case. Clamps the output allowance; raises ``BudgetExceeded`` when nothing viable fits.

    ``requested_output_tokens`` is the provider ``max_tokens`` the caller wants;
    the returned ``Reservation.max_output_tokens`` is what it must actually send.
    """
    est_in = max(0, int(estimated_input_tokens))
    want_out = max(0, int(requested_output_tokens))
    min_out = max(1, int(min_output_tokens))
    for _ in range(CAS_ATTEMPTS):
        job = session.get(AutonomousNovelJob, job_id)
        if job is None:
            raise fail.StageFailure(fail.INTERNAL_ERROR, f"job {job_id} not found")
        session.refresh(job)
        b = effective_budget(job)
        problems: List[str] = []
        used_in, used_out = int(job.input_tokens), int(job.output_tokens)
        res_in, res_out, res_calls = int(job.reserved_input_tokens), int(job.reserved_output_tokens), int(job.reserved_calls)
        res_repair = int(getattr(job, "reserved_repair_calls", 0) or 0)
        is_repair = 1 if role in REPAIR_ROLES else 0

        max_calls = _limit(b, "max_calls")
        if max_calls and job.model_calls + res_calls + 1 > max_calls:
            problems.append(f"max_calls {max_calls} reached ({job.model_calls} used, {res_calls} reserved)")
        max_repair = _limit(b, "max_repair_calls")
        if max_repair and is_repair and job.repair_calls + res_repair + 1 > max_repair:
            problems.append(f"max_repair_calls {max_repair} reached ({job.repair_calls} used, {res_repair} reserved)")

        # Output allowance: the smallest remaining headroom across every token limit.
        allow_out = want_out
        max_in = _limit(b, "max_input_tokens")
        if max_in and used_in + res_in + est_in > max_in:
            problems.append(f"max_input_tokens {max_in} would be exceeded ({used_in} used, {res_in} reserved, {est_in} requested)")
        max_out = _limit(b, "max_output_tokens")
        if max_out:
            allow_out = min(allow_out, max_out - used_out - res_out)
        max_total = _limit(b, "max_total_tokens")
        if max_total:
            allow_out = min(allow_out, max_total - used_in - used_out - res_in - res_out - est_in)
        stage_key = _stage_key(stage)
        stage_limits = (b.get("stage_limits") or {}).get(stage_key) or {}
        chapter_limits = b.get("chapter_limits") or {}
        stage_totals = _ledger_totals(session, job_id, stage_key=stage_key) if stage_limits else None
        if stage_limits:
            s_calls = _limit(stage_limits, "max_calls")
            if s_calls and stage_totals["calls"] + 1 > s_calls:
                problems.append(f"stage {stage_key} max_calls {s_calls} reached")
            s_tokens = _limit(stage_limits, "max_total_tokens")
            if s_tokens:
                allow_out = min(allow_out, s_tokens - stage_totals["tokens"] - est_in)
        if chapter_limits and ":ch" in stage:
            ch_totals = _ledger_totals(session, job_id, stage=stage)
            c_calls = _limit(chapter_limits, "max_calls_per_chapter")
            if c_calls and ch_totals["calls"] + 1 > c_calls:
                problems.append(f"chapter limit {c_calls} calls reached for {stage}")
            c_tokens = _limit(chapter_limits, "max_total_tokens_per_chapter")
            if c_tokens:
                allow_out = min(allow_out, c_tokens - ch_totals["tokens"] - est_in)

        max_cost = float(b.get("max_cost_usd") or 0.0)
        price = price_for(job, llm_config_id)
        reserve_cost = 0.0
        if max_cost:
            if price is None:
                problems.append(f"max_cost_usd {max_cost} is set but no price table is configured for LLM configuration {llm_config_id}; refusing to spend unpriced tokens")
            else:
                if job.cost_unknown_calls:
                    problems.append("max_cost_usd cannot be enforced: earlier calls had unknown cost")
                remaining_cost = max_cost - float(job.cost_usd or 0.0) - float(job.reserved_cost_usd or 0.0)
                input_cost = est_in / 1e6 * price["input"]
                if price["output"] > 0:
                    allow_out = min(allow_out, int(math.floor((remaining_cost - input_cost) * 1e6 / price["output"])))
                elif remaining_cost - input_cost < 0:
                    allow_out = -1
        if allow_out < min_out and not problems:
            problems.append(f"remaining token/cost allowance leaves {max(0, allow_out)} output tokens (< {min_out} minimum) for {stage}")
        if problems:
            raise BudgetExceeded("Budget exhausted: " + "; ".join(problems), detail={"usage": usage_snapshot(session, job), "role": role, "stage": stage, "llm_config_id": llm_config_id})
        allow_out = int(allow_out)
        if price is not None:
            reserve_cost = cost_of(price, est_in, allow_out) or 0.0

        t = AutonomousNovelJob.__table__
        stmt = (
            update(t)
            .where(t.c.id == job_id, t.c.reserved_calls == res_calls, t.c.reserved_repair_calls == res_repair, t.c.reserved_input_tokens == res_in, t.c.reserved_output_tokens == res_out, t.c.model_calls == job.model_calls, t.c.input_tokens == used_in, t.c.output_tokens == used_out)
            .values(reserved_calls=t.c.reserved_calls + 1, reserved_repair_calls=t.c.reserved_repair_calls + is_repair, reserved_input_tokens=t.c.reserved_input_tokens + est_in, reserved_output_tokens=t.c.reserved_output_tokens + allow_out, reserved_tokens=t.c.reserved_tokens + est_in + allow_out, reserved_cost_usd=t.c.reserved_cost_usd + reserve_cost)
        )
        if session.execute(stmt).rowcount != 1:
            session.rollback()
            continue  # a concurrent reservation moved the counters: re-evaluate against the new state
        row = BudgetReservation(job_id=job_id, role=role, stage=stage, stage_key=stage_key, llm_config_id=llm_config_id, reserved_input_tokens=est_in, reserved_output_tokens=allow_out, reserved_cost_usd=reserve_cost, status="open")
        session.add(row)
        session.commit()
        session.refresh(row)
        return Reservation(id=int(row.id), job_id=job_id, calls=1, input_tokens=est_in, output_tokens=allow_out, cost_usd=reserve_cost, role=role, stage=stage, llm_config_id=llm_config_id, dispatched=False)
    raise fail.StageFailure(fail.INTERNAL_ERROR, f"budget reservation for job {job_id} kept losing the compare-and-set race")


def mark_dispatched(session: Session, res: Reservation) -> None:
    """Durably mark a reservation as dispatched before provider network I/O begins.

    Idempotent. Raises BudgetAccountingError if reservation is not found or cannot transition.
    """
    if res.dispatched:
        return
    row = session.get(BudgetReservation, res.id)
    if row is None:
        raise BudgetAccountingError(f"reservation {res.id} not found")
    if row.status == "dispatched":
        res.dispatched = True
        return
    if row.status != "open":
        raise BudgetAccountingError(f"cannot dispatch reservation {res.id} in state '{row.status}'")
    row.status = "dispatched"
    row.dispatched_at = datetime.now()
    session.add(row)
    session.commit()
    res.dispatched = True


def release(session: Session, res: Reservation) -> None:
    """Safely release a reservation that was NEVER dispatched."""
    if res.released:
        return
    row = session.get(BudgetReservation, res.id)
    if row is None or row.status != "open":
        res.released = True
        return
    job = session.get(AutonomousNovelJob, res.job_id)
    if job is None:
        raise BudgetAccountingError(f"job {res.job_id} disappeared during release")
    session.refresh(job)
    is_repair = 1 if res.role in REPAIR_ROLES else 0
    t = AutonomousNovelJob.__table__
    values: Dict[str, Any] = {
        "reserved_calls": t.c.reserved_calls - res.calls,
        "reserved_repair_calls": t.c.reserved_repair_calls - is_repair,
        "reserved_input_tokens": t.c.reserved_input_tokens - res.input_tokens,
        "reserved_output_tokens": t.c.reserved_output_tokens - res.output_tokens,
        "reserved_tokens": t.c.reserved_tokens - res.input_tokens - res.output_tokens,
        "reserved_cost_usd": t.c.reserved_cost_usd - res.cost_usd,
    }
    try:
        session.execute(update(t).where(t.c.id == res.job_id).values(**values))
        row.status = "released"
        row.closed_at = datetime.now()
        session.add(row)
        _clamp_non_negative(session, res.job_id)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise BudgetAccountingError(f"budget release failed for job {res.job_id}: {type(exc).__name__}") from exc
    res.released = True


def reconcile(session: Session, res: Reservation, *, input_tokens: int, output_tokens: int, succeeded: bool, output_known: bool = True, usage_reported: bool = True, price: Optional[Dict[str, float]] = None) -> None:
    """Charge actual usage and release the unused part of ``res``. Idempotent per reservation.

    ``output_known=False`` (timeout, connection dropped mid-stream) charges the
    full reserved output because the provider may have generated all of it.
    ``usage_reported=False`` marks the call as estimated in the job counters.
    """
    if res.released:
        return
    row = session.get(BudgetReservation, res.id)
    if row is None or row.status not in ("open", "dispatched"):
        res.released = True  # already closed or transitioned by recovery
        return
    job = session.get(AutonomousNovelJob, res.job_id)
    if job is None:
        raise BudgetAccountingError(f"job {res.job_id} disappeared during reconcile")
    session.refresh(job)
    a_in = max(0, int(input_tokens))
    a_out = max(0, int(output_tokens)) if output_known else max(max(0, int(output_tokens)), res.output_tokens)
    table = price if price is not None else price_for(job, res.llm_config_id)
    actual_cost = cost_of(table, a_in, a_out)
    is_repair = 1 if res.role in REPAIR_ROLES else 0
    t = AutonomousNovelJob.__table__
    values: Dict[str, Any] = {
        "reserved_calls": t.c.reserved_calls - res.calls,
        "reserved_repair_calls": t.c.reserved_repair_calls - is_repair,
        "reserved_input_tokens": t.c.reserved_input_tokens - res.input_tokens,
        "reserved_output_tokens": t.c.reserved_output_tokens - res.output_tokens,
        "reserved_tokens": t.c.reserved_tokens - res.input_tokens - res.output_tokens,
        "reserved_cost_usd": t.c.reserved_cost_usd - res.cost_usd,
        "model_calls": t.c.model_calls + 1,
        "input_tokens": t.c.input_tokens + a_in,
        "output_tokens": t.c.output_tokens + a_out,
    }
    if actual_cost is None:
        values["cost_unknown_calls"] = t.c.cost_unknown_calls + 1
    else:
        values["cost_usd"] = t.c.cost_usd + actual_cost
    if not usage_reported:
        values["usage_estimated_calls"] = t.c.usage_estimated_calls + 1
    if is_repair:
        values["repair_calls"] = t.c.repair_calls + 1
    try:
        session.execute(update(t).where(t.c.id == res.job_id).values(**values))
        row.status = "closed"
        row.charged_input_tokens = a_in
        row.charged_output_tokens = a_out
        row.charged_cost_usd = actual_cost
        row.succeeded = bool(succeeded)
        row.usage_reported = bool(usage_reported)
        row.closed_at = datetime.now()
        session.add(row)
        _clamp_non_negative(session, res.job_id)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed accounting failure, never swallowed
        session.rollback()
        raise BudgetAccountingError(f"budget reconcile failed for job {res.job_id}: {type(exc).__name__}") from exc
    res.released = True


def _clamp_non_negative(session: Session, job_id: int) -> None:
    t = AutonomousNovelJob.__table__
    for col in (t.c.reserved_calls, t.c.reserved_repair_calls, t.c.reserved_input_tokens, t.c.reserved_output_tokens, t.c.reserved_tokens):
        session.execute(update(t).where(t.c.id == job_id, col < 0).values({col.name: 0}))
    session.execute(update(t).where(t.c.id == job_id, t.c.reserved_cost_usd < 0).values(reserved_cost_usd=0.0))


def rebuild_reserved_counters(session: Session, job_id: int) -> Dict[str, Any]:
    """Recompute the job's reserved counters from the open/dispatched ledger rows (deterministic repair)."""
    rows = session.exec(select(BudgetReservation).where(BudgetReservation.job_id == job_id, BudgetReservation.status.in_(["open", "dispatched"]))).all()
    repair_count = sum(1 for r in rows if r.role in REPAIR_ROLES)
    values = {
        "reserved_calls": len(rows),
        "reserved_repair_calls": repair_count,
        "reserved_input_tokens": sum(int(r.reserved_input_tokens) for r in rows),
        "reserved_output_tokens": sum(int(r.reserved_output_tokens) for r in rows),
        "reserved_cost_usd": round(sum(float(r.reserved_cost_usd or 0.0) for r in rows), COST_PRECISION),
    }
    values["reserved_tokens"] = values["reserved_input_tokens"] + values["reserved_output_tokens"]
    t = AutonomousNovelJob.__table__
    session.execute(update(t).where(t.c.id == job_id).values(**values))
    session.commit()
    return values


def clear_reservations(session: Session, job_id: int) -> int:
    """Startup recovery / resume:

    - Not-dispatched reservations (status == 'open') are safely released.
    - Dispatched reservations (status == 'dispatched') whose outcome is uncertain
      are conservatively charged (1 call, full reserved output tokens, reserved input tokens,
      reserved cost or unknown-cost marker, and repair_calls if applicable) and transitioned
      to 'uncertain_charged'.

    Returns the total number of cleared reservations (released + uncertain_charged).
    """
    job = session.get(AutonomousNovelJob, job_id)
    if job is None:
        return 0
    session.refresh(job)

    rows = session.exec(select(BudgetReservation).where(BudgetReservation.job_id == job_id, BudgetReservation.status.in_(["open", "dispatched"]))).all()
    if not rows:
        rebuild_reserved_counters(session, job_id)
        return 0

    now = datetime.now()
    t = AutonomousNovelJob.__table__

    not_dispatched = [r for r in rows if r.status == "open"]
    dispatched = [r for r in rows if r.status == "dispatched"]

    for r in not_dispatched:
        r.status = "released"
        r.closed_at = now
        session.add(r)

    for r in dispatched:
        r.status = "uncertain_charged"
        r.closed_at = now
        r.charged_input_tokens = int(r.reserved_input_tokens)
        r.charged_output_tokens = int(r.reserved_output_tokens)
        r.usage_reported = False
        r.succeeded = False

        table = price_for(job, r.llm_config_id)
        if table is not None or float(r.reserved_cost_usd or 0.0) > 0:
            r.charged_cost_usd = round(float(r.reserved_cost_usd or 0.0), COST_PRECISION)
            cost_val = r.charged_cost_usd
            cost_unknown = 0
        else:
            r.charged_cost_usd = None
            cost_val = 0.0
            cost_unknown = 1

        is_repair = 1 if r.role in REPAIR_ROLES else 0

        upd_vals: Dict[str, Any] = {
            "model_calls": t.c.model_calls + 1,
            "input_tokens": t.c.input_tokens + r.charged_input_tokens,
            "output_tokens": t.c.output_tokens + r.charged_output_tokens,
            "usage_estimated_calls": t.c.usage_estimated_calls + 1,
        }
        if cost_unknown:
            upd_vals["cost_unknown_calls"] = t.c.cost_unknown_calls + 1
        elif cost_val > 0:
            upd_vals["cost_usd"] = t.c.cost_usd + cost_val

        if is_repair:
            upd_vals["repair_calls"] = t.c.repair_calls + 1

        session.execute(update(t).where(t.c.id == job_id).values(**upd_vals))
        session.add(r)

    session.commit()
    rebuild_reserved_counters(session, job_id)
    _clamp_non_negative(session, job_id)
    session.commit()
    return len(rows)


def validate_budget_spec(spec: Dict[str, Any]) -> List[str]:
    """User-facing validation of a job budget: cost caps need a price table."""
    problems: List[str] = []
    if float(spec.get("max_cost_usd") or 0) > 0 and not (spec.get("price_per_million") or spec.get("prices")):
        problems.append("max_cost_usd requires price_per_million (or prices per LLM configuration) so the cap can be enforced before each call")
    for key, table in list((spec.get("prices") or {}).items()) + ([("price_per_million", spec.get("price_per_million"))] if spec.get("price_per_million") else []):
        if not isinstance(table, dict) or any(float(table.get(k) or 0) < 0 for k in ("input", "output")):
            problems.append(f"price table '{key}' must contain non-negative 'input' and 'output' USD per million tokens")
    return problems


__all__ = ["BudgetAccountingError", "BudgetExceeded", "MIN_OUTPUT_TOKENS", "REPAIR_ROLES", "Reservation", "clear_reservations", "cost_of", "effective_budget", "estimate_cost", "mark_dispatched", "price_for", "rebuild_reserved_counters", "reconcile", "release", "reserve", "usage_snapshot", "validate_budget_spec"]
