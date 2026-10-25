"""RT Workshop - service tickets (Python + PostgreSQL via psycopg).

Install: install.exe (Python, PostgreSQL and psycopg go into tools/, data into data/). Run: start.bat
On first run the page asks for the first admin's username and password.
Old SQLite data (data/workshop.db) is copied into PostgreSQL once, automatically.
"""
import base64, csv, datetime, decimal, hashlib, io, json, os, queue, secrets, socket, sqlite3, subprocess, sys, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
PHOTO_DIR = os.path.join(DATA, "photos")
BACKUP_DIR = os.path.join(DATA, "backups")
SQLITE_PATH = os.path.join(DATA, "workshop.db")
PG_BIN = os.path.join(BASE, "tools", "pgsql", "bin")
STATIC = os.path.join(BASE, "static")
MAX_PHOTO = 8 * 1024 * 1024
SESSION_DAYS = 14
KEEP_BACKUPS = 30

STATUSES = ["received", "assigned", "in_repair", "waiting_customer", "waiting_parts", "repaired", "delivered", "rejected"]
OPEN_STATUSES = ["received", "assigned", "in_repair", "waiting_customer", "waiting_parts"]
INTAKE_TYPES = ["setup", "repair", "warranty", "inspection", "maintenance"]
ROLES = ["admin", "manager", "technician"]
PAY_METHODS = ["cash", "card", "transfer", "wallet"]
SETTING_KEYS = {"printer_ip": 100, "company_name": 100, "company_sub": 150, "company_phone": 100,
                "company_address": 300, "receipt_terms": 3000, "warranty_days": 10, "due_days": 10,
                "currency": 20, "wa_template_ready": 1000, "idle_minutes": 4}
DEFAULT_SETTINGS = {"company_name": "RT — قسم الصيانة", "company_sub": "مركز صيانة الأجهزة", "warranty_days": "30",
                    "due_days": "3", "currency": "ج.م", "idle_minutes": "3",
                    "wa_template_ready": "أهلاً {name}، جهازك ({device}) رقم تذكرة #{id} جاهز للاستلام من RT. الإجمالي: {total}",
                    "receipt_terms": "\n".join([
                        "الإيصال ده لازم يتقدّم وقت استلام الجهاز، ومش هيتسلّم الجهاز من غيره إلا بإثبات شخصية صاحبه.",
                        "الجهاز اتفحص من برّه بس وقت الاستلام، والعطل الحقيقي بيتحدد بعد الفحص الفني.",
                        "أي تكلفة صيانة أو قطع غيار هنبلّغ العميل بيها وناخد موافقته قبل ما نبدأ.",
                        "الشركة مش مسؤولة عن البيانات اللي على الجهاز، ولازم العميل ياخد نسخة احتياطية منها.",
                        "الشركة مش مسؤولة عن الجهاز لو فضل أكتر من 30 يوم من تاريخ إبلاغ العميل إنه جاهز."])}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  role TEXT NOT NULL, pw_hash TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS customers(id SERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS devices(id SERIAL PRIMARY KEY, serial TEXT NOT NULL, serial_key TEXT NOT NULL UNIQUE,
  device_type TEXT, brand TEXT, model TEXT, customer_id INTEGER REFERENCES customers(id),
  first_seen_at TIMESTAMP NOT NULL, first_intake_type TEXT NOT NULL, notes TEXT);
CREATE TABLE IF NOT EXISTS tickets(id SERIAL PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  serial TEXT NOT NULL, device_type TEXT, brand TEXT, model TEXT, accessories TEXT, complaint TEXT,
  status TEXT NOT NULL, received_by INTEGER NOT NULL REFERENCES users(id), assigned_to INTEGER REFERENCES users(id),
  received_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, closed_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS work_notes(id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, text TEXT NOT NULL, created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS parts(id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  name TEXT NOT NULL, serial TEXT, price NUMERIC(12,2) NOT NULL DEFAULT 0, qty INTEGER NOT NULL DEFAULT 1,
  user_id INTEGER NOT NULL REFERENCES users(id), created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS photos(id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  stage TEXT NOT NULL, file TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id), created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS history(id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, detail TEXT, created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS payments(id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  amount NUMERIC(12,2) NOT NULL, method TEXT NOT NULL, note TEXT, user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

# New columns go here (never edit the CREATE TABLEs above for existing databases).
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone2 TEXT",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address TEXT",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS company TEXT",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP(0)",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS device_id INTEGER REFERENCES devices(id)",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS intake_type TEXT NOT NULL DEFAULT 'repair'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS first_visit BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'normal'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS condition TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS password_note TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS due_at TIMESTAMP",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS diagnosis TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS estimate NUMERIC(12,2)",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS approval TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS approval_at TIMESTAMP",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS approval_note TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS labor NUMERIC(12,2) NOT NULL DEFAULT 0",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS discount NUMERIC(12,2) NOT NULL DEFAULT 0",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS warranty_days INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS warranty_until DATE",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS delivered_to TEXT",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS parent_ticket_id INTEGER REFERENCES tickets(id)",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS under_warranty BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_tickets_serial ON tickets(upper(serial))",
    "CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_assigned ON tickets(assigned_to)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_device ON tickets(device_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_customer ON tickets(customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_received ON tickets(received_at)",
    "CREATE INDEX IF NOT EXISTS ix_customers_phone ON customers(phone)",
    "CREATE INDEX IF NOT EXISTS ix_history_ticket ON history(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_parts_ticket ON parts(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_notes_ticket ON work_notes(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_photos_ticket ON photos(ticket_id)",
    "CREATE INDEX IF NOT EXISTS ix_payments_ticket ON payments(ticket_id)",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_pw BOOLEAN NOT NULL DEFAULT FALSE",
    """CREATE TABLE IF NOT EXISTS audit_log(id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
       action TEXT NOT NULL, detail TEXT, created_at TIMESTAMP NOT NULL)""",
]


def load_config():
    cfg = {"db_host": "127.0.0.1", "db_port": 5440, "db_user": "postgres", "db_password": "", "db_name": "workshop"}
    p = os.path.join(DATA, "config.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
PORT = int(os.environ.get("PORT") or CFG.get("app_port") or 8085)
HOST = os.environ.get("HOST") or CFG.get("app_host") or "127.0.0.1"
DSN = os.environ.get("DATABASE_URL") or (
    f"host={CFG['db_host']} port={CFG['db_port']} user={CFG['db_user']} password={CFG['db_password']} dbname={CFG['db_name']}")


class Pool:
    """Tiny connection pool: reuse open connections instead of reconnecting on every request."""
    def __init__(self, size=10):
        self.q = queue.LifoQueue(size)

    def get(self):
        try:
            con = self.q.get_nowait()
            if not con.closed:
                return con
        except queue.Empty:
            pass
        return psycopg.connect(DSN, row_factory=dict_row)

    def put(self, con):
        if con.closed or con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            con.close()
            return
        try:
            self.q.put_nowait(con)
        except queue.Full:
            con.close()


POOL = Pool()


class DB:
    """One transaction per request; commits on success, rolls back on any error."""
    def __enter__(self):
        self.con = POOL.get()
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.con.rollback()
        else:
            self.con.commit()
        POOL.put(self.con)

    def all(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def val(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return next(iter(r.values())) if r else None

    def run(self, sql, args=()):
        return self.con.execute(sql, args)


def now():
    return datetime.datetime.now().replace(microsecond=0)


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with DB() as d:
        d.run(SCHEMA)
        for m in MIGRATIONS:
            d.run(m)
        for k, v in DEFAULT_SETTINGS.items():
            d.run("INSERT INTO settings VALUES(%s,%s) ON CONFLICT DO NOTHING", (k, v))
        migrate_sqlite(d)
        d.run("UPDATE users SET role='technician' WHERE role='reception'")
        # accounts still on the old default password must change it at next sign-in
        for u in d.all("SELECT id, pw_hash FROM users WHERE NOT must_change_pw"):
            if check_pw("admin", u["pw_hash"]):
                d.run("UPDATE users SET must_change_pw=TRUE WHERE id=%s", (u["id"],))
        d.run("DELETE FROM sessions WHERE created < %s", (time.time() - SESSION_DAYS * 86400,))


def migrate_sqlite(d):
    """Copy the old SQLite database once (same ids), then build the device registry from it."""
    if not os.path.isfile(SQLITE_PATH) or d.one("SELECT 1 FROM settings WHERE key='sqlite_migrated'"):
        return
    if d.one("SELECT 1 FROM tickets"):
        d.run("INSERT INTO settings VALUES('sqlite_migrated','skipped: postgres already had tickets')")
        return
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    tables = [("users", "id,username,full_name,role,pw_hash,active"), ("customers", "id,name,phone,notes"),
              ("tickets", "id,customer_id,serial,device_type,brand,model,accessories,complaint,status,received_by,assigned_to,received_at,updated_at,closed_at"),
              ("work_notes", "id,ticket_id,user_id,kind,text,created_at"), ("parts", "id,ticket_id,name,serial,price,qty,user_id,created_at"),
              ("photos", "id,ticket_id,stage,file,user_id,created_at"), ("history", "id,ticket_id,user_id,action,detail,created_at")]
    d.run("DELETE FROM sessions")
    d.run("DELETE FROM users")
    counts = {}
    for table, cols in tables:
        rs = src.execute(f"SELECT {cols} FROM {table} ORDER BY id").fetchall()
        n = len(cols.split(","))
        for r in rs:
            vals = [bool(v) if table == "users" and k == "active" else v for k, v in zip(cols.split(","), r)]
            d.run(f"INSERT INTO {table}({cols}) VALUES({','.join(['%s'] * n)})", vals)
        d.run(f"SELECT setval(pg_get_serial_sequence('{table}','id'), GREATEST((SELECT MAX(id) FROM {table}), 1))")
        counts[table] = len(rs)
    for r in src.execute("SELECT key,value FROM settings").fetchall():
        d.run("INSERT INTO settings VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (r["key"], r["value"]))
    src.close()
    for t in d.all("SELECT * FROM tickets ORDER BY id"):
        dev = upsert_device(d, t["serial"], t, t["customer_id"], "repair", t["received_at"])
        d.run("UPDATE tickets SET device_id=%s, first_visit=%s WHERE id=%s", (dev["id"], dev["_new"], t["id"]))
    d.run("INSERT INTO settings VALUES('sqlite_migrated',%s)", (json.dumps(counts),))
    print("Copied old SQLite data into PostgreSQL:", counts)


def serial_key(s):
    return "".join(str(s).split()).upper()


def upsert_device(d, serial, info, customer_id, intake_type, seen_at):
    key = serial_key(serial)
    dev = d.one("SELECT * FROM devices WHERE serial_key=%s", (key,))
    if dev:
        d.run("""UPDATE devices SET device_type=COALESCE(NULLIF(%s,''),device_type), brand=COALESCE(NULLIF(%s,''),brand),
                 model=COALESCE(NULLIF(%s,''),model), customer_id=%s WHERE id=%s""",
              (info.get("device_type") or "", info.get("brand") or "", info.get("model") or "", customer_id, dev["id"]))
        dev["_new"] = False
        return dev
    dev = d.one("""INSERT INTO devices(serial,serial_key,device_type,brand,model,customer_id,first_seen_at,first_intake_type)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (serial.strip(), key, info.get("device_type"), info.get("brand"), info.get("model"), customer_id, seen_at, intake_type))
    dev["_new"] = True
    return dev


class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def need(cond, status, msg):
    if not cond:
        raise ApiError(status, msg)


def text(body, key, required=False, maxlen=500):
    v = body.get(key)
    v = "" if v is None else str(v).strip()
    need(not required or v, 400, f"missing:{key}")
    need(len(v) <= maxlen, 400, f"too_long:{key}")
    return v


def money(body, key):
    try:
        v = decimal.Decimal(str(body.get(key) or 0)).quantize(decimal.Decimal("0.01"))
    except decimal.InvalidOperation:
        raise ApiError(400, "bad_number")
    need(0 <= v < 10_000_000, 400, "bad_number")
    return v


def setting(d, key):
    return d.val("SELECT value FROM settings WHERE key=%s", (key,)) or ""


TICKET_SELECT = """SELECT t.*, c.name customer_name, c.phone customer_phone, c.phone2 customer_phone2,
    ur.full_name received_by_name, ua.full_name assigned_to_name,
    (t.due_at < LOCALTIMESTAMP AND t.status = ANY(%(open)s)) overdue
    FROM tickets t JOIN customers c ON c.id=t.customer_id JOIN users ur ON ur.id=t.received_by
    LEFT JOIN users ua ON ua.id=t.assigned_to"""


def idle_seconds(d):
    try:
        return max(1, int(setting(d, "idle_minutes") or 3)) * 60
    except ValueError:
        return 180


def start_session(d, uid):
    token = secrets.token_urlsafe(32)
    d.run("INSERT INTO sessions(token,user_id,created,last_seen) VALUES(%s,%s,%s,%s)", (token, uid, time.time(), time.time()))
    return {"Set-Cookie": f"sid={token}; HttpOnly; Path=/; SameSite=Strict"}


def delete_ticket(d, t):
    """Remove a ticket and everything under it; keep the device registry consistent."""
    tid = t["id"]
    files = [r["file"] for r in d.all("SELECT file FROM photos WHERE ticket_id=%s", (tid,))]
    for table in ("payments", "history", "parts", "work_notes", "photos"):
        d.run(f"DELETE FROM {table} WHERE ticket_id=%s", (tid,))
    d.run("UPDATE tickets SET parent_ticket_id=NULL WHERE parent_ticket_id=%s", (tid,))
    d.run("DELETE FROM tickets WHERE id=%s", (tid,))
    if t["device_id"]:
        first = d.one("SELECT id, received_at, intake_type FROM tickets WHERE device_id=%s ORDER BY received_at, id LIMIT 1", (t["device_id"],))
        if first:
            d.run("UPDATE devices SET first_seen_at=%s, first_intake_type=%s WHERE id=%s",
                  (first["received_at"], first["intake_type"], t["device_id"]))
            d.run("UPDATE tickets SET first_visit=(id=%s) WHERE device_id=%s", (first["id"], t["device_id"]))
        else:
            d.run("DELETE FROM devices WHERE id=%s", (t["device_id"],))
    return files


def ticket_view(d, tid):
    return d.one(TICKET_SELECT + " WHERE t.id=%(id)s", {"open": OPEN_STATUSES, "id": tid})


def ticket_money(d, t):
    parts = d.val("SELECT COALESCE(SUM(price*qty),0) FROM parts WHERE ticket_id=%s", (t["id"],))
    paid = d.val("SELECT COALESCE(SUM(amount),0) FROM payments WHERE ticket_id=%s", (t["id"],))
    total = parts + t["labor"] - t["discount"]
    return {"parts_total": parts, "total": total, "paid": paid, "balance": total - paid}


def redact_ticket_finances(t):
    for key in ("estimate", "labor", "discount", "parts_total", "total", "paid", "balance", "payments"):
        t.pop(key, None)
    for part in t.get("parts", []):
        part.pop("price", None)
    for entry in t.get("history", []):
        if entry.get("action") in ("diagnosis", "charges", "payment"):
            entry["detail"] = ""
    return t


def log(d, tid, uid, action, detail=""):
    d.run("INSERT INTO history(ticket_id,user_id,action,detail,created_at) VALUES(%s,%s,%s,%s,%s)",
          (tid, uid, action, detail, now()))
    d.run("UPDATE tickets SET updated_at=%s WHERE id=%s", (now(), tid))


def can_work(user, t):
    return user["role"] in ("admin", "manager") or (user["role"] == "technician" and t["assigned_to"] == user["id"])


def save_photo(d, tid, uid, stage, data_url):
    need(isinstance(data_url, str) and data_url.startswith("data:image/") and "," in data_url, 400, "bad_photo")
    head, b64 = data_url.split(",", 1)
    ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}.get(head[11:].split(";")[0])
    need(ext, 400, "bad_photo_type")
    raw = base64.b64decode(b64, validate=True)
    need(len(raw) <= MAX_PHOTO, 400, "photo_too_large")
    name = f"{tid}_{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(PHOTO_DIR, name), "wb") as f:
        f.write(raw)
    d.run("INSERT INTO photos(ticket_id,stage,file,user_id,created_at) VALUES(%s,%s,%s,%s,%s)",
          (tid, stage, name, uid, now()))


def zpl_label(t):
    def clean(s):
        return (s or "").replace("^", " ").replace("~", " ")[:40]
    # 50x30 mm at 203 dpi = 400 x 240 dots. ^CI28 = UTF-8 (Arabic needs a Unicode font on the printer).
    return ("^XA^CI28^PW400^LL240"
            f"^FO15,12^A0N,30,30^FD#{t['id']}^FS"
            f"^FO15,45^BCN,50,N,N,N^FD{clean(t['serial'])}^FS"
            f"^FO15,100^A0N,22,22^FDSN: {clean(t['serial'])}^FS"
            f"^FO15,128^A0N,22,22^FD{clean(t['customer_name'])}^FS"
            f"^FO15,156^A0N,22,22^FDIN: {str(t['received_at'])[:16]}^FS"
            f"^FO15,184^A0N,22,22^FDTECH: {clean(t['assigned_to_name']) or '-'}^FS"
            "^XZ")


# ---------- backups (pg_dump custom format, kept in data/backups)
BACKUP_LOCK = threading.Lock()


def make_backup(tag="auto"):
    with BACKUP_LOCK:
        name = f"workshop_{time.strftime('%Y%m%d_%H%M%S')}_{tag}.dump"
        path = os.path.join(BACKUP_DIR, name)
        env = dict(os.environ, PGPASSWORD=str(CFG["db_password"]))
        exe = os.path.join(PG_BIN, "pg_dump.exe")
        r = subprocess.run([exe if os.path.isfile(exe) else "pg_dump", "-h", str(CFG["db_host"]), "-p", str(CFG["db_port"]),
                            "-U", CFG["db_user"], "-Fc", "-f", path, CFG["db_name"]], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300])
        old = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".dump"))
        for f in old[:-KEEP_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, f))
        return name


def backup_loop():
    while True:
        today = time.strftime("%Y%m%d")
        if not any(f.startswith(f"workshop_{today}") for f in os.listdir(BACKUP_DIR)):
            try:
                print("Daily backup:", make_backup())
            except Exception as e:
                print("Backup failed:", e)
        time.sleep(3600)


def to_json(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, datetime.date):
        return o.isoformat()
    raise TypeError(type(o))


def parse_date(s, end=False):
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "bad_date")
    return d + datetime.timedelta(days=1) if end else d


class Handler(BaseHTTPRequestHandler):
    server_version = "RTWorkshop/2.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, obj, headers=None):
        data = json.dumps(obj, ensure_ascii=False, default=to_json).encode()
        self.send_bytes(status, data, "application/json; charset=utf-8", headers)

    def send_bytes(self, status, data, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            self.send_bytes(200, f.read(), ctype)

    def current_user(self, d):
        cookie = self.headers.get("Cookie", "")
        token = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("sid=")), None)
        if not token:
            return None
        t = time.time()
        u = d.one("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                     WHERE s.token=%s AND u.active AND s.created > %s AND COALESCE(s.last_seen, s.created) > %s""",
                  (token, t - SESSION_DAYS * 86400, t - idle_seconds(d)))
        if u:
            d.run("UPDATE sessions SET last_seen=%s WHERE token=%s", (t, token))
        else:
            d.run("DELETE FROM sessions WHERE token=%s", (token,))
        return u

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path
        try:
            if method == "GET" and not path.startswith("/api/"):
                return self.static(path)
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                need(n <= 60 * 1024 * 1024, 413, "too_large")
                body = json.loads(self.rfile.read(n) or b"{}")
                need(isinstance(body, dict), 400, "bad_body")
            with DB() as d:
                self.api(d, method, path, {k: v[0] for k, v in parse_qs(url.query).items()}, body)
        except ApiError as e:
            self.send_json(e.status, {"error": e.msg})
        except (ValueError, TypeError, json.JSONDecodeError, psycopg.errors.DataError):
            self.send_json(400, {"error": "bad_request"})
        except psycopg.OperationalError:
            self.send_json(503, {"error": "db_down"})

    def static(self, path):
        if path.startswith("/photos/"):
            name = os.path.basename(path)
            with DB() as d:
                need(self.current_user(d), 401, "login")
            p = os.path.join(PHOTO_DIR, name)
            need(os.path.isfile(p), 404, "not_found")
            ext = name.rsplit(".", 1)[-1]
            return self.send_file(p, {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext])
        name = "index.html" if path in ("/", "") else os.path.basename(path)
        p = os.path.join(STATIC, name)
        need(os.path.isfile(p), 404, "not_found")
        ext = name.rsplit(".", 1)[-1]
        if ext in ("jpg", "png"):
            return self.send_file(p, {"jpg": "image/jpeg", "png": "image/png"}[ext])
        ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css"}.get(ext, "text/plain")
        self.send_file(p, ctype + "; charset=utf-8")

    def api(self, d, method, path, q, body):
        parts = path.strip("/").split("/")[1:]  # drop "api"
        if parts == ["login"] and method == "POST":
            u = d.one("SELECT * FROM users WHERE username=%s AND active", (text(body, "username"),))
            need(u and check_pw(str(body.get("password", "")), u["pw_hash"]), 401, "bad_login")
            return self.send_json(200, {"ok": True}, start_session(d, u["id"]))
        if parts == ["company"]:  # shown on the login page, before sign-in
            res = {k: setting(d, k) for k in ("company_name", "company_sub")}
            res["needs_setup"] = not d.one("SELECT 1 FROM users")
            return self.send_json(200, res)
        if parts == ["setup"] and method == "POST":  # first run only: create the first admin
            d.run("LOCK TABLE users IN EXCLUSIVE MODE")
            need(not d.one("SELECT 1 FROM users"), 409, "already_setup")
            pw = str(body.get("password", ""))
            need(len(pw) >= 6, 400, "short_password_admin")
            uid = d.val("INSERT INTO users(username,full_name,role,pw_hash,phone) VALUES(%s,%s,'admin',%s,%s) RETURNING id",
                        (text(body, "username", True, 50), text(body, "full_name", True), hash_pw(pw), text(body, "phone")))
            for k in ("company_name", "company_sub", "company_phone", "company_address"):
                if text(body, k):
                    d.run("UPDATE settings SET value=%s WHERE key=%s", (text(body, k, False, SETTING_KEYS[k]), k))
            return self.send_json(200, {"ok": True}, start_session(d, uid))

        user = self.current_user(d)
        need(user, 401, "login")
        role = user["role"]
        is_mgr = role in ("admin", "manager")
        is_front = is_mgr
        can_receive = is_mgr or role == "technician"

        if parts == ["logout"]:
            d.run("DELETE FROM sessions WHERE user_id=%s", (user["id"],))
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
        if parts == ["me"]:
            me = {k: user[k] for k in ("id", "username", "full_name", "role", "must_change_pw")}
            me["idle_seconds"] = idle_seconds(d)
            return self.send_json(200, me)
        if parts == ["ping"]:  # the page calls this while the user is active, to keep the session alive
            return self.send_json(200, {"ok": True})
        if parts == ["me", "password"] and method == "POST":
            need(check_pw(str(body.get("old", "")), user["pw_hash"]), 400, "bad_old_password")
            new = str(body.get("new", ""))
            need(len(new) >= (6 if role == "admin" else 4), 400, "short_password_admin" if role == "admin" else "short_password")
            need(new != str(body.get("old", "")), 400, "same_password")
            d.run("UPDATE users SET pw_hash=%s, must_change_pw=FALSE WHERE id=%s", (hash_pw(new), user["id"]))
            return self.send_json(200, {"ok": True})
        need(not user["must_change_pw"], 403, "must_change_pw")

        if parts == ["audit"]:
            need(role == "admin", 403, "forbidden")
            return self.send_json(200, d.all("""SELECT a.*, u.full_name user_name FROM audit_log a JOIN users u ON u.id=a.user_id
                                                ORDER BY a.id DESC LIMIT 50"""))

        # ---- users
        if parts == ["users"] and method == "GET":
            return self.send_json(200, d.all("SELECT id,username,full_name,role,active,phone FROM users ORDER BY full_name"))
        if parts == ["users"] and method == "POST":
            need(role == "admin", 403, "forbidden")
            r = text(body, "role", True)
            need(r in ROLES, 400, "bad_role")
            pw = str(body.get("password", ""))
            uid = body.get("id")
            active = bool(body.get("active", True))
            if uid:
                uid = int(uid)
                need(uid != user["id"] or (active and r == "admin"), 400, "cant_demote_self")
                d.run("UPDATE users SET full_name=%s, role=%s, active=%s, phone=%s WHERE id=%s",
                      (text(body, "full_name", True), r, active, text(body, "phone"), uid))
                if pw:
                    need(len(pw) >= 4, 400, "short_password")
                    d.run("UPDATE users SET pw_hash=%s WHERE id=%s", (hash_pw(pw), uid))
                if not active:
                    d.run("DELETE FROM sessions WHERE user_id=%s", (uid,))
            else:
                need(len(pw) >= 4, 400, "short_password")
                need(not d.one("SELECT 1 FROM users WHERE lower(username)=lower(%s)", (text(body, "username"),)), 409, "username_taken")
                d.run("INSERT INTO users(username,full_name,role,pw_hash,phone) VALUES(%s,%s,%s,%s,%s)",
                      (text(body, "username", True, 50), text(body, "full_name", True), r, hash_pw(pw), text(body, "phone")))
            return self.send_json(200, {"ok": True})

        # ---- settings
        if parts == ["settings"]:
            if method == "POST":
                need(role == "admin", 403, "forbidden")
                for k, maxlen in SETTING_KEYS.items():
                    if k in body:
                        d.run("INSERT INTO settings VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                              (k, text(body, k, False, maxlen)))
            return self.send_json(200, {k: setting(d, k) for k in SETTING_KEYS})

        # ---- backups
        if parts == ["backups"]:
            need(role == "admin", 403, "forbidden")
            if method == "POST":
                try:
                    make_backup("manual")
                except Exception:
                    raise ApiError(500, "backup_failed")
            files = sorted((f for f in os.listdir(BACKUP_DIR) if f.endswith(".dump")), reverse=True)
            return self.send_json(200, {"dir": BACKUP_DIR, "files": [
                {"name": f, "size": os.path.getsize(os.path.join(BACKUP_DIR, f))} for f in files]})

        # ---- autocomplete for intake/parts fields
        if parts == ["suggest"]:
            col = {"device_type": "SELECT device_type v FROM tickets", "brand": "SELECT brand v FROM tickets",
                   "model": "SELECT model v FROM tickets", "part": "SELECT name v FROM parts"}.get(q.get("field"))
            need(col, 400, "bad_field")
            return self.send_json(200, [r["v"] for r in d.all(
                f"SELECT v FROM ({col}) x WHERE v<>'' GROUP BY v ORDER BY COUNT(*) DESC LIMIT 40")])

        # ---- dashboard
        if parts == ["dashboard"]:
            mine = "" if role != "technician" else (
                f" AND (assigned_to={int(user['id'])} OR received_by={int(user['id'])})")
            by_status = {r["status"]: r["n"] for r in d.all(f"SELECT status, COUNT(*) n FROM tickets WHERE TRUE{mine} GROUP BY status")}
            today = datetime.date.today()
            res = {"by_status": by_status,
                   "open": sum(by_status.get(s, 0) for s in OPEN_STATUSES),
                   "overdue": d.val(f"SELECT COUNT(*) FROM tickets WHERE due_at < LOCALTIMESTAMP AND status = ANY(%s){mine}", (OPEN_STATUSES,)),
                   "urgent": d.val(f"SELECT COUNT(*) FROM tickets WHERE priority='urgent' AND status = ANY(%s){mine}", (OPEN_STATUSES,)),
                   "in_today": d.val(f"SELECT COUNT(*) FROM tickets WHERE received_at >= %s{mine}", (today,)),
                   "out_today": d.val(f"SELECT COUNT(*) FROM tickets WHERE status='delivered' AND closed_at >= %s{mine}", (today,)),
                   "waiting_approval": d.val(f"SELECT COUNT(*) FROM tickets WHERE approval='pending' AND status = ANY(%s){mine}", (OPEN_STATUSES,)),
                   "attention": d.all(TICKET_SELECT + f""" WHERE t.status = ANY(%(open)s) AND (t.priority='urgent' OR t.due_at < LOCALTIMESTAMP
                       OR t.approval='pending' OR t.assigned_to IS NULL){mine.replace('assigned_to', 't.assigned_to')}
                       ORDER BY (t.priority='urgent') DESC, t.due_at NULLS LAST LIMIT 12""", {"open": OPEN_STATUSES})}
            if role == "technician":
                res["attention"] = [redact_ticket_finances(t) for t in res["attention"]]
            if role != "technician":
                res["techs"] = d.all("""SELECT u.id, u.full_name, COUNT(t.id) FILTER (WHERE t.status = ANY(%s)) open,
                    COUNT(t.id) FILTER (WHERE t.status IN ('repaired','delivered') AND t.updated_at >= %s) done_month
                    FROM users u LEFT JOIN tickets t ON t.assigned_to=u.id WHERE u.active AND u.role IN ('technician','manager')
                    GROUP BY u.id ORDER BY open DESC, u.full_name""", (OPEN_STATUSES, today.replace(day=1)))
            if is_mgr:
                res["revenue_month"] = d.val("SELECT COALESCE(SUM(amount),0) FROM payments WHERE created_at >= %s", (today.replace(day=1),))
                res["revenue_today"] = d.val("SELECT COALESCE(SUM(amount),0) FROM payments WHERE created_at >= %s", (today,))
            res["activity"] = d.all(f"""SELECT h.*, u.full_name user_name, t.serial FROM history h JOIN users u ON u.id=h.user_id
                JOIN tickets t ON t.id=h.ticket_id WHERE TRUE{mine.replace('assigned_to', 't.assigned_to')} ORDER BY h.id DESC LIMIT 15""")
            if role == "technician":
                for entry in res["activity"]:
                    if entry["action"] in ("diagnosis", "charges", "payment"):
                        entry["detail"] = ""
            return self.send_json(200, res)

        # ---- device file by serial
        if len(parts) == 2 and parts[0] == "devices" and method == "GET":
            need(role != "technician", 403, "forbidden")
            dev = d.one("""SELECT dv.*, c.name customer_name, c.phone customer_phone FROM devices dv
                           LEFT JOIN customers c ON c.id=dv.customer_id WHERE dv.serial_key=%s""", (serial_key(unquote(parts[1])),))
            need(dev, 404, "device_not_found")
            visits = d.all(TICKET_SELECT + " WHERE t.device_id=%(dev)s ORDER BY t.id DESC", {"open": OPEN_STATUSES, "dev": dev["id"]})
            spent = 0
            for v in visits:
                v.update(ticket_money(d, v))
                spent += v["total"] if v["status"] == "delivered" else 0
                v["notes"] = d.all("SELECT kind,text,created_at FROM work_notes WHERE ticket_id=%s ORDER BY id", (v["id"],))
                v["parts"] = d.all("SELECT name,serial,qty,price FROM parts WHERE ticket_id=%s ORDER BY id", (v["id"],))
            delivered = [v for v in visits if v["status"] == "delivered" and v["closed_at"]]
            dev["visits"] = visits
            dev["total_spent"] = spent
            dev["parts_count"] = sum(len(v["parts"]) for v in visits)
            dev["warranty_until"] = max((v["warranty_until"] for v in delivered if v["warranty_until"]), default=None)
            dev["in_warranty"] = bool(dev["warranty_until"] and dev["warranty_until"] >= datetime.date.today())
            dev["open_ticket"] = next((v["id"] for v in visits if v["status"] in OPEN_STATUSES), None)
            return self.send_json(200, dev)

        if parts == ["intake", "device"] and method == "GET":
            need(role == "technician", 403, "forbidden")
            serial = q.get("serial", "").strip()
            need(serial, 400, "missing:serial")
            dev = d.one("""SELECT dv.id, dv.device_type, dv.brand, dv.model, dv.customer_id,
                                  dv.first_seen_at, dv.first_intake_type, c.name customer_name, c.phone customer_phone
                           FROM devices dv LEFT JOIN customers c ON c.id=dv.customer_id
                           WHERE dv.serial_key=%s""", (serial_key(serial),))
            need(dev, 404, "device_not_found")
            recent = d.one("""SELECT status,received_at FROM tickets
                              WHERE device_id=%s ORDER BY id DESC LIMIT 1""", (dev["id"],))
            warranty_until = d.val("""SELECT MAX(warranty_until) FROM tickets
                                      WHERE device_id=%s AND status='delivered'""", (dev["id"],))
            open_ticket = d.one("""SELECT id FROM tickets WHERE device_id=%s AND status = ANY(%s)
                                  AND (received_by=%s OR assigned_to=%s) ORDER BY id DESC LIMIT 1""",
                                (dev["id"], OPEN_STATUSES, user["id"], user["id"]))
            has_open_ticket = bool(d.one("SELECT 1 FROM tickets WHERE device_id=%s AND status = ANY(%s)",
                                         (dev["id"], OPEN_STATUSES)))
            dev["visit_count"] = d.val("SELECT COUNT(*) FROM tickets WHERE device_id=%s", (dev["id"],))
            dev["recent_visit"] = recent
            dev["warranty_until"] = warranty_until
            dev["in_warranty"] = bool(dev["warranty_until"] and dev["warranty_until"] >= datetime.date.today())
            dev["open_ticket"] = open_ticket["id"] if open_ticket else None
            dev["has_open_ticket"] = has_open_ticket
            return self.send_json(200, dev)

        # ---- customers
        if parts == ["customers"] and method == "GET":
            s = f"%{q.get('q', '')}%"
            if role == "technician":
                need(len(q.get("q", "").strip()) >= 2, 400, "search_too_short")
                return self.send_json(200, d.all("""SELECT c.id,c.name,c.phone,c.phone2,c.company,
                    (SELECT COUNT(*) FROM tickets t WHERE t.customer_id=c.id AND t.received_by=%s) tickets
                    FROM customers c WHERE c.name ILIKE %s OR c.phone ILIKE %s OR c.phone2 ILIKE %s OR c.company ILIKE %s
                    ORDER BY c.id DESC LIMIT 20""", (user["id"], s, s, s, s)))
            return self.send_json(200, d.all("""SELECT c.*, (SELECT COUNT(*) FROM tickets t WHERE t.customer_id=c.id) tickets
                FROM customers c WHERE c.name ILIKE %s OR c.phone ILIKE %s OR c.phone2 ILIKE %s OR c.company ILIKE %s
                ORDER BY c.id DESC LIMIT %s""", (s, s, s, s, 20 if q.get("q") else 200)))
        if len(parts) == 2 and parts[0] == "customers":
            need(role != "technician", 403, "forbidden")
            cid = int(parts[1])
            c = d.one("SELECT * FROM customers WHERE id=%s", (cid,))
            need(c, 404, "not_found")
            if method == "POST":
                need(is_front, 403, "forbidden")
                d.run("UPDATE customers SET name=%s, phone=%s, phone2=%s, company=%s, address=%s, notes=%s WHERE id=%s",
                      (text(body, "name", True), text(body, "phone"), text(body, "phone2"), text(body, "company"),
                       text(body, "address"), text(body, "notes", False, 2000), cid))
                return self.send_json(200, {"ok": True})
            c["tickets"] = d.all(TICKET_SELECT + " WHERE t.customer_id=%(c)s ORDER BY t.id DESC", {"open": OPEN_STATUSES, "c": cid})
            for t in c["tickets"]:
                t.update(ticket_money(d, t))
            c["devices"] = d.all("SELECT * FROM devices WHERE customer_id=%s ORDER BY id DESC", (cid,))
            return self.send_json(200, c)

        # ---- reports (managers)
        if parts and parts[0] == "reports":
            need(is_mgr, 403, "forbidden")
            frm = parse_date(q.get("from") or datetime.date.today().replace(day=1).isoformat())
            to = parse_date(q.get("to") or datetime.date.today().isoformat(), end=True)
            if parts == ["reports", "summary"]:
                return self.send_json(200, {
                    "received": d.val("SELECT COUNT(*) FROM tickets WHERE received_at >= %s AND received_at < %s", (frm, to)),
                    "delivered": d.val("SELECT COUNT(*) FROM tickets WHERE status='delivered' AND closed_at >= %s AND closed_at < %s", (frm, to)),
                    "rejected": d.val("SELECT COUNT(*) FROM tickets WHERE status='rejected' AND closed_at >= %s AND closed_at < %s", (frm, to)),
                    "warranty_returns": d.val("SELECT COUNT(*) FROM tickets WHERE under_warranty AND received_at >= %s AND received_at < %s", (frm, to)),
                    "first_setup": d.val("SELECT COUNT(*) FROM tickets WHERE intake_type='setup' AND received_at >= %s AND received_at < %s", (frm, to)),
                    "payments": d.val("SELECT COALESCE(SUM(amount),0) FROM payments WHERE created_at >= %s AND created_at < %s", (frm, to)),
                    "parts_value": d.val("SELECT COALESCE(SUM(price*qty),0) FROM parts WHERE created_at >= %s AND created_at < %s", (frm, to)),
                    "by_type": d.all("SELECT intake_type k, COUNT(*) n FROM tickets WHERE received_at >= %s AND received_at < %s GROUP BY 1 ORDER BY 2 DESC", (frm, to)),
                    "by_brand": d.all("SELECT COALESCE(NULLIF(brand,''),'—') k, COUNT(*) n FROM tickets WHERE received_at >= %s AND received_at < %s GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (frm, to)),
                    "techs": d.all("""SELECT u.full_name, COUNT(t.id) assigned,
                        COUNT(t.id) FILTER (WHERE t.status IN ('repaired','delivered')) done,
                        COUNT(t.id) FILTER (WHERE t.status = ANY(%s)) open,
                        ROUND(AVG(EXTRACT(EPOCH FROM (t.closed_at - t.received_at))/86400) FILTER (WHERE t.status='delivered')::numeric, 1) avg_days,
                        COALESCE(SUM((SELECT SUM(p.price*p.qty) FROM parts p WHERE p.ticket_id=t.id)),0) parts_value,
                        COALESCE(SUM(t.labor),0) labor
                        FROM users u JOIN tickets t ON t.assigned_to=u.id AND t.received_at >= %s AND t.received_at < %s
                        GROUP BY u.id ORDER BY done DESC""", (OPEN_STATUSES, frm, to))})
            if parts == ["reports", "export"]:
                rows = d.all(TICKET_SELECT + " WHERE t.received_at >= %(f)s AND t.received_at < %(t)s ORDER BY t.id",
                             {"open": OPEN_STATUSES, "f": frm, "t": to})
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["رقم التذكرة", "السيريال", "النوع", "الماركة", "الموديل", "نوع الدخول", "العميل", "الموبايل", "الحالة",
                            "الفني", "تاريخ الاستلام", "تاريخ القفل", "قطع الغيار", "المصنعية", "الخصم", "الإجمالي", "المدفوع"])
                for t in rows:
                    m = ticket_money(d, t)
                    w.writerow([t["id"], t["serial"], t["device_type"], t["brand"], t["model"], t["intake_type"], t["customer_name"],
                                t["customer_phone"], t["status"], t["assigned_to_name"] or "", to_json(t["received_at"]),
                                to_json(t["closed_at"]) if t["closed_at"] else "", m["parts_total"], t["labor"], t["discount"], m["total"], m["paid"]])
                return self.send_bytes(200, ("﻿" + out.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                                       {"Content-Disposition": f'attachment; filename="tickets_{q.get("from", "")}_{q.get("to", "")}.csv"'})

        # ---- tickets
        if parts == ["tickets"] and method == "GET":
            sql = TICKET_SELECT + " WHERE TRUE"
            args = {"open": OPEN_STATUSES}
            if role == "technician":
                sql += " AND (t.assigned_to=%(me)s OR t.received_by=%(me)s)"
                args["me"] = user["id"]
            st = q.get("status", "")
            if st == "open":
                sql += " AND t.status = ANY(%(open)s)"
            elif st:
                sql += " AND t.status=%(st)s"
                args["st"] = st
            for key, col in (("type", "t.intake_type"), ("priority", "t.priority")):
                if q.get(key):
                    sql += f" AND {col}=%({key})s"
                    args[key] = q[key]
            if q.get("tech"):
                sql += " AND t.assigned_to IS NULL" if q["tech"] == "none" else " AND t.assigned_to=%(tech)s"
                args["tech"] = int(q["tech"]) if q["tech"].isdigit() else None
            if q.get("overdue"):
                sql += " AND t.due_at < LOCALTIMESTAMP AND t.status = ANY(%(open)s)"
            if q.get("approval"):
                sql += " AND t.approval=%(approval)s"
                args["approval"] = q["approval"]
            s = q.get("q", "").strip()
            if s:
                sql += """ AND (upper(t.serial) LIKE %(like_up)s OR c.name ILIKE %(like)s OR c.phone LIKE %(like)s
                          OR t.brand ILIKE %(like)s OR t.model ILIKE %(like)s OR CAST(t.id AS TEXT)=%(id)s)"""
                args.update(like=f"%{s}%", like_up=f"%{serial_key(s)}%", id=s.lstrip("#"))
            total = d.val(f"SELECT COUNT(*) FROM ({sql}) x", args)
            sql += " ORDER BY (t.priority='urgent' AND t.status = ANY(%(open)s)) DESC, t.id DESC LIMIT %(lim)s OFFSET %(off)s"
            args.update(lim=min(int(q.get("limit", 50)), 500), off=int(q.get("offset", 0)))
            rows = d.all(sql, args)
            if role == "technician":
                rows = [redact_ticket_finances(t) for t in rows]
            return self.send_json(200, {"total": total, "rows": rows})

        if parts == ["tickets"] and method == "POST":
            need(can_receive, 403, "forbidden")
            serial = text(body, "serial", True, 100)
            itype = text(body, "intake_type") or "repair"
            need(itype in INTAKE_TYPES, 400, "bad_type")
            priority = "urgent" if body.get("priority") == "urgent" else "normal"
            cid = body.get("customer_id")
            if cid:
                cid = int(cid)
                need(d.one("SELECT 1 FROM customers WHERE id=%s", (cid,)), 404, "customer_not_found")
            else:
                cid = d.val("INSERT INTO customers(name,phone,phone2,company,address) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                            (text(body, "customer_name", True), text(body, "customer_phone"), text(body, "customer_phone2"),
                             text(body, "customer_company"), text(body, "customer_address")))
            info = {k: text(body, k) for k in ("device_type", "brand", "model")}
            ts = now()
            open_t = d.one("""SELECT t.id FROM tickets t JOIN devices dv ON dv.id=t.device_id
                              WHERE dv.serial_key=%s AND t.status = ANY(%s)""", (serial_key(serial), OPEN_STATUSES))
            if open_t and not (is_mgr and body.get("force")):
                raise ApiError(409, f"device_open:{open_t['id']}" if is_mgr else "device_already_open")
            dev = upsert_device(d, serial, info, cid, itype, ts)
            last = d.one("""SELECT id, warranty_until FROM tickets WHERE device_id=%s AND status='delivered'
                            ORDER BY closed_at DESC NULLS LAST LIMIT 1""", (dev["id"],))
            in_warranty = bool(last and last["warranty_until"] and last["warranty_until"] >= ts.date())
            due_days = int(body.get("due_days") or setting(d, "due_days") or 3)
            tid = d.val("""INSERT INTO tickets(customer_id,device_id,serial,device_type,brand,model,accessories,complaint,condition,
                password_note,intake_type,priority,first_visit,under_warranty,parent_ticket_id,status,received_by,received_at,updated_at,due_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'received',%s,%s,%s,%s) RETURNING id""",
                (cid, dev["id"], serial, info["device_type"], info["brand"], info["model"], text(body, "accessories"),
                 text(body, "complaint", False, 2000), text(body, "condition", False, 1000), text(body, "password_note", False, 200),
                 itype, priority, dev["_new"], in_warranty, last["id"] if last else None, user["id"], ts, ts,
                 ts + datetime.timedelta(days=max(0, min(due_days, 365)))))
            photos = body.get("photos") or []
            need(isinstance(photos, list) and len(photos) <= 20, 400, "too_many_photos")
            for p in photos:
                save_photo(d, tid, user["id"], "intake", p)
            log(d, tid, user["id"], "created", f"{itype}|{len(photos)}" + ("|first" if dev["_new"] else "") + ("|warranty" if in_warranty else ""))
            if body.get("assign_to"):
                tech = d.one("SELECT * FROM users WHERE id=%s AND active", (int(body["assign_to"]),))
                if tech and is_mgr:
                    d.run("UPDATE tickets SET assigned_to=%s, status='assigned' WHERE id=%s", (tech["id"], tid))
                    log(d, tid, user["id"], "assigned", tech["full_name"])
            return self.send_json(200, {"id": tid, "first_visit": dev["_new"], "under_warranty": in_warranty})

        if len(parts) >= 2 and parts[0] == "tickets":
            tid = int(parts[1])
            t = ticket_view(d, tid)
            need(t, 404, "not_found")
            if role == "technician":
                can_view = t["assigned_to"] == user["id"] or t["received_by"] == user["id"]
                need(can_view if method == "GET" else t["assigned_to"] == user["id"], 403, "forbidden")
            sub = parts[2] if len(parts) > 2 else ""

            if sub == "" and method == "GET":
                t["photos"] = d.all("SELECT id,stage,file,created_at FROM photos WHERE ticket_id=%s ORDER BY id", (tid,))
                t["parts"] = d.all("""SELECT p.*,u.full_name user_name FROM parts p JOIN users u ON u.id=p.user_id
                    WHERE ticket_id=%s ORDER BY p.id""", (tid,))
                t["notes"] = d.all("""SELECT n.*,u.full_name user_name FROM work_notes n JOIN users u ON u.id=n.user_id
                    WHERE ticket_id=%s ORDER BY n.id""", (tid,))
                t["history"] = d.all("""SELECT h.*,u.full_name user_name FROM history h JOIN users u ON u.id=h.user_id
                    WHERE ticket_id=%s ORDER BY h.id DESC""", (tid,))
                t["payments"] = d.all("""SELECT p.*,u.full_name user_name FROM payments p JOIN users u ON u.id=p.user_id
                    WHERE ticket_id=%s ORDER BY p.id""", (tid,))
                t["other_visits"] = d.all("SELECT id,status,intake_type,received_at FROM tickets WHERE device_id=%s AND id<>%s ORDER BY id DESC",
                                          (t["device_id"], tid)) if role != "technician" else []
                t["device_first_seen"] = d.one("SELECT first_seen_at, first_intake_type FROM devices WHERE id=%s", (t["device_id"],))
                t.update(ticket_money(d, t))
                if role == "technician":
                    redact_ticket_finances(t)
                return self.send_json(200, t)

            need(method == "POST" or sub == "zpl", 405, "method")
            closed = t["status"] in ("delivered", "rejected")
            if sub == "assign":
                need(is_mgr, 403, "forbidden")
                tech = d.one("SELECT * FROM users WHERE id=%s AND active", (int(body.get("user_id")),))
                need(tech, 404, "user_not_found")
                d.run("UPDATE tickets SET assigned_to=%s, status=CASE WHEN status='received' THEN 'assigned' ELSE status END WHERE id=%s",
                      (tech["id"], tid))
                log(d, tid, user["id"], "assigned", tech["full_name"])
            elif sub == "status":
                need(can_work(user, t) or (is_front and body.get("status") in ("waiting_customer", "rejected")), 403, "forbidden")
                st = text(body, "status", True)
                need(st in STATUSES, 400, "bad_status")
                need(st != "delivered", 400, "use_deliver")
                need(not closed or is_mgr, 403, "ticket_closed")
                closed_at = now() if st == "rejected" else None
                d.run("UPDATE tickets SET status=%s, closed_at=%s WHERE id=%s", (st, closed_at, tid))
                log(d, tid, user["id"], "status", f"{t['status']} -> {st}" + (f" | {text(body, 'note')}" if body.get("note") else ""))
            elif sub == "edit":
                need(is_front, 403, "forbidden")
                pr = "urgent" if body.get("priority") == "urgent" else "normal"
                itype = text(body, "intake_type") or t["intake_type"]
                need(itype in INTAKE_TYPES, 400, "bad_type")
                due = parse_date(body["due_at"]) if body.get("due_at") else None
                d.run("""UPDATE tickets SET device_type=%s, brand=%s, model=%s, accessories=%s, complaint=%s, condition=%s,
                         password_note=%s, priority=%s, intake_type=%s, due_at=%s WHERE id=%s""",
                      (text(body, "device_type"), text(body, "brand"), text(body, "model"), text(body, "accessories"),
                       text(body, "complaint", False, 2000), text(body, "condition", False, 1000),
                       text(body, "password_note", False, 200), pr, itype, due, tid))
                log(d, tid, user["id"], "edited")
            elif sub == "diagnosis":
                need(can_work(user, t), 403, "forbidden")
                est = money(body, "estimate") if body.get("estimate") not in (None, "") else None
                d.run("""UPDATE tickets SET diagnosis=%s, estimate=%s,
                         approval=CASE WHEN %s AND approval<>'approved' THEN 'pending' ELSE approval END,
                         status=CASE WHEN %s AND status IN ('received','assigned','in_repair') THEN 'waiting_customer' ELSE status END
                         WHERE id=%s""",
                      (text(body, "diagnosis", True, 4000), est, bool(body.get("ask_approval")), bool(body.get("ask_approval")), tid))
                log(d, tid, user["id"], "diagnosis", f"{est if est is not None else ''}" + ("|ask" if body.get("ask_approval") else ""))
            elif sub == "approval":
                need(is_front, 403, "forbidden")
                dec = text(body, "decision", True)
                need(dec in ("approved", "rejected"), 400, "bad_decision")
                new_status = ("in_repair" if t["assigned_to"] else "received") if dec == "approved" else "waiting_customer"
                d.run("UPDATE tickets SET approval=%s, approval_at=%s, approval_note=%s, status=%s WHERE id=%s",
                      (dec, now(), text(body, "note"), new_status if t["status"] == "waiting_customer" else t["status"], tid))
                log(d, tid, user["id"], "approval", dec + (f" | {text(body, 'note')}" if body.get("note") else ""))
            elif sub == "notes":
                need(can_work(user, t), 403, "forbidden")
                kind = text(body, "kind") or "maintenance"
                need(kind in ("maintenance", "inspection", "setup", "note"), 400, "bad_kind")
                d.run("INSERT INTO work_notes(ticket_id,user_id,kind,text,created_at) VALUES(%s,%s,%s,%s,%s)",
                      (tid, user["id"], kind, text(body, "text", True, 4000), now()))
                if t["status"] in ("received", "assigned"):
                    d.run("UPDATE tickets SET status='in_repair' WHERE id=%s", (tid,))
                log(d, tid, user["id"], "note", kind)
            elif sub == "parts":
                need(can_work(user, t), 403, "forbidden")
                need(not closed, 400, "ticket_closed")
                qty = int(body.get("qty") or 1)
                need(1 <= qty <= 1000, 400, "bad_number")
                d.run("INSERT INTO parts(ticket_id,name,serial,price,qty,user_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                      (tid, text(body, "name", True), text(body, "serial", False, 100), money(body, "price"), qty, user["id"], now()))
                log(d, tid, user["id"], "part_added", text(body, "name"))
            elif sub == "parts_delete":
                need(is_mgr, 403, "forbidden")
                p = d.one("SELECT name FROM parts WHERE id=%s AND ticket_id=%s", (int(body.get("id")), tid))
                need(p, 404, "not_found")
                d.run("DELETE FROM parts WHERE id=%s", (int(body.get("id")),))
                log(d, tid, user["id"], "part_removed", p["name"])
            elif sub == "charges":
                need(is_mgr, 403, "forbidden")
                d.run("UPDATE tickets SET labor=%s, discount=%s WHERE id=%s", (money(body, "labor"), money(body, "discount"), tid))
                log(d, tid, user["id"], "charges", f"{money(body, 'labor')}|{money(body, 'discount')}")
            elif sub == "payments":
                need(is_front, 403, "forbidden")
                amt = money(body, "amount")
                need(amt > 0, 400, "bad_number")
                method_ = text(body, "method") or "cash"
                need(method_ in PAY_METHODS, 400, "bad_method")
                d.run("INSERT INTO payments(ticket_id,amount,method,note,user_id,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                      (tid, amt, method_, text(body, "note"), user["id"], now()))
                log(d, tid, user["id"], "payment", f"{amt}|{method_}")
            elif sub == "deliver":
                need(is_front, 403, "forbidden")
                need(not closed, 400, "ticket_closed")
                need(t["status"] == "repaired" or body.get("force"), 400, "not_repaired")
                days = int(body.get("warranty_days") or 0)
                need(0 <= days <= 3650, 400, "bad_number")
                amt = money(body, "amount")
                if amt > 0:
                    method_ = text(body, "method") or "cash"
                    need(method_ in PAY_METHODS, 400, "bad_method")
                    d.run("INSERT INTO payments(ticket_id,amount,method,note,user_id,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                          (tid, amt, method_, "وقت التسليم", user["id"], now()))
                ts = now()
                d.run("""UPDATE tickets SET status='delivered', closed_at=%s, delivered_to=%s, warranty_days=%s,
                         warranty_until=%s WHERE id=%s""",
                      (ts, text(body, "delivered_to") or t["customer_name"], days, ts.date() + datetime.timedelta(days=days) if days else None, tid))
                log(d, tid, user["id"], "delivered", f"{text(body, 'delivered_to') or t['customer_name']}|{days}")
            elif sub == "reopen":
                need(is_mgr, 403, "forbidden")
                need(closed, 400, "not_closed")
                d.run("UPDATE tickets SET status='in_repair', closed_at=NULL WHERE id=%s", (tid,))
                log(d, tid, user["id"], "reopened", text(body, "note"))
            elif sub == "photos":
                need(can_work(user, t) or is_front, 403, "forbidden")
                stage = text(body, "stage") or "repair"
                need(stage in ("intake", "repair", "delivery"), 400, "bad_stage")
                photos = body.get("photos") or []
                need(isinstance(photos, list) and 0 < len(photos) <= 20, 400, "no_photos")
                for p in photos:
                    save_photo(d, tid, user["id"], stage, p)
                log(d, tid, user["id"], "photos", f"{len(photos)} {stage}")
            elif sub == "delete":
                need(role == "admin", 403, "forbidden")
                need(str(body.get("confirm", "")).strip().lstrip("#") == str(tid), 400, "confirm_mismatch")
                files = delete_ticket(d, t)
                d.run("INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(%s,'ticket_deleted',%s,%s)",
                      (user["id"], json.dumps({"id": tid, "serial": t["serial"], "customer": t["customer_name"], "status": t["status"],
                                               "received_at": to_json(t["received_at"]), "reason": text(body, "reason")}, ensure_ascii=False), now()))
                for f in files:
                    try:
                        os.remove(os.path.join(PHOTO_DIR, f))
                    except OSError:
                        pass
            elif sub == "printed":
                log(d, tid, user["id"], "printed", text(body, "what"))
            elif sub == "zpl":
                return self.send_json(200, {"zpl": zpl_label(t)})
            elif sub == "print_zebra":
                ip = setting(d, "printer_ip")
                need(ip, 400, "no_printer_ip")
                try:
                    with socket.create_connection((ip, 9100), timeout=5) as s:
                        s.sendall(zpl_label(t).encode("utf-8"))
                except OSError:
                    raise ApiError(502, "printer_unreachable")
                log(d, tid, user["id"], "label_printed")
            else:
                raise ApiError(404, "not_found")
            return self.send_json(200, {"ok": True})

        raise ApiError(404, "not_found")


def create_first_admin():
    """Installer step: first admin from RT_ADMIN_* environment variables (never on the command line)."""
    user, pw = os.environ.get("RT_ADMIN_USER", "").strip(), os.environ.get("RT_ADMIN_PASS", "")
    with DB() as d:
        if d.one("SELECT 1 FROM users"):
            print("users already exist - admin not changed")
            return
        if not user:
            print("no admin given - the first-run page will ask for one")
            return
        if len(pw) < 6:
            raise SystemExit("admin password must be at least 6 characters")
        d.run("INSERT INTO users(username,full_name,role,pw_hash) VALUES(%s,%s,'admin',%s)",
              (user[:50], os.environ.get("RT_ADMIN_NAME", "").strip()[:100] or user, hash_pw(pw)))
        company = os.environ.get("RT_COMPANY", "").strip()
        if company:
            d.run("UPDATE settings SET value=%s WHERE key='company_name'", (company[:100],))
        print(f"admin '{user}' created")


if __name__ == "__main__":
    try:
        init_db()
        if "--init" in sys.argv:
            create_first_admin()
            sys.exit(0)
    except psycopg.OperationalError as e:
        raise SystemExit(f"مش قادر أوصل لقاعدة البيانات\n{e}")
    threading.Thread(target=backup_loop, daemon=True).start()
    print(f"RT Workshop running on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
