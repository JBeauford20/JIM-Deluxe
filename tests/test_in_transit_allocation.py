"""Unit tests for the in-transit allocator (pure function, no DB, no workbooks).

Run:  python -m pytest tests/test_in_transit_allocation.py -q
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "ingest"))
from load_in_transit_lines import allocate  # noqa: E402

D1, D2 = date(2026, 11, 17), date(2026, 11, 20)


def row(route, kit, kq, sku, total, var=1, notes=None, day=D1, desc="X"):
    return {"ShipDate": day, "Route": route, "KitDescription": kit, "KitQuantity": kq,
            "Variation": var, "Notes": notes, "ItemDescription": desc, "SKU": sku,
            "Eaches": total / kq, "Total Used": total}


def total(lines):
    return sum(v[0] for v in lines.values())


def test_exact_allocation_conserves_units():
    compiled = [(1, "R1", date(2026, 11, 18)), (2, "R1", date(2026, 11, 18))]
    sc = {1: {"Holiday C": 1}, 2: {"Holiday C": 2}}
    pack = [row("R1", "Holiday C", 3, 111, 300), row("R1", "Holiday C", 3, 222, 30)]
    lines, rep, unalloc, *_ = allocate(compiled, sc, pack)
    assert lines[(1, 111, "R1")][0] == 100 and lines[(2, 111, "R1")][0] == 200
    assert lines[(2, 222, "R1")][2] == "exact"
    assert lines[(1, 111, "R1")][1] == date(2026, 11, 18)
    assert total(lines) == 330 and not unalloc


def test_mix_rows_do_not_inflate_cart_count():
    compiled = [(1, "R1", D1), (2, "R1", D1)]
    sc = {1: {"BM 2": 1}, 2: {"BM 2": 1}}
    pack = [row("R1", "BM 2", 2, 111, 72),
            row("R1", "BM 2", 1, 222, 6, notes="Mix A: 1 of 2 carts"),
            row("R1", "BM 2", 1, 333, 6, notes="Mix A: 1 of 2 carts")]
    lines, rep, *_ = allocate(compiled, sc, pack)
    assert all(v[2] == "exact" for v in lines.values())
    assert total(lines) == 84


def test_unmatched_config_pools_on_route_then_day():
    compiled = [(1, "N1", D1), (2, "N1", D1), (3, "N8", D2), (4, "N8", D2)]
    sc = {1: {"Holiday B": 1}, 2: {"Holiday C": 1}, 3: {"Holiday D": 3}, 4: {"Holiday D": 1}}
    pack = [row("N1", "Holiday AS", 2, 111, 200),                # not in S&C -> route-pool
            row("N8", "Holiday D", 1, 111, 50, day=D2),           # 1 of 4 S&C carts shipped
            row("N9", "BM 6", 3, 999, 300, day=D2)]               # route with no stores -> day-pool
    lines, rep, unalloc, *_ = allocate(compiled, sc, pack)
    assert lines[(1, 111, "N1")][0] == 100 and lines[(1, 111, "N1")][2] == "route-pool"
    assert lines[(3, 999, "N8")][0] == 225 and lines[(4, 999, "N8")][0] == 75
    assert lines[(3, 999, "N8")][2] == "day-pool"
    assert abs(total(lines) - 550) < 1e-9 and not unalloc


def test_store_specific_kit_and_split_stop():
    compiled = [(156, "S4", D1), (7, "S4", D1), (4180, "N2", D1), (4180, "N3", D2), (5, "N3", D2)]
    sc = {156: {"Holiday C": 1}, 7: {"Holiday C": 1}, 4180: {"BM 2": 1}, 5: {"BM 2": 1}}
    pack = [row("S4", "Holiday C", 1, 111, 100),
            row("S4", "Holiday C - BM (Store 0156)", 1, 222, 40),
            row("N3", "BM 2", 2, 333, 60, day=D2)]
    lines, rep, unalloc, *_ = allocate(compiled, sc, pack)
    assert lines[(7, 111, "S4")][0] == 100 and (156, 111, "S4") not in lines
    assert lines[(156, 222, "S4")][2] == "store"
    assert lines[(4180, 333, "N3")][0] == 30 and lines[(4180, 333, "N3")][1] == D2
    assert not unalloc


def test_rows_without_sku_are_reported_not_loaded():
    compiled = [(1, "R1", D1)]
    sc = {1: {"Soil A 25": 1}}
    pack = [row("R1", "Soil A 25", 1, None, 24, desc="Airy Root Blend")]
    lines, rep, unalloc, unmapped, *_ = allocate(compiled, sc, pack)
    assert not lines and unmapped["(no SKU) Airy Root Blend"] == 24
