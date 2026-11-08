"""Read-only /explorer/... routes over the indexer database (see indexer.py)"""
import json
import os
import sqlite3
import time

from flask import Blueprint, jsonify, request

INDEX_DB = os.getenv("INDEX_DB", "explorer_index.sqlite")
MAX_LIMIT = 100

bp = Blueprint("explorer_index", __name__, url_prefix="/explorer")


def db():
    if not os.path.exists(INDEX_DB):
        return None
    con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def unavailable():
    return jsonify({"error": {"code": 503, "message": "index not available"}}), 503


def arg_int(name, default, lo=0, hi=None):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    v = max(lo, v)
    return min(v, hi) if hi is not None else v


@bp.route("/status")
def status():
    con = db()
    if not con:
        return unavailable()
    with con:
        high = con.execute("SELECT MAX(height) FROM blocks").fetchone()[0]
        low = con.execute("SELECT value FROM meta WHERE key='backfill_low'").fetchone()
        caches = {r["key"]: r["updated"] for r in con.execute("SELECT key, updated FROM cache")}
    return jsonify({"result": {"high": high, "low": int(low[0]) if low else None,
                               "caches": caches, "now": int(time.time())}})


def tx_rows(con, where, params, limit):
    rows = con.execute(
        f"SELECT * FROM txs WHERE {where} ORDER BY height DESC, txid LIMIT ?",
        (*params, limit)).fetchall()
    ids = [r["txid"] for r in rows]
    moves = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for m in con.execute(f"SELECT * FROM moves WHERE txid IN ({marks}) ORDER BY idx", ids):
            moves.setdefault(m["txid"], []).append(dict(m))
    return [{**dict(r), "moves": moves.get(r["txid"], [])} for r in rows]


@bp.route("/transactions")
def transactions():
    """Newest first; transfers=1 hides mining/staking rewards; before=<height> pages down"""
    con = db()
    if not con:
        return unavailable()
    limit = arg_int("limit", 25, 1, MAX_LIMIT)
    before = arg_int("before", 2 ** 31)
    where, params = "height < ?", [before]
    if request.args.get("transfers") == "1":
        where += " AND is_reward = 0 AND type != 'unreadable'"
    with con:
        rows = tx_rows(con, where, params, limit)
        #finish the last block so before=<lowest height> never skips part of a block
        if len(rows) == limit:
            last = rows[-1]["height"]
            seen = {r["txid"] for r in rows}
            rest = tx_rows(con, where.replace("height < ?", "height = ?"), [last, *params[1:]], 10000)
            rows += [r for r in rest if r["txid"] not in seen]
    nxt = rows[-1]["height"] if len(rows) >= limit else None
    return jsonify({"result": rows, "next": nxt})


@bp.route("/address/<addr>/transactions")
def address_transactions(addr):
    con = db()
    if not con:
        return unavailable()
    limit = arg_int("limit", 25, 1, MAX_LIMIT)
    before = arg_int("before", 2 ** 31)
    with con:
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT txid FROM moves WHERE (from_addr = ? OR to_addr = ?) AND height < ? "
            "ORDER BY height DESC LIMIT ?", (addr, addr, before, limit))]
        marks = ",".join("?" * len(ids)) or "''"
        rows = tx_rows(con, f"txid IN ({marks})", ids, limit)
    return jsonify({"result": rows})


@bp.route("/cache/<key>")
def cached(key):
    con = db()
    if not con:
        return unavailable()
    with con:
        row = con.execute("SELECT updated, data FROM cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return jsonify({"error": {"code": 404, "message": f"no cache for {key}"}}), 404
    return jsonify({"result": json.loads(row["data"]), "updated": row["updated"]})
