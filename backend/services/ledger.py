"""
Single inventory-ledger write path (Phase 0 of the multi-user concurrency build).

Every inventory draw/return in the app MUST go through `allocate()` / `release()` here so
that under parallel edits two writers can't both pass a "remaining" check and then oversell.
The guarantee is provided by locking the AVAILABILITY LINE row `FOR UPDATE` before re-reading
remaining and before writing the ledger row — so concurrent transactions serialize on that row.

`order_line_allocations` (the ledger, created in migrations/ledger_step1.py) is the authoritative
record of what each order/cart-shelf draws from each Aster availability line. The old
`allocation_reservations` table is a PARALLEL accounting of the same thing and is being retired;
`remaining_for_line()` here reads the ledger only (reservations are NOT subtracted), so once a
path is converged onto this helper it no longer needs to touch reservations.

Design notes:
- These helpers take an OPEN cursor inside an existing `with get_db() as conn` transaction so the
  FOR UPDATE lock + the write + the caller's other updates all commit/rollback atomically. They do
  NOT open their own connection.
- `allocate()` raises AllocationError (caller maps to HTTP 400) when a line can't cover the qty.
- All quantities are integers >= 0 (the ledger CHECK enforces qty >= 0).
"""
from __future__ import annotations

from typing import Optional


class AllocationError(Exception):
    """Raised when a requested draw exceeds what the availability line has remaining."""
    def __init__(self, availability_line_id: str, need: int, have: int):
        self.availability_line_id = availability_line_id
        self.need = need
        self.have = have
        super().__init__(f"Insufficient availability on line {availability_line_id}: need {need}, have {have}")


def lock_lines(cur, availability_line_ids) -> None:
    """Acquire the row locks for a set of availability lines up-front, in a stable order.

    Locking every line a transaction will touch BEFORE doing any per-line check/allocate avoids
    lock-ordering deadlocks between two transactions that touch the same lines in different orders.
    Safe to call with an empty / None-containing iterable.
    """
    ids = sorted({str(x) for x in (availability_line_ids or []) if x})
    if not ids:
        return
    cur.execute("SELECT id FROM availability_lines WHERE id = ANY(%s::uuid[]) FOR UPDATE", (ids,))


def remaining_for_line(cur, availability_line_id: str) -> int:
    """Authoritative remaining for ONE availability line = units_available - SUM(ledger qty).

    Reads the ledger only (order_line_allocations). Call lock_lines() first if you intend to
    allocate against this line in the same transaction, or the value can go stale under a race.
    """
    cur.execute(
        """
        SELECT al.units_available
               - COALESCE((SELECT SUM(qty) FROM order_line_allocations
                           WHERE availability_line_id = al.id), 0) AS remaining
        FROM availability_lines al
        WHERE al.id = %s
        """,
        (availability_line_id,),
    )
    row = cur.fetchone()
    if not row:
        return 0
    return int(row["remaining"] or 0)


def remaining_for_lines(cur, availability_line_ids) -> dict:
    """Batch form of remaining_for_line — {availability_line_id(str): remaining(int)}."""
    ids = [str(x) for x in (availability_line_ids or []) if x]
    if not ids:
        return {}
    cur.execute(
        """
        SELECT al.id,
               al.units_available
               - COALESCE((SELECT SUM(qty) FROM order_line_allocations
                           WHERE availability_line_id = al.id), 0) AS remaining
        FROM availability_lines al
        WHERE al.id = ANY(%s::uuid[])
        """,
        (ids,),
    )
    return {str(r["id"]): int(r["remaining"] or 0) for r in cur.fetchall()}


def allocate(cur, *, availability_line_id: str, qty: int,
             order_line_id: Optional[str] = None,
             cart_shelf_id: Optional[str] = None,
             run_id: Optional[str] = None,
             batch_id: Optional[str] = None,
             sku_id: Optional[int] = None,
             source: str = "manual",
             lock: bool = True) -> str:
    """Draw `qty` units from an availability line, writing ONE ledger row. Returns the ledger id.

    Locks the availability line FOR UPDATE (unless lock=False because the caller already locked a
    batch of lines via lock_lines()), re-reads remaining from the ledger, and raises AllocationError
    if the line can't cover `qty`. This is the ONLY sanctioned way to add a draw.
    """
    qty = int(qty)
    if qty <= 0:
        raise ValueError("allocate() qty must be > 0")
    if lock:
        cur.execute("SELECT id FROM availability_lines WHERE id = %s FOR UPDATE", (availability_line_id,))
        if not cur.fetchone():
            raise AllocationError(availability_line_id, qty, 0)
    have = remaining_for_line(cur, availability_line_id)
    if have < qty:
        raise AllocationError(availability_line_id, qty, have)
    cur.execute(
        """
        INSERT INTO order_line_allocations
            (order_line_id, availability_line_id, cart_shelf_id, run_id, batch_id, sku_id, qty, source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (order_line_id, availability_line_id, cart_shelf_id, run_id, batch_id, sku_id, qty, source),
    )
    return str(cur.fetchone()["id"])


def release_by_shelf(cur, cart_shelf_id: str) -> int:
    """Return all units a given cart shelf drew (delete its ledger rows). Returns units released.

    Use when a shelf row is being deleted but you are NOT relying on the ON DELETE CASCADE from
    cart_shelves (e.g. you delete the shelf after). If you DELETE the cart_shelves row, the
    ledger's ON DELETE CASCADE already frees it — call this only when you need the count or when
    the shelf row itself is kept.
    """
    cur.execute(
        "DELETE FROM order_line_allocations WHERE cart_shelf_id = %s RETURNING qty",
        (cart_shelf_id,),
    )
    return sum(int(r["qty"] or 0) for r in cur.fetchall())


def release_by_shelves(cur, cart_shelf_ids) -> int:
    """Batch release for many cart shelves. Returns total units released."""
    ids = [str(x) for x in (cart_shelf_ids or []) if x]
    if not ids:
        return 0
    cur.execute(
        "DELETE FROM order_line_allocations WHERE cart_shelf_id = ANY(%s::uuid[]) RETURNING qty",
        (ids,),
    )
    return sum(int(r["qty"] or 0) for r in cur.fetchall())
