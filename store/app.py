"""
分店 (Store) 服務 —— 所有分店共用同一份程式碼與同一個 Docker image，
只靠環境變數 (STORE_ID / STORE_NAME ...) 區分，因此版面與操作介面完全一致。

- 前台：顯示「總部共用菜單」+「本店特色飲品」，讓顧客點餐
- 店長後台：管理本店特色飲品（含上傳產品圖）、查看本店經營資訊
- 背景同步：定期向總部報到、拉取最新共用菜單、回報訂單
  （總部暫時斷線時，分店仍可營業；訂單會暫存並在恢復後自動補傳）
"""
import json
import os
import random
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

STORE_ID = os.environ.get("STORE_ID", "dev")
STORE_NAME = os.environ.get("STORE_NAME", "開發測試店")
STORE_CITY = os.environ.get("STORE_CITY", "")
# 經由 Traefik 閘道對外的網址（預設為 /store/<STORE_ID>/）；
# 若分店部署在另一台機器，可設為完整網址，例如 http://203.0.113.5:8001/
STORE_PUBLIC_URL = os.environ.get("STORE_PUBLIC_URL", f"/store/{STORE_ID}/")
BRAND_NAME = os.environ.get("BRAND_NAME", "開心飲品")
BRAND_SITE_URL = os.environ.get("BRAND_SITE_URL", "/")
HQ_URL = os.environ.get("HQ_URL", "http://localhost:8000").rstrip("/")
HQ_API_KEY = os.environ.get("HQ_API_KEY", "change-me-api-key")
MANAGER_USER = os.environ.get("MANAGER_USER", "manager")
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "manager1234")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "store.db"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "30"))
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_specials.json")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}

# 總部完全連不上、本地也沒有快取時的預設菜單
FALLBACK_MENU = [
    ("開心奶茶", 70, "happy_milk_tea.jpg"),
    ("快樂紅茶", 55, "happy_black_tea.jpg"),
    ("烏龍綠茶", 60, "oolong_green_tea.jpg"),
    ("茉莉花茶", 70, "jasmine_tea.jpg"),
    ("甘蔗青茶", 65, "sugarcane_green_tea.jpg"),
]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me") + STORE_ID
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 上傳圖片上限 5MB
# 所有分店共用同一個網域時，每間店用不同的 cookie 名稱，登入狀態才不會互相干擾
app.config["SESSION_COOKIE_NAME"] = f"store_{STORE_ID}_session"
# 經過 Traefik 閘道（/store/<id> 前綴）時，url_for 會自動產生正確路徑
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

sync_state = {"hq_online": False, "last_sync": None, "last_error": None}


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
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS common_menu (      -- 總部共用菜單的本地快取
            name        TEXT PRIMARY KEY,
            price       INTEGER NOT NULL,
            description TEXT DEFAULT '',
            image       TEXT DEFAULT '',
            sort        INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS specials (         -- 本店特色飲品
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            price       INTEGER NOT NULL,
            description TEXT DEFAULT '',
            image       TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_no   TEXT PRIMARY KEY,
            total      INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            synced     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS order_items (
            order_no TEXT NOT NULL,
            name     TEXT NOT NULL,
            kind     TEXT NOT NULL,
            qty      INTEGER NOT NULL,
            price    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_order_items_no ON order_items(order_no);
        """
    )
    if conn.execute("SELECT COUNT(*) FROM common_menu").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO common_menu(name, price, image, sort) VALUES (?, ?, ?, ?)",
            [(n, p, img, i) for i, (n, p, img) in enumerate(FALLBACK_MENU)])

    # 第一次啟動時，載入本店預設的特色飲品（只做一次，店長之後刪除也不會再出現）
    seeded = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
    if not seeded:
        try:
            with open(SEED_FILE, encoding="utf-8") as f:
                seeds = json.load(f).get(STORE_ID, [])
        except (OSError, ValueError):
            seeds = []
        conn.executemany(
            "INSERT OR IGNORE INTO specials(name, price, description, image) VALUES (?, ?, ?, ?)",
            [(s["name"], s["price"], s.get("description", ""), s.get("image", ""))
             for s in seeds])
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded', '1')")
    conn.commit()
    conn.close()


init_db()


# ------------------------------------------------------- 與總部同步 ----
def hq_request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        HQ_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-API-Key": HQ_API_KEY})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


sync_lock = threading.Lock()


def sync_once():
    if not sync_lock.acquire(blocking=False):  # 已經有同步在進行
        return
    conn = connect()
    try:
        # 1) 向總部報到（新分店第一次報到就會自動出現在總部）
        hq_request("POST", "/api/stores/register", {
            "store_id": STORE_ID, "name": STORE_NAME,
            "city": STORE_CITY, "url": STORE_PUBLIC_URL})

        # 2) 拉取最新共用菜單
        menu = hq_request("GET", "/api/menu")
        conn.execute("DELETE FROM common_menu")
        conn.executemany(
            "INSERT INTO common_menu(name, price, description, image, sort) VALUES (?, ?, ?, ?, ?)",
            [(m["name"], m["price"], m.get("description") or "", m.get("image") or "", i)
             for i, m in enumerate(menu)])
        conn.commit()

        # 3) 回報尚未同步的訂單
        pending = conn.execute(
            "SELECT * FROM orders WHERE synced=0 ORDER BY created_at LIMIT 200").fetchall()
        if pending:
            payload = []
            for o in pending:
                items = conn.execute(
                    "SELECT name, kind, qty, price FROM order_items WHERE order_no=?",
                    (o["order_no"],)).fetchall()
                payload.append({"order_no": o["order_no"], "total": o["total"],
                                "created_at": o["created_at"],
                                "items": [dict(i) for i in items]})
            res = hq_request("POST", "/api/orders",
                             {"store_id": STORE_ID, "orders": payload})
            conn.executemany("UPDATE orders SET synced=1 WHERE order_no=?",
                             [(n,) for n in res.get("accepted", [])])
            conn.commit()

        sync_state.update(hq_online=True, last_error=None,
                          last_sync=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:  # 總部斷線時不影響分店營業
        sync_state.update(hq_online=False, last_error=str(exc))
    finally:
        conn.close()
        sync_lock.release()


def sync_loop():
    while True:
        sync_once()
        time.sleep(SYNC_INTERVAL)


if os.environ.get("DISABLE_SYNC") != "1":
    threading.Thread(target=sync_loop, daemon=True).start()


# ------------------------------------------------------------ helpers ----
def full_menu(db):
    common = db.execute(
        "SELECT name, price, description, image FROM common_menu ORDER BY sort").fetchall()
    specials = db.execute(
        "SELECT name, price, description, image FROM specials WHERE active=1 ORDER BY id"
    ).fetchall()
    return common, specials


def require_manager(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("manager"):
            return redirect(url_for("manager_login"))
        return f(*args, **kwargs)
    return wrapper


def image_url(filename):
    """先找店長上傳的圖，再找映像檔內建的產品圖；都沒有就回傳 None（顯示預設色塊）"""
    if not filename:
        return None
    filename = os.path.basename(filename)
    if os.path.isfile(os.path.join(UPLOAD_DIR, filename)):
        return url_for("uploaded_file", filename=filename)
    if os.path.isfile(os.path.join(app.static_folder, "drinks", filename)):
        return url_for("static", filename="drinks/" + filename)
    return None


def save_upload(file_storage):
    """儲存店長上傳的產品圖，回傳檔名；沒有上傳則回傳 None"""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("圖片格式僅支援 png / jpg / webp / gif")
    name = f"{STORE_ID}_{uuid.uuid4().hex[:10]}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, name))
    return name


def remove_upload(filename):
    if filename and os.path.isfile(os.path.join(UPLOAD_DIR, filename)):
        os.remove(os.path.join(UPLOAD_DIR, filename))


@app.context_processor
def inject_store():
    return {"store": {"id": STORE_ID, "name": STORE_NAME, "city": STORE_CITY},
            "sync": sync_state, "brand": BRAND_NAME, "brand_site": BRAND_SITE_URL,
            "image_url": image_url}


@app.template_filter("money")
def money(v):
    return f"${int(v or 0):,}"


# ------------------------------------------------------------ 前台 ----
@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "store", "store_id": STORE_ID}


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename, max_age=86400)


@app.get("/")
def index():
    common, specials = full_menu(get_db())
    return render_template("index.html", common=common, specials=specials)


@app.post("/order")
def place_order():
    db = get_db()
    common, specials = full_menu(db)
    # 價格一律以伺服器端菜單為準，不信任前端送來的金額
    price_map = {r["name"]: (r["price"], "common") for r in common}
    price_map.update({r["name"]: (r["price"], "special") for r in specials})

    items = []
    for name, (price, kind) in price_map.items():
        try:
            qty = int(request.form.get(f"qty_{name}", 0) or 0)
        except ValueError:
            qty = 0
        if 0 < qty <= 99:
            items.append((name, kind, qty, price))
    if not items:
        flash("請至少選擇一杯飲料")
        return redirect(url_for("index"))

    now = datetime.now()
    order_no = f"{STORE_ID}-{now:%Y%m%d%H%M%S}-{random.randint(1000, 9999)}"
    total = sum(q * p for _, _, q, p in items)
    db.execute("INSERT INTO orders(order_no, total, created_at) VALUES (?, ?, ?)",
               (order_no, total, now.strftime("%Y-%m-%d %H:%M:%S")))
    db.executemany(
        "INSERT INTO order_items(order_no, name, kind, qty, price) VALUES (?, ?, ?, ?, ?)",
        [(order_no, *i) for i in items])
    db.commit()
    # 立即嘗試回報總部（失敗也沒關係，背景同步會補傳）
    threading.Thread(target=sync_once, daemon=True).start()
    return redirect(url_for("receipt", order_no=order_no))


@app.get("/order/<order_no>")
def receipt(order_no):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if not order:
        abort(404)
    items = db.execute("SELECT * FROM order_items WHERE order_no=?", (order_no,)).fetchall()
    return render_template("receipt.html", order=order, items=items)


# ------------------------------------------------------- 店長後台 ----
@app.route("/manager/login", methods=["GET", "POST"])
def manager_login():
    if request.method == "POST":
        if (request.form.get("username") == MANAGER_USER
                and request.form.get("password") == MANAGER_PASSWORD):
            session["manager"] = True
            return redirect(url_for("manager_dashboard"))
        flash("帳號或密碼錯誤")
    return render_template("login.html")


@app.get("/manager/logout")
def manager_logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/manager")
@require_manager
def manager_dashboard():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    stats = db.execute(
        """SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS revenue,
                  COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN total END),0) AS today,
                  COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN 1 END),0) AS today_orders,
                  COALESCE(SUM(CASE WHEN synced=0 THEN 1 END),0) AS unsynced
           FROM orders""", (today, today)).fetchone()
    items = db.execute(
        """SELECT name, kind, SUM(qty) AS qty, SUM(qty*price) AS revenue
           FROM order_items GROUP BY name, kind ORDER BY revenue DESC""").fetchall()
    rows = {r["day"]: r for r in db.execute(
        """SELECT substr(created_at,1,10) AS day, COUNT(*) AS orders, SUM(total) AS revenue
           FROM orders GROUP BY day ORDER BY day DESC LIMIT 14""")}
    daily = []
    for i in range(13, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        r = rows.get(day)
        daily.append({"day": day, "label": day[5:].replace("-", "/"),
                      "orders": r["orders"] if r else 0,
                      "revenue": r["revenue"] if r else 0})
    recent = db.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT 15").fetchall()
    specials = db.execute("SELECT * FROM specials ORDER BY id").fetchall()
    avg_ticket = round(stats["revenue"] / stats["orders"]) if stats["orders"] else 0
    return render_template("manager.html", stats=stats, items=items, daily=daily,
                           recent=recent, specials=specials, avg_ticket=avg_ticket)


@app.post("/manager/specials")
@require_manager
def manage_specials():
    db = get_db()
    action = request.form.get("action")
    try:
        if action == "add":
            name = request.form["name"].strip()
            price = int(request.form["price"])
            if not name or price <= 0:
                raise ValueError("請輸入品項名稱與正確的價格")
            if db.execute("SELECT 1 FROM common_menu WHERE name=?", (name,)).fetchone():
                flash("此名稱與總部共用品項重複，請換一個名稱")
                return redirect(url_for("manager_dashboard"))
            image = save_upload(request.files.get("image")) or ""
            db.execute(
                "INSERT INTO specials(name, price, description, image) VALUES (?, ?, ?, ?)",
                (name, price, request.form.get("description", "").strip(), image))
            flash(f"已新增本店特色飲品：{name}")
        elif action == "update":
            sid = int(request.form["id"])
            price = int(request.form["price"])
            if price <= 0:
                raise ValueError("價格必須大於 0")
            db.execute("UPDATE specials SET price=?, description=? WHERE id=?",
                       (price, request.form.get("description", "").strip(), sid))
            new_image = save_upload(request.files.get("image"))
            if new_image:
                old = db.execute("SELECT image FROM specials WHERE id=?", (sid,)).fetchone()
                if old:
                    remove_upload(old["image"])
                db.execute("UPDATE specials SET image=? WHERE id=?", (new_image, sid))
            flash("特色飲品已更新")
        elif action == "toggle":
            db.execute("UPDATE specials SET active = 1 - active WHERE id=?",
                       (int(request.form["id"]),))
            flash("品項狀態已切換")
        elif action == "delete":
            sid = int(request.form["id"])
            old = db.execute("SELECT image FROM specials WHERE id=?", (sid,)).fetchone()
            if old:
                remove_upload(old["image"])
            db.execute("DELETE FROM specials WHERE id=?", (sid,))
            flash("品項已刪除")
        db.commit()
    except sqlite3.IntegrityError:
        flash("品項名稱已存在")
    except ValueError as exc:
        flash(str(exc) if str(exc) and not str(exc).startswith("invalid literal")
              else "輸入資料有誤")
    except KeyError:
        flash("輸入資料有誤")
    return redirect(url_for("manager_dashboard"))


@app.errorhandler(413)
def too_large(_e):
    flash("圖片檔案太大（上限 5MB）")
    return redirect(url_for("manager_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
