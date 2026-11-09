"""Read-only /explorer/... routes over the indexer database (see indexer.py)"""
import json
import os
import sqlite3
import time

from flask import Blueprint, jsonify, request

from indexer import REWARD_OPS

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
        for m in con.execute(f"SELECT * FROM moves WHERE txid IN ({marks(ids)}) ORDER BY idx", ids):
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


def marks(values):
    return ",".join("?" * len(values))


def moves_page(con, addrs, before, limit, ops=None, skip_ops=None):
    """Transactions moving value to or from any of addrs below height `before`, newest first:
    (rows, next cursor). One ordered index walk per address and side reads only limit + 1
    rows each; one IN / OR query would read every move of a busy account before sorting"""
    where, extra = "", ()
    if ops:
        where, extra = f" AND op IN ({marks(ops)})", tuple(ops)
    elif skip_ops:
        where, extra = f" AND op NOT IN ({marks(skip_ops)})", tuple(skip_ops)
    found = []
    for col in ("from_addr", "to_addr"):
        for a in addrs:
            found += con.execute(
                f"SELECT txid, height FROM moves WHERE {col} = ? AND height < ?{where} "
                "ORDER BY height DESC LIMIT ?", (a, before, *extra, limit + 1)).fetchall()
    found = sorted(set(found), key=lambda r: r[1], reverse=True)
    full = len(found) > limit
    found = found[:limit]
    #finish the last block so before=<lowest height> never skips part of a block
    if full:
        last = found[-1][1]
        for col in ("from_addr", "to_addr"):
            for a in addrs:
                found += con.execute(
                    f"SELECT txid, height FROM moves WHERE {col} = ? AND height = ?{where}",
                    (a, last, *extra)).fetchall()
    ids = list(dict.fromkeys(r[0] for r in found))
    id_marks = marks(ids) or "''"
    rows = tx_rows(con, f"txid IN ({id_marks})", ids, len(ids) or 1)
    return rows, (rows[-1]["height"] if full else None)


#operations on a register itself, for its history
REGISTER_OPS = ("CREATE", "WRITE", "APPEND", "TRANSFER", "CLAIM")


@bp.route("/address/<addr>/transactions")
def address_transactions(addr):
    """registers=1 = only operations on the register itself (created, changed, transferred)"""
    con = db()
    if not con:
        return unavailable()
    limit = arg_int("limit", 25, 1, MAX_LIMIT)
    before = arg_int("before", 2 ** 31)
    ops = REGISTER_OPS if request.args.get("registers") == "1" else None
    with con:
        rows, nxt = moves_page(con, [addr], before, limit, ops=ops)
    return jsonify({"result": rows, "next": nxt})


@bp.route("/address/<addr>/rewards")
def address_rewards(addr):
    """Staking rewards of a trust account per UTC month: [month, rewards, NXS]"""
    con = db()
    if not con:
        return unavailable()
    with con:
        rows = con.execute(
            "SELECT strftime('%Y-%m', time, 'unixepoch') AS m, COUNT(*), SUM(amount) FROM moves "
            f"WHERE to_addr = ? AND op IN ({marks(REWARD_OPS)}) GROUP BY m ORDER BY m",
            (addr, *REWARD_OPS)).fetchall()
        last = con.execute(
            f"SELECT COUNT(*), SUM(amount) FROM moves WHERE to_addr = ? AND op IN ({marks(REWARD_OPS)}) "
            "AND time >= ?", (addr, *REWARD_OPS, int(time.time()) - 30 * 86400)).fetchone()
    return jsonify({"result": [[m, n, round(s or 0, 6)] for m, n, s in rows],
                    "last30": {"count": last[0], "amount": round(last[1] or 0, 6)}})


@bp.route("/register/<register_id>")
def register_by_id(register_id):
    """Base58 address of a register given in its 64 hex form (owner of a tokenized register)"""
    con = db()
    if not con:
        return unavailable()
    try:
        with con:
            row = con.execute("SELECT address, kind, name, ticker FROM registers WHERE id = ?",
                              (register_id.lower(),)).fetchone()
    except sqlite3.OperationalError:
        return unavailable()
    if not row:
        return jsonify({"error": {"code": 404, "message": "register not in the owner index"}}), 404
    return jsonify({"result": dict(row)})


OWNER_LIMIT = 1000


@bp.route("/owner/<genesis>")
def owner_registers(genesis):
    """Registers owned by one signature chain, from the hourly owner index"""
    con = db()
    if not con:
        return unavailable()
    try:
        with con:
            total = con.execute("SELECT COUNT(*) FROM registers WHERE owner = ?", (genesis,)).fetchone()[0]
            rows = con.execute(
                "SELECT * FROM registers WHERE owner = ? "
                "ORDER BY kind, COALESCE(balance, 0) DESC, modified DESC LIMIT ?",
                (genesis, OWNER_LIMIT)).fetchall()
            updated = con.execute("SELECT updated FROM cache WHERE key = 'tokenholders'").fetchone()
    except sqlite3.OperationalError:
        #index made by an indexer without the owner table
        return unavailable()
    regs = [{**dict(r), "info": json.loads(r["info"]) if r["info"] else None} for r in rows]
    return jsonify({"result": {"registers": regs, "total": total,
                               "updated": updated[0] if updated else None}})


#registers whose value moves count as the chain's own
VALUE_KINDS = ("account", "trust", "token")
#two small queries per address; a chain with more registers gets its largest balances only
MAX_OWNER_ADDRESSES = 500


@bp.route("/owner/<genesis>/transactions")
def owner_transactions(genesis):
    """Value moves to or from any account, trust account or token of one signature chain;
    transfers=1 leaves out mining and staking rewards"""
    con = db()
    if not con:
        return unavailable()
    limit = arg_int("limit", 25, 1, MAX_LIMIT)
    before = arg_int("before", 2 ** 31)
    skip = REWARD_OPS if request.args.get("transfers") == "1" else None
    try:
        with con:
            addrs = [r[0] for r in con.execute(
                f"SELECT address FROM registers WHERE owner = ? AND kind IN ({marks(VALUE_KINDS)}) "
                "ORDER BY COALESCE(balance, 0) DESC",
                (genesis, *VALUE_KINDS))]
            if not addrs:
                return jsonify({"result": [], "addresses": [], "next": None})
            addrs = addrs[:MAX_OWNER_ADDRESSES]
            rows, nxt = moves_page(con, addrs, before, limit, skip_ops=skip)
    except sqlite3.OperationalError:
        return unavailable()
    return jsonify({"result": rows, "addresses": addrs, "next": nxt})


SEARCH_LIMIT = 50
SEARCH_MAX_LEN = 64
#searchable by name: tokens (ticker), global names, namespaces, assets; local names are
#only unique inside one signature chain, and most are "default" or "trust"


@bp.route("/search")
def search_names():
    """Registers whose name starts with q (any case), exact matches first"""
    con = db()
    if not con:
        return unavailable()
    q = (request.args.get("q") or "").strip()[:SEARCH_MAX_LEN]
    if len(q) < 2:
        return jsonify({"result": []})
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        with con:
            rows = con.execute(
                "SELECT address, owner, kind, name, token, ticker, info, modified FROM registers "
                "WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "AND (kind IN ('token', 'namespace', 'asset') "
                "OR (kind = 'name' AND json_extract(info, '$.global') = 1)) "
                "ORDER BY lower(name) = lower(?) DESC, "
                "CASE kind WHEN 'token' THEN 0 WHEN 'name' THEN 1 WHEN 'namespace' THEN 2 ELSE 3 END, "
                "length(name), name LIMIT ?", (like, q, SEARCH_LIMIT)).fetchall()
    except sqlite3.OperationalError:
        return unavailable()
    return jsonify({"result": [{**dict(r), "info": json.loads(r["info"]) if r["info"] else None}
                               for r in rows]})


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
