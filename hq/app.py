"""
總部 (HQ) 服務
- 品牌入口網站：列出所有門市（分店自動報到，新增分店不需修改總部）
- 共用菜單 API：所有分店從這裡取得固定品項
- 接收各分店回報的訂單 / 營收
- 老闆後台：全品牌營收儀表板、各分店明細、共用菜單管理
"""
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

DB_PATH = os.environ.get("DB_PATH", "/data/hq.db")
HQ_API_KEY = os.environ.get("HQ_API_KEY", "change-me-api-key")
ADMIN_USER = os.environ.get("ADMIN_USER", "boss")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "boss1234")
OFFLINE_AFTER_SECONDS = int(os.environ.get("OFFLINE_AFTER_SECONDS", "90"))
BRAND_NAME = os.environ.get("BRAND_NAME", "開心飲品")

DEFAULT_MENU = [
    ("開心奶茶", 70, "濃郁紅茶與香醇奶香的招牌組合", "happy_milk_tea.jpg"),
    ("快樂紅茶", 55, "嚴選錫蘭紅茶，清爽回甘", "happy_black_tea.jpg"),
    ("烏龍綠茶", 60, "烏龍焙香融合綠茶清新", "oolong_green_tea.jpg"),
    ("茉莉花茶", 70, "茉莉花香層層綻放", "jasmine_tea.jpg"),
    ("甘蔗青茶", 65, "古早味甘蔗搭配青茶，自然清甜", "sugarcane_green_tea.jpg"),
]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me") + "-hq"
app.config["SESSION_COOKIE_NAME"] = "hq_session"
# 經過 Traefik 閘道時，正確取得前綴路徑、Host 與協定
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


# ---------------------------------------------------------------- DB ----
def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS menu (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            price       INTEGER NOT NULL,
            description TEXT DEFAULT '',
            image       TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS stores (
            store_id   TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            city       TEXT,
            url        TEXT,
            first_seen TEXT,
            last_seen  TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            store_id   TEXT NOT NULL,
            order_no   TEXT NOT NULL,
            total      INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (store_id, order_no)
        );
        CREATE TABLE IF NOT EXISTS order_items (
            store_id  TEXT NOT NULL,
            order_no  TEXT NOT NULL,
            name      TEXT NOT NULL,
            kind      TEXT NOT NULL,      -- common / special
            qty       INTEGER NOT NULL,
            price     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_items_store ON order_items(store_id);
        """
    )
    if conn.execute("SELECT COUNT(*) FROM menu").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO menu(name, price, description, image) VALUES (?, ?, ?, ?)",
            DEFAULT_MENU)
    conn.commit()
    conn.close()


init_db()


# ------------------------------------------------------------ helpers ----
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != HQ_API_KEY:
            return jsonify({"error": "invalid api key"}), 401
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def is_online(last_seen):
    if not last_seen:
        return False
    seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
    return datetime.now() - seen < timedelta(seconds=OFFLINE_AFTER_SECONDS)


@app.template_filter("money")
def money(v):
    return f"${int(v or 0):,}"


def image_url(filename):
    """產品圖存在才回傳網址，否則回傳 None（頁面改顯示預設色塊）"""
    if filename and os.path.isfile(os.path.join(app.static_folder, "drinks",
                                                os.path.basename(filename))):
        return url_for("static", filename="drinks/" + os.path.basename(filename))
    return None


@app.context_processor
def inject_globals():
    return {"brand": BRAND_NAME, "image_url": image_url}


# ---------------------------------------------------------- 分店用 API ----
@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "hq"}


@app.get("/api/menu")
def api_menu():
    """所有分店共用的固定菜單（公開讀取）"""
    rows = get_db().execute(
        "SELECT name, price, description, image FROM menu WHERE active=1 ORDER BY id"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/stores")
def api_stores():
    """門市清單（公開讀取）"""
    return jsonify([{k: s[k] for k in ("store_id", "name", "city", "url", "online")}
                    for s in store_summaries()])


@app.post("/api/stores/register")
@require_api_key
def api_register():
    """分店啟動時 / 定期向總部報到（新分店會自動加入）"""
    data = request.get_json(force=True)
    if not data.get("store_id") or not data.get("name"):
        return jsonify({"error": "store_id and name required"}), 400
    db = get_db()
    now = now_str()
    db.execute(
        """INSERT INTO stores(store_id, name, city, url, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(store_id) DO UPDATE SET
             name=excluded.name, city=excluded.city,
             url=excluded.url, last_seen=excluded.last_seen""",
        (data["store_id"], data["name"], data.get("city", ""),
         data.get("url", ""), now, now),
    )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/orders")
@require_api_key
def api_orders():
    """分店回報訂單（可批次、可重送；重複的訂單編號會被忽略）"""
    data = request.get_json(force=True)
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    db = get_db()
    accepted = []
    for o in data.get("orders", []):
        cur = db.execute(
            "INSERT OR IGNORE INTO orders(store_id, order_no, total, created_at) "
            "VALUES (?, ?, ?, ?)",
            (store_id, o["order_no"], int(o["total"]), o["created_at"]),
        )
        if cur.rowcount:  # 第一次收到才寫入明細
            db.executemany(
                "INSERT INTO order_items(store_id, order_no, name, kind, qty, price) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(store_id, o["order_no"], i["name"], i["kind"], int(i["qty"]),
                  int(i["price"])) for i in o.get("items", [])],
            )
        accepted.append(o["order_no"])
    db.execute("UPDATE stores SET last_seen=? WHERE store_id=?", (now_str(), store_id))
    db.commit()
    return jsonify({"accepted": accepted})


@app.get("/api/summary")
@require_api_key
def api_summary():
    """各分店營收摘要（JSON，供程式介接）"""
    return jsonify(store_summaries())


# ------------------------------------------------------------ 統計 ----
def store_summaries():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute(
        """SELECT s.store_id, s.name, s.city, s.url, s.first_seen, s.last_seen,
                  COUNT(o.order_no)              AS order_count,
                  COALESCE(SUM(o.total), 0)      AS revenue,
                  COALESCE(SUM(CASE WHEN substr(o.created_at,1,10)=? THEN o.total END), 0)
                                                 AS today_revenue,
                  COALESCE(SUM(CASE WHEN substr(o.created_at,1,10)=? THEN 1 END), 0)
                                                 AS today_orders
           FROM stores s LEFT JOIN orders o ON o.store_id = s.store_id
           GROUP BY s.store_id ORDER BY s.city, s.store_id""",
        (today, today),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["online"] = is_online(r["last_seen"])
        d["avg_ticket"] = round(d["revenue"] / d["order_count"]) if d["order_count"] else 0
        result.append(d)
    return result


def daily_series(days=14, store_id=None):
    """回傳最近 N 天（含今天）每天的營收，沒有訂單的日子補 0"""
    start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    sql = ("SELECT substr(created_at,1,10) AS day, SUM(total) AS revenue, COUNT(*) AS orders "
           "FROM orders WHERE substr(created_at,1,10) >= ?")
    args = [start]
    if store_id:
        sql += " AND store_id=?"
        args.append(store_id)
    rows = {r["day"]: r for r in get_db().execute(sql + " GROUP BY day", args)}
    series = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        r = rows.get(day)
        series.append({"day": day, "label": day[5:].replace("-", "/"),
                       "revenue": r["revenue"] if r else 0,
                       "orders": r["orders"] if r else 0})
    return series


def group_by_city(stores):
    cities = {}
    for s in stores:
        cities.setdefault(s["city"] or "其他", []).append(s)
    return cities


# --------------------------------------------------------- 品牌入口 ----
@app.get("/")
def portal():
    stores = store_summaries()
    menu = get_db().execute(
        "SELECT name, price, description, image FROM menu WHERE active=1 ORDER BY id"
    ).fetchall()
    return render_template("portal.html", cities=group_by_city(stores),
                           store_count=len(stores), menu=menu)


# ------------------------------------------------------- 老闆後台 ----
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (request.form.get("username") == ADMIN_USER
                and request.form.get("password") == ADMIN_PASSWORD):
            session["admin"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("dashboard"))
        flash("帳號或密碼錯誤")
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("portal"))


@app.get("/admin")
@require_admin
def dashboard():
    db = get_db()
    stores = store_summaries()
    totals = {
        "revenue": sum(s["revenue"] for s in stores),
        "today": sum(s["today_revenue"] for s in stores),
        "orders": sum(s["order_count"] for s in stores),
        "today_orders": sum(s["today_orders"] for s in stores),
        "online": sum(1 for s in stores if s["online"]),
        "stores": len(stores),
    }
    totals["avg_ticket"] = round(totals["revenue"] / totals["orders"]) if totals["orders"] else 0

    cities = []
    for city, ss in group_by_city(stores).items():
        cities.append({"city": city, "stores": len(ss),
                       "revenue": sum(s["revenue"] for s in ss),
                       "today": sum(s["today_revenue"] for s in ss)})
    cities.sort(key=lambda c: c["revenue"], reverse=True)

    top_items = db.execute(
        """SELECT name, kind, SUM(qty) AS qty, SUM(qty*price) AS revenue
           FROM order_items GROUP BY name, kind ORDER BY revenue DESC LIMIT 10"""
    ).fetchall()
    special_rank = db.execute(
        """SELECT i.name, i.store_id, s.name AS store_name, SUM(i.qty) AS qty,
                  SUM(i.qty*i.price) AS revenue
           FROM order_items i LEFT JOIN stores s ON s.store_id = i.store_id
           WHERE i.kind='special' GROUP BY i.store_id, i.name
           ORDER BY revenue DESC LIMIT 8"""
    ).fetchall()
    ranking = sorted(stores, key=lambda s: s["revenue"], reverse=True)
    return render_template("dashboard.html", stores=stores, totals=totals,
                           cities=cities, top_items=top_items,
                           special_rank=special_rank, ranking=ranking[:10],
                           daily=daily_series(14))


@app.get("/admin/stores/<store_id>")
@require_admin
def store_detail(store_id):
    db = get_db()
    summary = next((s for s in store_summaries() if s["store_id"] == store_id), None)
    if not summary:
        abort(404)
    items = db.execute(
        """SELECT name, kind, SUM(qty) AS qty, SUM(qty*price) AS revenue
           FROM order_items WHERE store_id=? GROUP BY name, kind ORDER BY revenue DESC""",
        (store_id,),
    ).fetchall()
    recent = db.execute(
        "SELECT * FROM orders WHERE store_id=? ORDER BY created_at DESC LIMIT 20",
        (store_id,),
    ).fetchall()
    return render_template("store_detail.html", s=summary, items=items,
                           recent=recent, daily=daily_series(14, store_id))


@app.post("/admin/stores/<store_id>/remove")
@require_admin
def remove_store(store_id):
    """把已停業的分店從門市清單移除（歷史營收保留）。分店若仍在運作會再次自動報到。"""
    db = get_db()
    db.execute("DELETE FROM stores WHERE store_id=?", (store_id,))
    db.commit()
    flash(f"已將 {store_id} 從門市清單移除（歷史營收保留）")
    return redirect(url_for("dashboard"))


@app.route("/admin/menu", methods=["GET", "POST"])
@require_admin
def manage_menu():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                name = request.form["name"].strip()
                price = int(request.form["price"])
                if not name or price <= 0:
                    raise ValueError
                db.execute("INSERT INTO menu(name, price, description) VALUES (?, ?, ?)",
                           (name, price, request.form.get("description", "").strip()))
                flash(f"已新增共用品項「{name}」，各分店 30 秒內自動同步")
            elif action == "update":
                price = int(request.form["price"])
                if price <= 0:
                    raise ValueError
                db.execute("UPDATE menu SET price=?, description=? WHERE id=?",
                           (price, request.form.get("description", "").strip(),
                            int(request.form["id"])))
                flash("已更新，各分店將在下次同步時套用")
            elif action == "toggle":
                db.execute("UPDATE menu SET active = 1 - active WHERE id=?",
                           (int(request.form["id"]),))
                flash("品項狀態已切換")
            db.commit()
        except sqlite3.IntegrityError:
            flash("品項名稱已存在")
        except (KeyError, ValueError):
            flash("輸入資料有誤")
        return redirect(url_for("manage_menu"))
    rows = db.execute("SELECT * FROM menu ORDER BY id").fetchall()
    return render_template("menu.html", menu=rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
