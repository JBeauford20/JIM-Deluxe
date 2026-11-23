"""Store master: rule meanings (workers/store_rules.py), workbook parsing + change planning
(services/store_master.py) and the handling tag in exports. Pure — no DB.
Run: python -m pytest tests/test_store_master.py -q"""
import io
import os
import sys

import openpyxl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import store_rules as R  # noqa: E402
from services import store_master as M  # noqa: E402
from services import v2_exports as X  # noqa: E402

LITTLES = {"family": "9cm Littles", "program": "Littles", "legacy_product_group": "Premium",
           "ipr_family": "9cm Standard"}
CERAMIC9 = {"family": "9cm Littles", "program": "Littles Ceramic", "legacy_product_group": "Premium",
            "ipr_family": "9cm Standard"}
HB9 = {"family": "9cm Canopy", "program": None, "legacy_product_group": "Premium",
       "ipr_family": "9cm Canopy/Hanging Basket"}
STD12 = {"family": "12cm", "legacy_product_group": "Premium", "ipr_family": "12cm Standard"}
COLL17 = {"family": "17cm", "legacy_product_group": "Collectors", "ipr_family": "17cm Collector"}
RARE12 = {"family": "12cm", "legacy_product_group": "Rare Collectors", "ipr_family": "12cm Rare Collector"}
HOLIDAY = {"family": "Holiday", "legacy_product_group": "Premium", "is_holiday": True}


# ── rule meanings ────────────────────────────────────────────────────────────
def test_sheet_text_maps_to_rule_codes():
    assert R.rule_code_for_text("No Hedera") == "no_hedera"
    assert R.rule_code_for_text(" only 9CM littles, Collectors, Rare Collectors, Holiday, or Promo ") \
        == "littles_collectors_only"
    assert R.rule_code_for_text("Only Holiday when directed") == "holiday_only_directed"
    assert R.rule_code_for_text(None) is None and R.rule_code_for_text("  ") is None
    assert R.rule_code_for_text("No Ferns").startswith("unknown:")


def test_unrestricted_store_allows_everything():
    assert R.rule_for({"sku_rule": None}) is None
    assert R.sku_allowed(None, STD12) == (True, None)


def test_littles_collectors_only_allow_list():
    r = R.rule_for({"sku_rule": "littles_collectors_only"})
    for ok in (LITTLES, COLL17, RARE12, HOLIDAY):
        assert R.sku_allowed(r, ok)[0], ok
    for no in (STD12, CERAMIC9, HB9):          # Josh 2026-11-22: no ceramics, no hanging basket
        assert not R.sku_allowed(r, no)[0], no
    assert not R.routine_blocked(r)


def test_holiday_only_directed_blocks_all_routine_and_needs_direction():
    r = R.rule_for({"sku_rule": "holiday_only_directed"})
    assert R.routine_blocked(r)
    assert not R.sku_allowed(r, LITTLES)[0] and not R.sku_allowed(r, HOLIDAY)[0]
    # directed by an approved scenario naming the store: only its allowed (holiday) product
    assert R.sku_allowed(r, HOLIDAY, directed=True)[0]
    assert not R.sku_allowed(r, LITTLES, directed=True)[0]
    assert R.promo_eligible(r, directed=False) is False
    assert R.promo_eligible(r, directed=True) is True
    assert R.promo_eligible(None) is True


def test_unknown_rule_fails_closed():
    r = R.rule_for({"sku_rule": "something_new"})
    assert r["unknown"] and R.routine_blocked(r) and not R.sku_allowed(r, LITTLES)[0]
    assert R.promo_eligible(r, directed=True) is False


def test_setting_can_extend_rules():
    rules = R.merged_rules({"no_ferns": {"label": "No Ferns", "deny_genus": ["Nephrolepis"]}})
    assert R.denied_genera(R.rule_for({"sku_rule": "no_ferns"}, rules)) == frozenset({"nephrolepis"})
    assert R.denied_genera(R.rule_for({"sku_rule": "no_hedera"}, rules)) == frozenset({"hedera"})


def test_handling_notes():
    assert R.handling_notes({"cross_dock": "ICS-Mass", "sku_rule": "no_hedera"}) == "X-Dock: ICS-Mass; NO HEDERA"
    assert R.handling_notes({}) == ""


# ── variety pools ────────────────────────────────────────────────────────────
HED = frozenset({"hedera"})


def test_restricted_store_never_draws_denied_genus():
    p = R.VarietyPools({"hedera"})
    p.add(1, "Hedera", 60)
    p.add(1, "Epipremnum", 40)
    assert p.left(1) == 100 and p.left(1, HED) == 40
    assert p.take(1, 40, HED) == 40
    assert p.left(1, HED) == 0 and p.left(1) == 60
    assert p.supply(1, HED) == 40 and p.denied_supply(1, HED) == 60


def test_unrestricted_store_uses_sensitive_genus_first():
    p = R.VarietyPools({"hedera"})
    p.add(1, "hedera", 60)
    p.add(1, "epipremnum", 40)
    p.take(1, 50)                         # unrestricted: all from hedera
    assert p.left(1, HED) == 40           # the restricted-safe stock is untouched


def _dist(total, lines):
    out, left = [], total
    for lid, av in lines:
        t = min(left, av)
        if t > 0:
            out.append((lid, t))
            left -= t
    return out


def test_assign_lines_keeps_denied_lines_away_from_restricted_group():
    lines = [["H", 60, "hedera"], ["E", 40, "epipremnum"]]
    got = R.assign_lines({HED: 30, frozenset(): 60}, lines, _dist)
    assert got[HED] == {"E": 30}
    assert got[frozenset()] == {"H": 60}          # unrestricted soak up the Hedera first
    got2 = R.assign_lines({HED: 40, frozenset(): 60}, lines, _dist)
    assert "H" not in got2[HED] and sum(got2[HED].values()) == 40


def test_assign_lines_shortfall_is_left_unassigned_not_given_denied_stock():
    got = R.assign_lines({HED: 50}, [["H", 60, "hedera"], ["E", 10, "epipremnum"]], _dist)
    assert got[HED] == {"E": 10}


# ── workbook parsing + planning ──────────────────────────────────────────────
HDR = ["STORE_NUM", "Status", "Cross Dock?", "SKU Restrictions", "Merchandiser", "Facility Name",
       "District #", "MKT_NBR", "MKT_NM", "DIV_NBM", "Division", "Merchant", "Region Name",
       "Street Address", "City", "State", "Postal Code", "BYO_NBR"]


def _wb(rows, header=HDR, extra_sheet=True):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(header)
    for r in rows:
        ws.append(r)
    if extra_sheet:                       # must be ignored: only the first sheet counts
        w2 = wb.create_sheet("Stores")
        w2.append(["STORE_NUM", "Status", "Cross Dock?", "SKU Restrictions"])
        w2.append([999, "Do Not Ship", "No", None])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _row(sid, status=None, cd="No", restr=None, merch="Altman - W", **kw):
    return [sid, status, cd, restr, merch, "Fac", "0074", 48, "LA", "2", kw.get("division", "WESTERN"),
            kw.get("merchant", "Brian Parker"), "PAC CENTRAL", "1 Main St", "Town", kw.get("state", "CA"),
            "90703", 5]


def test_parse_reads_only_first_sheet_and_normalises():
    p = M.parse_workbook(_wb([
        _row(664, "Do Not Ship"),
        _row(608, "With Exceptions", restr="Only 9cm Littles, Collectors, Rare Collectors, Holiday, or Promo"),
        _row(2601, None, cd="ICS-Mass", merch="#N/A"),
        _row(4701, None, restr="No Hedera", merch="Costa - W"),
    ]), "m.xlsx")
    assert p["sheet"] == "Sheet1" and p["sheets_ignored"] == ["Stores"] and not p["problems"]
    by = {r["store_id"]: r for r in p["rows"]}
    assert set(by) == {664, 608, 2601, 4701}
    assert by[664]["master_status"] == "Do Not Ship" and by[664]["cross_dock"] is None
    assert by[608]["sku_rule"] == "littles_collectors_only"
    # same market + district as the others, but they disagree (Altman - W / Costa - W)
    assert by[2601]["cross_dock"] == "ICS-Mass" and by[2601]["merchandiser"] is None
    assert [x["store_id"] for x in p["merchandiser_unresolved"]] == [2601]
    assert by[4701]["sku_rule"] == "no_hedera" and by[4701]["district"] == "0074"
    assert by[664]["division"] == "West" and by[664]["region"] == "Western"


def test_parse_rejects_unknown_status_restriction_duplicates_and_missing_columns():
    p = M.parse_workbook(_wb([
        _row(1, "Closed"), _row(2, None, restr="No Ferns"), _row(3), _row(3),
        _row(4, "Kurt Weiss"),                       # needs a restriction
    ]), "m.xlsx")
    assert [r["store_id"] for r in p["rows"]] == [3]
    assert len(p["problems"]) == 4
    short = M.parse_workbook(_wb([[1, "Do Not Ship"]], header=["STORE_NUM", "Status"]), "m.xlsx")
    assert short["rows"] == [] and short["problems"]


DB = {
    664: {"store_id": 664, "store_name": "Clovis", "state": "CA", "store_status": "active", "merchant": "Brian Parker",
          "division": "West", "region": "Western"},
    700: {"store_id": 700, "store_name": "Was DNS", "state": "CA", "store_status": "do_not_ship",
          "master_status": "Do Not Ship", "merchant": "Old Name", "division": "West", "region": "Western"},
    701: {"store_id": 701, "store_name": "Manual DNS", "state": "CA", "store_status": "do_not_ship",
          "merchant": None, "division": "West", "region": "Western"},
    6177: {"store_id": 6177, "store_name": "Not on sheet", "state": "NY", "store_status": "active"},
}


def _plan(update=False):
    parsed = M.parse_workbook(_wb([
        _row(664, "Do Not Ship"),
        _row(700, None, merchant="New Name"),
        _row(701, None, merchant="Filled In"),
        _row(12345, None),
    ]), "m.xlsx")
    return M.plan(parsed, DB, update_descriptive=update)


def test_plan_status_rules():
    pl = _plan()
    st = {(c["store_id"], c["new"]) for c in pl["changes"] if c["field"] == "store_status"}
    assert (664, "do_not_ship") in st                 # sheet says Do Not Ship
    assert (700, "active") in st                      # master set it, master lifts it
    assert not any(s == 701 for s, _ in st)           # a person set it — never lifted by a load
    assert pl["qa"]["before"]["do_not_ship"] == 2 and pl["qa"]["after"]["do_not_ship"] == 2
    assert [x["store_id"] for x in pl["not_in_app"]] == [12345]
    assert [x["store_id"] for x in pl["not_in_sheet"]] == [6177]
    assert pl["qa"]["matched"] == 3 and pl["qa"]["unmatched_sheet_not_in_app"] == 1


def test_plan_descriptive_fields_fill_blanks_but_keep_conflicts_unless_asked():
    pl = _plan()
    merch = {c["store_id"]: c["new"] for c in pl["changes"] if c["field"] == "merchant"}
    assert merch == {701: "Filled In"}                # blank filled; 700's conflict kept
    assert pl["conflicts_total"] == 1 and pl["conflicts_summary"][0]["sheet"] == "New Name"
    pl2 = _plan(update=True)
    merch2 = {c["store_id"]: c["new"] for c in pl2["changes"] if c["field"] == "merchant"}
    assert merch2 == {701: "Filled In", 700: "New Name"} and pl2["conflicts_total"] == 0


def test_blank_merchandiser_filled_only_when_neighbours_agree():
    p = M.parse_workbook(_wb([_row(1), _row(2), _row(3, merch="#N/A"), _row(4, merch="Altmas - S")]), "m.xlsx")
    by = {r["store_id"]: r for r in p["rows"]}
    assert by[4]["merchandiser"] == "Altman - S"            # known typo canonicalised
    assert by[3]["merchandiser"] is None                     # market now has two merchandisers
    p2 = M.parse_workbook(_wb([_row(1), _row(2), _row(3, merch="#N/A")]), "m.xlsx")
    assert {r["store_id"]: r["merchandiser"] for r in p2["rows"]}[3] == "Altman - W"
    assert p2["merchandiser_inferred"][0]["basis"].startswith("market 48")


def test_confirmed_merchant_successions_apply_without_the_flag():
    db = {1: {"store_id": 1, "store_status": "active", "merchant": "MARK KOCIUBINSKI"},
          2: {"store_id": 2, "store_status": "active", "merchant": "Bryan Dennison"},
          3: {"store_id": 3, "store_status": "active", "merchant": "Mark Kociubinski"},
          4: {"store_id": 4, "store_status": "active", "merchant": "Someone Else"}}
    parsed = M.parse_workbook(_wb([_row(1, merchant="Paul Haitsch"), _row(2, merchant="Joe Dandan"),
                                   _row(3, merchant="Dave Graham"), _row(4, merchant="Paul Haitsch")]), "m.xlsx")
    pl = M.plan(parsed, db)
    assert {c["store_id"]: c["new"] for c in pl["changes"] if c["field"] == "merchant"} \
        == {1: "Paul Haitsch", 2: "Joe Dandan"}
    assert pl["conflicts_total"] == 2                        # 3 and 4 are not confirmed successions


def test_plan_is_idempotent_once_applied():
    pl = _plan()
    after = {sid: dict(s) for sid, s in DB.items()}
    for c in pl["changes"]:
        after[c["store_id"]][c["field"]] = c["new"]
    parsed = M.parse_workbook(_wb([_row(664, "Do Not Ship"), _row(700, None, merchant="New Name"),
                                   _row(701, None, merchant="Filled In")]), "m.xlsx")
    assert M.plan(parsed, after)["changes"] == []


# ── exports ──────────────────────────────────────────────────────────────────
def test_tagged_cart_gets_its_own_config_and_notes():
    shelves = [{"cart_id": "a", "shelf_position": 1, "sku_id": 1, "kit_type": None, "tray_count": 8},
               {"cart_id": "b", "shelf_position": 1, "sku_id": 1, "kit_type": None, "tray_count": 8}]
    plain = {"id": "a", "store_id": 121, "config_code": "CC1"}
    tagged = {"id": "b", "store_id": 4701, "config_code": "CC1", "handling_tag": "NO HEDERA"}
    assert X.cart_signature(plain, shelves[:1]) != X.cart_signature(tagged, shelves[1:])
    by_cart, _names, _new = X.assign_config_names({"carts": [plain, tagged], "shelves": shelves})
    assert by_cart["a"] != by_cart["b"]
    assert X.store_notes({"cross_dock": "Free State", "sku_rule": "no_hedera"}) == "X-Dock: Free State; NO HEDERA"
    assert X.store_address({"street_address": "1 Main St", "postal_code": "90703"}) == "1 Main St, 90703"
    assert X.store_address({}) == ""


# ── availability grade: grade 3 (mature / contract grower) is picked first ──
RANK = {"G3a": 0, "G3b": 0, "G2": 998}          # as the engine builds it for grade_first = 3


def test_preferred_grade_is_used_up_before_the_other():
    lines = [["G2", 500, ""], ["G3a", 60, ""], ["G3b", 40, ""]]
    got = R.assign_lines({frozenset(): 80}, lines, _dist, rank=RANK)[frozenset()]
    assert sum(got.values()) == 80 and "G2" not in got
    got = R.assign_lines({frozenset(): 150}, lines, _dist, rank=RANK)[frozenset()]
    assert got["G3a"] == 60 and got["G3b"] == 40 and got["G2"] == 50


def test_grade_never_overrides_a_variety_restriction():
    lines = [["G3a", 100, "hedera"], ["G2", 100, "epipremnum"]]
    got = R.assign_lines({HED: 30, frozenset(): 50}, lines, _dist, rank={"G3a": 0, "G2": 998})
    assert got[HED] == {"G2": 30}                 # restricted store never gets the Hedera, grade 3 or not
    assert got[frozenset()] == {"G3a": 50}


def test_without_a_rank_assignment_is_unchanged():
    lines = [["A", 60, ""], ["B", 40, ""]]
    assert R.assign_lines({frozenset(): 50}, lines, _dist) == R.assign_lines({frozenset(): 50}, lines, _dist, rank=None)


# ── a directed shipment of ONE named variety (e.g. Monstera Bulbasaur 17cm) ──
PLANT = {"B1": "1001312", "B2": "3000668", "X": "1000999", "Y": "1000888"}
BULBA = ("variety", frozenset({"1001312", "3000668", "3000696"}))


def test_named_variety_draws_only_its_own_lines_and_is_served_first():
    lines = [["X", 500, ""], ["B1", 100, ""], ["B2", 50, ""], ["Y", 300, ""]]
    got = R.assign_sku_lines({BULBA: 126, frozenset(): 400}, lines, PLANT, _dist)
    assert set(got[BULBA]) <= {"B1", "B2"} and sum(got[BULBA].values()) == 126
    assert sum(got[frozenset()].values()) == 400
    # everyone else only gets what the named shipment left on those lines
    left = {"B1": 100 - got[BULBA].get("B1", 0), "B2": 50 - got[BULBA].get("B2", 0)}
    assert all(got[frozenset()].get(k, 0) <= v for k, v in left.items())


def test_named_variety_is_never_substituted_when_short():
    got = R.assign_sku_lines({BULBA: 200}, [["X", 500, ""], ["B1", 60, ""]], PLANT, _dist)
    assert got[BULBA] == {"B1": 60}                      # only what exists; no other variety


def test_assign_sku_lines_without_varieties_matches_assign_lines():
    lines = [["G2", 500, ""], ["G3a", 60, ""], ["G3b", 40, ""]]
    grp = {frozenset(): 150, HED: 20}
    assert R.assign_sku_lines(grp, lines, {}, _dist, rank=RANK) == R.assign_lines(grp, lines, _dist, rank=RANK)


def test_an_approved_directed_shipment_overrides_product_rules_but_not_unknown_ones():
    assert R.directed_overrides(R.rule_for({"sku_rule": "holiday_only_directed"}))
    assert R.directed_overrides(R.rule_for({"sku_rule": "littles_collectors_only"}))
    assert not R.directed_overrides(R.rule_for({"sku_rule": "something_new"}))
    assert not R.directed_overrides(None)
    # the variety ban is separate and untouched by the override
    assert R.denied_genera(R.rule_for({"sku_rule": "no_hedera"})) == frozenset({"hedera"})
