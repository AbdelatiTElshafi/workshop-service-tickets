"""Workshop service tickets - single-computer app (Python standard library only).

Run:  python server.py        then open http://localhost:8080
First run creates user "admin" with password "admin" - change it from the Users page.
"""
import base64, hashlib, json, os, secrets, socket, sqlite3, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "workshop.db")
PHOTO_DIR = os.path.join(BASE, "data", "photos")
STATIC = os.path.join(BASE, "static")
PORT = int(os.environ.get("PORT", "8080"))
MAX_PHOTO = 8 * 1024 * 1024

STATUSES = ["received", "assigned", "in_repair", "waiting_customer", "repaired", "delivered", "rejected"]
ROLES = ["admin", "manager", "reception", "technician"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  role TEXT NOT NULL, pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  serial TEXT NOT NULL, device_type TEXT, brand TEXT, model TEXT, accessories TEXT, complaint TEXT,
  status TEXT NOT NULL, received_by INTEGER NOT NULL REFERENCES users(id), assigned_to INTEGER REFERENCES users(id),
  received_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_tickets_serial ON tickets(serial);
CREATE TABLE IF NOT EXISTS work_notes(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  user_id INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  name TEXT NOT NULL, serial TEXT, price REAL NOT NULL DEFAULT 0, qty INTEGER NOT NULL DEFAULT 1,
  user_id INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  stage TEXT NOT NULL, file TEXT NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)
        if not con.execute("SELECT 1 FROM users").fetchone():
            con.execute("INSERT INTO users(username,full_name,role,pw_hash) VALUES(?,?,?,?)",
                        ("admin", "Administrator", "admin", hash_pw("admin")))


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


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


def ticket_view(con, tid):
    t = con.execute("""SELECT t.*, c.name customer_name, c.phone customer_phone,
        ur.full_name received_by_name, ua.full_name assigned_to_name
        FROM tickets t JOIN customers c ON c.id=t.customer_id JOIN users ur ON ur.id=t.received_by
        LEFT JOIN users ua ON ua.id=t.assigned_to WHERE t.id=?""", (tid,)).fetchone()
    return dict(t) if t else None


def log(con, tid, uid, action, detail=""):
    con.execute("INSERT INTO history(ticket_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (tid, uid, action, detail, now()))
    con.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now(), tid))


def can_work(user, t):
    return user["role"] in ("admin", "manager") or (user["role"] == "technician" and t["assigned_to"] == user["id"])


def save_photo(con, tid, uid, stage, data_url):
    need(isinstance(data_url, str) and data_url.startswith("data:image/") and "," in data_url, 400, "bad_photo")
    head, b64 = data_url.split(",", 1)
    ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}.get(head[11:].split(";")[0])
    need(ext, 400, "bad_photo_type")
    raw = base64.b64decode(b64, validate=True)
    need(len(raw) <= MAX_PHOTO, 400, "photo_too_large")
    name = f"{tid}_{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(PHOTO_DIR, name), "wb") as f:
        f.write(raw)
    con.execute("INSERT INTO photos(ticket_id,stage,file,user_id,created_at) VALUES(?,?,?,?,?)",
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
            f"^FO15,156^A0N,22,22^FDIN: {t['received_at'][:16]}^FS"
            f"^FO15,184^A0N,22,22^FDTECH: {clean(t['assigned_to_name']) or '-'}^FS"
            "^XZ")


class Handler(BaseHTTPRequestHandler):
    server_version = "Workshop/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, obj, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def current_user(self, con):
        cookie = self.headers.get("Cookie", "")
        token = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("sid=")), None)
        if not token:
            return None
        r = con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND u.active=1",
                        (token,)).fetchone()
        return dict(r) if r else None

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
            with db() as con:
                self.api(con, method, path, parse_qs(url.query), body)
        except ApiError as e:
            self.send_json(e.status, {"error": e.msg})
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "bad_request"})

    def static(self, path):
        if path.startswith("/photos/"):
            name = os.path.basename(path)
            with db() as con:
                need(self.current_user(con), 401, "login")
            p = os.path.join(PHOTO_DIR, name)
            need(os.path.isfile(p), 404, "not_found")
            ext = name.rsplit(".", 1)[-1]
            return self.send_file(p, {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext])
        name = "index.html" if path in ("/", "") else os.path.basename(path)
        p = os.path.join(STATIC, name)
        need(os.path.isfile(p), 404, "not_found")
        ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css"}.get(name.rsplit(".", 1)[-1], "text/plain")
        self.send_file(p, ctype + "; charset=utf-8")

    def api(self, con, method, path, q, body):
        parts = path.strip("/").split("/")[1:]  # drop "api"
        if parts == ["login"] and method == "POST":
            u = con.execute("SELECT * FROM users WHERE username=? AND active=1", (text(body, "username"),)).fetchone()
            need(u and check_pw(str(body.get("password", "")), u["pw_hash"]), 401, "bad_login")
            token = secrets.token_urlsafe(32)
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (token, u["id"], time.time()))
            return self.send_json(200, {"ok": True}, {"Set-Cookie": f"sid={token}; HttpOnly; Path=/; SameSite=Strict"})

        user = self.current_user(con)
        need(user, 401, "login")
        role = user["role"]
        is_mgr = role in ("admin", "manager")

        if parts == ["logout"]:
            con.execute("DELETE FROM sessions WHERE user_id=? ", (user["id"],))
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
        if parts == ["me"]:
            return self.send_json(200, {k: user[k] for k in ("id", "username", "full_name", "role")})
        if parts == ["me", "password"] and method == "POST":
            need(check_pw(str(body.get("old", "")), user["pw_hash"]), 400, "bad_old_password")
            new = str(body.get("new", ""))
            need(len(new) >= 4, 400, "short_password")
            con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(new), user["id"]))
            return self.send_json(200, {"ok": True})

        # ---- users
        if parts == ["users"] and method == "GET":
            return self.send_json(200, rows(con.execute(
                "SELECT id,username,full_name,role,active FROM users ORDER BY full_name")))
        if parts == ["users"] and method == "POST":
            need(role == "admin", 403, "forbidden")
            r = text(body, "role", True)
            need(r in ROLES, 400, "bad_role")
            pw = str(body.get("password", ""))
            uid = body.get("id")
            if uid:
                con.execute("UPDATE users SET full_name=?, role=?, active=? WHERE id=?",
                            (text(body, "full_name", True), r, 1 if body.get("active", True) else 0, int(uid)))
                if pw:
                    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), int(uid)))
                if not body.get("active", True):
                    con.execute("DELETE FROM sessions WHERE user_id=?", (int(uid),))
            else:
                need(len(pw) >= 4, 400, "short_password")
                try:
                    con.execute("INSERT INTO users(username,full_name,role,pw_hash) VALUES(?,?,?,?)",
                                (text(body, "username", True, 50), text(body, "full_name", True), r, hash_pw(pw)))
                except sqlite3.IntegrityError:
                    raise ApiError(409, "username_taken")
            return self.send_json(200, {"ok": True})

        # ---- customers
        if parts == ["customers"] and method == "GET":
            s = f"%{q.get('q', [''])[0]}%"
            return self.send_json(200, rows(con.execute(
                "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY name LIMIT 20", (s, s))))

        # ---- settings (printer)
        if parts == ["settings"]:
            if method == "POST":
                need(role == "admin", 403, "forbidden")
                con.execute("INSERT OR REPLACE INTO settings VALUES('printer_ip',?)", (text(body, "printer_ip"),))
            r = con.execute("SELECT value FROM settings WHERE key='printer_ip'").fetchone()
            return self.send_json(200, {"printer_ip": r["value"] if r else ""})

        # ---- tickets
        if parts == ["tickets"] and method == "GET":
            sql = """SELECT t.id,t.serial,t.device_type,t.brand,t.model,t.status,t.received_at,t.updated_at,
                c.name customer_name,c.phone customer_phone,ua.full_name assigned_to_name
                FROM tickets t JOIN customers c ON c.id=t.customer_id LEFT JOIN users ua ON ua.id=t.assigned_to WHERE 1=1"""
            args = []
            if role == "technician":
                sql += " AND t.assigned_to=?"
                args.append(user["id"])
            st = q.get("status", [""])[0]
            if st:
                sql += " AND t.status=?"
                args.append(st)
            s = q.get("q", [""])[0].strip()
            if s:
                sql += " AND (t.serial LIKE ? OR c.name LIKE ? OR c.phone LIKE ? OR CAST(t.id AS TEXT)=?)"
                args += [f"%{s}%"] * 3 + [s.lstrip("#")]
            sql += " ORDER BY t.id DESC LIMIT 300"
            return self.send_json(200, rows(con.execute(sql, args)))

        if parts == ["tickets"] and method == "POST":
            need(role in ("admin", "manager", "reception"), 403, "forbidden")
            cid = body.get("customer_id")
            if cid:
                need(con.execute("SELECT 1 FROM customers WHERE id=?", (int(cid),)).fetchone(), 404, "customer_not_found")
                cid = int(cid)
            else:
                cid = con.execute("INSERT INTO customers(name,phone) VALUES(?,?)",
                                  (text(body, "customer_name", True), text(body, "customer_phone"))).lastrowid
            ts = now()
            tid = con.execute("""INSERT INTO tickets(customer_id,serial,device_type,brand,model,accessories,complaint,
                status,received_by,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, text(body, "serial", True, 100), text(body, "device_type"), text(body, "brand"),
                 text(body, "model"), text(body, "accessories"), text(body, "complaint", False, 2000),
                 "received", user["id"], ts, ts)).lastrowid
            photos = body.get("photos") or []
            need(isinstance(photos, list) and len(photos) <= 20, 400, "too_many_photos")
            for p in photos:
                save_photo(con, tid, user["id"], "intake", p)
            log(con, tid, user["id"], "created", f"{len(photos)} photos")
            return self.send_json(200, {"id": tid})

        if len(parts) >= 2 and parts[0] == "tickets":
            tid = int(parts[1])
            t = ticket_view(con, tid)
            need(t, 404, "not_found")
            need(role != "technician" or t["assigned_to"] == user["id"], 403, "forbidden")
            sub = parts[2] if len(parts) > 2 else ""

            if sub == "" and method == "GET":
                t["photos"] = rows(con.execute("SELECT id,stage,file,created_at FROM photos WHERE ticket_id=? ORDER BY id", (tid,)))
                t["parts"] = rows(con.execute("""SELECT p.*,u.full_name user_name FROM parts p JOIN users u ON u.id=p.user_id
                    WHERE ticket_id=? ORDER BY p.id""", (tid,)))
                t["notes"] = rows(con.execute("""SELECT n.*,u.full_name user_name FROM work_notes n JOIN users u ON u.id=n.user_id
                    WHERE ticket_id=? ORDER BY n.id""", (tid,)))
                t["history"] = rows(con.execute("""SELECT h.*,u.full_name user_name FROM history h JOIN users u ON u.id=h.user_id
                    WHERE ticket_id=? ORDER BY h.id DESC""", (tid,)))
                t["other_visits"] = rows(con.execute(
                    "SELECT id,status,received_at FROM tickets WHERE serial=? AND id<>? ORDER BY id DESC", (t["serial"], tid)))
                t["total"] = sum(p["price"] * p["qty"] for p in t["parts"])
                return self.send_json(200, t)

            need(method == "POST" or sub == "zpl", 405, "method")
            if sub == "assign":
                need(is_mgr, 403, "forbidden")
                tech = con.execute("SELECT * FROM users WHERE id=? AND active=1", (int(body.get("user_id")),)).fetchone()
                need(tech, 404, "user_not_found")
                con.execute("UPDATE tickets SET assigned_to=?, status=CASE WHEN status='received' THEN 'assigned' ELSE status END WHERE id=?",
                            (tech["id"], tid))
                log(con, tid, user["id"], "assigned", tech["full_name"])
            elif sub == "status":
                need(can_work(user, t), 403, "forbidden")
                st = text(body, "status", True)
                need(st in STATUSES, 400, "bad_status")
                need(st != "delivered" or role != "technician", 403, "forbidden")
                closed = now() if st in ("delivered", "rejected") else None
                con.execute("UPDATE tickets SET status=?, closed_at=? WHERE id=?", (st, closed, tid))
                log(con, tid, user["id"], "status", f"{t['status']} -> {st}" + (f" | {text(body, 'note')}" if body.get("note") else ""))
            elif sub == "notes":
                need(can_work(user, t), 403, "forbidden")
                kind = text(body, "kind") or "maintenance"
                con.execute("INSERT INTO work_notes(ticket_id,user_id,kind,text,created_at) VALUES(?,?,?,?,?)",
                            (tid, user["id"], kind, text(body, "text", True, 4000), now()))
                if t["status"] in ("received", "assigned"):
                    con.execute("UPDATE tickets SET status='in_repair' WHERE id=?", (tid,))
                log(con, tid, user["id"], "note", kind)
            elif sub == "parts":
                need(can_work(user, t), 403, "forbidden")
                price = float(body.get("price") or 0)
                qty = int(body.get("qty") or 1)
                need(price >= 0 and qty >= 1, 400, "bad_number")
                con.execute("INSERT INTO parts(ticket_id,name,serial,price,qty,user_id,created_at) VALUES(?,?,?,?,?,?,?)",
                            (tid, text(body, "name", True), text(body, "serial", False, 100), price, qty, user["id"], now()))
                log(con, tid, user["id"], "part_added", text(body, "name"))
            elif sub == "parts_delete":
                need(is_mgr, 403, "forbidden")
                con.execute("DELETE FROM parts WHERE id=? AND ticket_id=?", (int(body.get("id")), tid))
                log(con, tid, user["id"], "part_removed", str(body.get("id")))
            elif sub == "photos":
                need(can_work(user, t) or role == "reception", 403, "forbidden")
                stage = text(body, "stage") or "repair"
                photos = body.get("photos") or []
                need(isinstance(photos, list) and 0 < len(photos) <= 20, 400, "no_photos")
                for p in photos:
                    save_photo(con, tid, user["id"], stage, p)
                log(con, tid, user["id"], "photos", f"{len(photos)} {stage}")
            elif sub == "zpl":
                return self.send_json(200, {"zpl": zpl_label(t)})
            elif sub == "print_zebra":
                r = con.execute("SELECT value FROM settings WHERE key='printer_ip'").fetchone()
                need(r and r["value"], 400, "no_printer_ip")
                try:
                    with socket.create_connection((r["value"], 9100), timeout=5) as s:
                        s.sendall(zpl_label(t).encode("utf-8"))
                except OSError:
                    raise ApiError(502, "printer_unreachable")
                log(con, tid, user["id"], "label_printed")
            else:
                raise ApiError(404, "not_found")
            return self.send_json(200, {"ok": True})

        raise ApiError(404, "not_found")


if __name__ == "__main__":
    init_db()
    print(f"Workshop app running on http://localhost:{PORT}")
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), PORT), Handler).serve_forever()
