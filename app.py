import os, uuid, random, json, base64, atexit
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
import requests
import resend
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, render_template, send_from_directory, abort
from dotenv import load_dotenv

_APP_DIR = Path(__file__).resolve().parent
_LOGO_PATH = _APP_DIR / "logo.png"
_ENV_FILE = _APP_DIR / ".env"
# 你指定嘅絕對路徑（同專案內 .env 二選一試）
_ENV_FILE_ABSOLUTE = Path(r"C:\Users\CHUCHU\OneDrive\桌面\Stock Predict App\.env")

load_dotenv(_ENV_FILE)
load_dotenv()

app = Flask(__name__)


def _env_strip(val):
    if val is None:
        return ""
    s = str(val).strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _parse_resend_key_from_dotenv_text(text: str):
    """
    手動解析：唔依賴 load_dotenv。用 split 搵 RESEND_API_KEY= 右邊嘅值。
    會去掉行首尾空白、去掉值外圍引號。
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        parts = line.split("=", 1)
        if len(parts) != 2:
            continue
        key_name = parts[0].strip()
        if key_name != "RESEND_API_KEY":
            continue
        val = parts[1].strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1].strip()
        return val
    return None


_RESEND_MANUAL_OK_PRINTED = False


def _manual_load_resend_key_from_disk():
    """
    用手動 open + read 讀 .env，再 split 拎 RESEND_API_KEY。
    成功則寫入 os.environ，並只 print 一次：前 5 位。
    """
    global _RESEND_MANUAL_OK_PRINTED
    if _env_strip(os.getenv("RESEND_API_KEY")):
        return _env_strip(os.getenv("RESEND_API_KEY"))

    tried = []
    for path in (_ENV_FILE_ABSOLUTE, _ENV_FILE):
        tried.append(path)
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        except OSError as e:
            print(f"[手動讀取 .env] 無法開啟 {path}: {e}")
            continue
        if content.startswith("\ufeff"):
            content = content.lstrip("\ufeff")

        key = _parse_resend_key_from_dotenv_text(content)
        if not key:
            print(
                f"[手動讀取 .env] 有內容但搵唔到有效行「RESEND_API_KEY=re_xxx」（檢查空格／引號）: {path}"
            )
            continue

        os.environ["RESEND_API_KEY"] = key
        if not _RESEND_MANUAL_OK_PRINTED:
            prefix = key[:5] if len(key) >= 5 else key
            print(f"手動讀取成功：{prefix}")
            _RESEND_MANUAL_OK_PRINTED = True
        return key

    print("[手動讀取 .env] 失敗：以下路徑都讀唔到 Key")
    for p in tried:
        print(f"  - {p}  存在: {p.is_file()}")
    return None


_manual_load_resend_key_from_disk()


def get_resend_api_key():
    """優先 os.getenv（已由手動讀取注入）；再試 load_dotenv。"""
    load_dotenv(_ENV_FILE)
    load_dotenv()
    k = _env_strip(os.getenv("RESEND_API_KEY"))
    if k:
        return k
    _manual_load_resend_key_from_disk()
    return _env_strip(os.getenv("RESEND_API_KEY"))


def _log_resend_key_status(where="啟動"):
    """Terminal 備用診斷。"""
    raw = os.getenv("RESEND_API_KEY")
    if raw is None:
        print(f"[env:{where}] RESEND_API_KEY is None（os.getenv 讀唔到）")
        print(f"[env:{where}] .env 路徑: {_ENV_FILE}  存在: {_ENV_FILE.is_file()}")
    elif _env_strip(raw) == "":
        print(f"[env:{where}] RESEND_API_KEY is 空字串或只有空白")
        print(f"[env:{where}] .env 路徑: {_ENV_FILE}  存在: {_ENV_FILE.is_file()}")
    else:
        k = _env_strip(raw)
        print(f"[env:{where}] RESEND_API_KEY 已讀取（長度 {len(k)}，內容唔會顯示）")


_log_resend_key_status("啟動")


def get_backup_receiver_email():
    """優先 MY_RECEIVER_EMAIL，其次 BACKUP_EMAIL；未填則用固定 Gmail。"""
    r = _env_strip(os.environ.get("MY_RECEIVER_EMAIL")) or _env_strip(
        os.environ.get("BACKUP_EMAIL")
    )
    return r if r else "tsangbobo49@gmail.com"

SUPABASE_URL        = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY        = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
TABLE_NAME     = "stock_notes"
PATTERNS_TABLE = "stock_patterns"
PATTERN_TYPES  = ("上升", "下跌", "教訓")
BUCKET_NAME    = "stock_images"
REST_BASE      = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
PATTERNS_BASE  = f"{SUPABASE_URL}/rest/v1/{PATTERNS_TABLE}"
STORAGE_BASE   = f"{SUPABASE_URL}/storage/v1/object"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
# Service-role key bypasses RLS — used for all mutating operations
SERVICE_HEADERS = {
    "apikey": SUPABASE_SECRET_KEY,
    "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    "Content-Type": "application/json",
}
STORAGE_HEADERS = {
    "apikey": SUPABASE_SECRET_KEY,
    "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
}

RESEND_FROM = os.environ.get("RESEND_FROM", "股票筆記備份 <onboarding@resend.dev>")
BACKUP_TZ = os.environ.get("BACKUP_TZ", "Asia/Hong_Kong").strip() or "Asia/Hong_Kong"

NOTE_IMPORT_KEYS = frozenset(
    {"id", "symbol", "category", "date", "content", "source", "image_url"}
)
PATTERN_IMPORT_KEYS = frozenset(
    {"id", "name", "pattern_type", "category", "content", "next_review_date", "image_url", "created_at"}
)
UPSERT_CHUNK = 80

# ── 欄位別名映射（備份 JSON 可能用唔同欄位名） ──
# stock_notes 別名：左邊係 JSON 用嘅名，右邊係 Supabase 欄位名
NOTE_FIELD_ALIASES = {
    "ticker":    "symbol",
    "stock":     "symbol",
    "code":      "symbol",
    "type":      "category",
    "cat":       "category",
    "text":      "content",
    "body":      "content",
    "note":      "content",
    "day":       "date",
    "ref":       "source",
    "img":       "image_url",
    "image":     "image_url",
}
# stock_patterns 別名
PATTERN_FIELD_ALIASES = {
    "title":      "name",
    "label":      "name",
    "kind":       "pattern_type",
    "type":       "pattern_type",
    "text":       "content",
    "body":       "content",
    "review":     "next_review_date",
    "review_date":"next_review_date",
    "img":        "image_url",
    "image":      "image_url",
}


# ── Storage helpers ──────────────────────────────────────

def upload_to_storage(file_bytes, original_filename, content_type):
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "png"
    unique_name = f"{uuid.uuid4()}.{ext}"
    resp = requests.post(
        f"{STORAGE_BASE}/{BUCKET_NAME}/{unique_name}",
        data=file_bytes,
        headers={**STORAGE_HEADERS, "Content-Type": content_type, "x-upsert": "true"},
    )
    if resp.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{unique_name}"
    return None


def delete_from_storage(image_url):
    if not image_url:
        return
    marker = f"/storage/v1/object/public/{BUCKET_NAME}/"
    if marker not in image_url:
        return
    filename = image_url.split(marker, 1)[-1]
    requests.delete(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}",
        json={"prefixes": [filename]},
        headers={**STORAGE_HEADERS, "Content-Type": "application/json"},
    )


def fetch_image_url(row_id):
    resp = requests.get(f"{REST_BASE}?id=eq.{row_id}&select=image_url", headers=HEADERS)
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            return rows[0].get("image_url")
    return None


# ── Page routes ──────────────────────────────────────────

@app.route("/logo.png")
def logo_png():
    if not _LOGO_PATH.is_file():
        abort(404)
    return send_from_directory(_APP_DIR, "logo.png", mimetype="image/png")


@app.route("/favicon.ico")
def favicon():
    """Browsers default-request /favicon.ico; serve app logo (PNG bytes, PNG type)."""
    if not _LOGO_PATH.is_file():
        abort(404)
    return send_from_directory(_APP_DIR, "logo.png", mimetype="image/png")


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/notes")
def notes_page():
    return render_template("notes.html")

@app.route("/patterns")
def patterns_page():
    return render_template("patterns.html")

@app.route("/quiz")
def quiz_page():
    return render_template("quiz.html")


# ── Image upload ─────────────────────────────────────────

@app.route("/upload_image", methods=["POST"])
def upload_image():
    image_file = request.files.get("image")
    if not image_file or not image_file.filename:
        return jsonify({"error": "沒有收到圖片"}), 400
    url = upload_to_storage(
        image_file.read(), image_file.filename,
        image_file.content_type or "image/png",
    )
    if url:
        return jsonify({"url": url}), 200
    return jsonify({"error": "上傳失敗"}), 500


# ── Notes CRUD ───────────────────────────────────────────

@app.route("/save", methods=["POST"])
def save():
    data     = request.get_json()
    symbol   = data.get("symbol",   "").strip().upper()
    category = data.get("category", "").strip()
    date_val = data.get("date",     "").strip()
    content  = (data.get("content") or "").strip()
    source   = data.get("source",   "").strip()
    if not symbol or not category or not date_val or not content:
        return jsonify({"error": "所有欄位皆為必填"}), 400
    if category not in {"Prediction", "Value Range"}:
        return jsonify({"error": "類別無效"}), 400
    resp = requests.post(
        REST_BASE,
        json={"symbol": symbol, "category": category,
              "date": date_val, "content": content, "source": source},
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    if resp.status_code in (200, 201):
        return jsonify({"message": "儲存成功", "data": resp.json()[0]}), 201
    return jsonify({"error": "儲存失敗", "detail": resp.text}), 500


@app.route("/search")
def search():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "請提供股票代號"}), 400
    resp = requests.get(
        REST_BASE,
        params={"symbol": f"eq.{symbol}", "order": "date.desc"},
        headers=HEADERS,
    )
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    rows = resp.json() or []
    grouped = {}
    for row in rows:
        cat = row.get("category", "Other")
        grouped.setdefault(cat, []).append(row)
    return jsonify({"symbol": symbol, "grouped": grouped}), 200


@app.route("/all")
def all_records():
    resp = requests.get(
        REST_BASE, params={"order": "symbol.asc,date.desc"}, headers=HEADERS
    )
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    rows = resp.json() or []
    grouped = {}
    for row in rows:
        sym = row.get("symbol", "?")
        grouped.setdefault(sym, []).append(row)
    return jsonify({"total": len(rows), "grouped": grouped, "order": list(grouped.keys())}), 200


@app.route("/update/<int:row_id>", methods=["PATCH"])
def update(row_id):
    data     = request.get_json() or {}
    payload  = {}
    if data.get("category"): payload["category"] = data["category"].strip()
    if data.get("date"):     payload["date"]     = data["date"].strip()
    payload["source"] = data.get("source", "").strip()
    if data.get("content"):  payload["content"]  = data["content"].strip()
    if not payload:
        return jsonify({"error": "沒有可更新的欄位"}), 400
    resp = requests.patch(
        f"{REST_BASE}?id=eq.{row_id}", json=payload,
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    if resp.status_code in (200, 204):
        result = resp.json() if resp.text.strip() else []
        return jsonify({"message": "更新成功", "data": result[0] if result else {}}), 200
    return jsonify({"error": "更新失敗", "detail": resp.text}), 500


@app.route("/delete/<int:row_id>", methods=["DELETE"])
def delete(row_id):
    print(f"\n[DELETE NOTE] ▶ row_id={row_id}")
    image_url = fetch_image_url(row_id)
    url = f"{REST_BASE}?id=eq.{row_id}"
    print(f"[DELETE NOTE]   Supabase URL: {url}")
    resp = requests.delete(
        url,
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    print(f"[DELETE NOTE]   Response status: {resp.status_code}  body: {resp.text[:200]}")
    if resp.status_code not in (200, 204):
        try:
            detail = resp.json().get("message") or resp.text
        except Exception:
            detail = resp.text
        print(f"[DELETE NOTE]   ✗ Failed: {detail}")
        return jsonify({"error": "刪除失敗", "detail": detail, "status": resp.status_code}), 500

    deleted = resp.json() if resp.text.strip() else []
    if isinstance(deleted, list) and len(deleted) == 0:
        print(f"[DELETE NOTE]   ✗ 0 rows deleted (RLS or wrong id?)")
        return jsonify({"error": f"找不到 ID {row_id} 的記錄，可能已被刪除"}), 404

    print(f"[DELETE NOTE]   ✓ Deleted {len(deleted)} row(s)")
    if image_url:
        delete_from_storage(image_url)
    return jsonify({"message": "刪除成功"}), 200


# ── Tickers API ──────────────────────────────────────────

@app.route("/api/tickers")
def get_tickers():
    resp = requests.get(REST_BASE, params={"select": "symbol"}, headers=HEADERS)
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    rows = resp.json() or []
    tickers = sorted({r["symbol"] for r in rows})
    return jsonify({"tickers": tickers}), 200


# ── Patterns CRUD ────────────────────────────────────────

@app.route("/api/patterns")
def get_all_patterns():
    resp = requests.get(
        PATTERNS_BASE,
        params={"order": "category.asc,created_at.desc"},
        headers=HEADERS,
    )
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    return jsonify({"patterns": resp.json() or []}), 200


@app.route("/api/pattern/save", methods=["POST"])
def save_pattern():
    data = request.get_json(silent=True) or {}
    name         = (data.get("name")         or "").strip()
    pattern_type = (data.get("pattern_type") or "").strip()
    category     = (data.get("category")     or "").strip()
    content      = (data.get("content")      or "").strip()

    missing = []
    if not name:         missing.append("Pattern 名稱")
    if not pattern_type: missing.append("類型")
    if not category:     missing.append("類別（庫）")
    if not content:      missing.append("內容")
    if missing:
        return jsonify({"error": f"以下欄位為必填：{', '.join(missing)}"}), 400
    if pattern_type not in PATTERN_TYPES:
        return jsonify({"error": "類型必須是「上升」、「下跌」或「教訓」"}), 400

    today = date.today().isoformat()
    payload = {
        "name": name,
        "pattern_type": pattern_type,
        "category": category,
        "content": content,
        "next_review_date": today,
        "image_url": None,          # always send null; upload handled separately
    }
    resp = requests.post(
        PATTERNS_BASE,
        json=payload,
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    if resp.status_code in (200, 201):
        rows = resp.json()
        return jsonify({"message": "儲存成功", "data": rows[0] if rows else {}}), 201

    try:
        err_body = resp.json()
        detail = err_body.get("message") or err_body.get("hint") or resp.text
    except Exception:
        detail = resp.text
    return jsonify({"error": "儲存失敗（資料庫回傳錯誤）", "detail": detail, "status": resp.status_code}), 500


@app.route("/api/patterns/<pattern_type>")
def get_patterns(pattern_type):
    resp = requests.get(
        PATTERNS_BASE,
        params={"pattern_type": f"eq.{pattern_type}", "order": "created_at.desc"},
        headers=HEADERS,
    )
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    return jsonify({"patterns": resp.json() or []}), 200


@app.route("/api/pattern/update/<int:row_id>", methods=["PATCH"])
def update_pattern(row_id):
    data    = request.get_json(silent=True) or {}
    payload = {}
    if data.get("name"):         payload["name"]         = data["name"].strip()
    if data.get("pattern_type") is not None:
        pt = (data.get("pattern_type") or "").strip()
        if pt not in PATTERN_TYPES:
            return jsonify({"error": "類型必須是「上升」、「下跌」或「教訓」"}), 400
        payload["pattern_type"] = pt
    if data.get("category") is not None:
        payload["category"] = (data.get("category") or "").strip() or None
    if data.get("content"):      payload["content"]      = data["content"].strip()
    if not payload:
        return jsonify({"error": "沒有可更新的欄位"}), 400
    resp = requests.patch(
        f"{PATTERNS_BASE}?id=eq.{row_id}", json=payload,
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    if resp.status_code in (200, 204):
        result = resp.json() if resp.text.strip() else []
        return jsonify({"message": "更新成功", "data": result[0] if result else {}}), 200
    try:
        err_body = resp.json()
        detail = err_body.get("message") or err_body.get("hint") or resp.text
    except Exception:
        detail = resp.text
    return jsonify({"error": "更新失敗", "detail": detail}), 500


@app.route("/api/pattern/delete/<int:row_id>", methods=["DELETE"])
def delete_pattern(row_id):
    print(f"\n[DELETE PATTERN] ▶ row_id={row_id}")
    url = f"{PATTERNS_BASE}?id=eq.{row_id}"
    print(f"[DELETE PATTERN]   Supabase URL: {url}")
    resp = requests.delete(
        url,
        headers={**SERVICE_HEADERS, "Prefer": "return=representation"},
    )
    print(f"[DELETE PATTERN]   Response status: {resp.status_code}  body: {resp.text[:200]}")
    if resp.status_code not in (200, 204):
        try:
            detail = resp.json().get("message") or resp.text
        except Exception:
            detail = resp.text
        print(f"[DELETE PATTERN]   ✗ Failed: {detail}")
        return jsonify({"error": "刪除失敗", "detail": detail, "status": resp.status_code}), 500

    deleted = resp.json() if resp.text.strip() else []
    if isinstance(deleted, list) and len(deleted) == 0:
        print(f"[DELETE PATTERN]   ✗ 0 rows deleted (RLS or wrong id?)")
        return jsonify({"error": f"找不到 ID {row_id} 的 Pattern，可能已被刪除"}), 404

    print(f"[DELETE PATTERN]   ✓ Deleted {len(deleted)} row(s)")
    return jsonify({"message": "刪除成功"}), 200


# ── Quiz ─────────────────────────────────────────────────

@app.route("/api/quiz")
def get_quiz():
    today = date.today().isoformat()
    resp = requests.get(
        PATTERNS_BASE,
        params={
            "next_review_date": f"lte.{today}",
            "pattern_type": "neq.教訓",
            "order": "next_review_date.asc",
        },
        headers=HEADERS,
    )
    if resp.status_code != 200:
        return jsonify({"error": "查詢失敗"}), 500
    rows = resp.json() or []
    selected = random.sample(rows, min(10, len(rows)))
    return jsonify({"questions": selected, "due_total": len(rows)}), 200


@app.route("/api/quiz/review/<int:row_id>", methods=["PATCH"])
def review_pattern(row_id):
    data   = request.get_json()
    result = (data.get("result") or "").strip()
    delays = {"好熟": 7, "一般熟": 2, "答錯": 1}
    if result not in delays:
        return jsonify({"error": "無效的結果"}), 400
    new_date = (date.today() + timedelta(days=delays[result])).isoformat()
    resp = requests.patch(
        f"{PATTERNS_BASE}?id=eq.{row_id}",
        json={"next_review_date": new_date},
        headers={**HEADERS, "Prefer": "return=representation"},
    )
    if resp.status_code in (200, 204):
        return jsonify({"message": "更新成功", "next_review_date": new_date}), 200
    return jsonify({"error": "更新失敗"}), 500


# ── Backup (export / Resend / import) ────────────────────

def _fetch_all_table(base_url, order):
    """Paginate through PostgREST using Range; requires service role for full data."""
    rows = []
    offset = 0
    step = 1000
    while True:
        hdrs = {
            **SERVICE_HEADERS,
            "Range": f"{offset}-{offset + step - 1}",
        }
        params = {"select": "*", "order": order}
        resp = requests.get(base_url, params=params, headers=hdrs)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Supabase 讀取失敗 ({resp.status_code}): {resp.text[:800]}"
            )
        chunk = resp.json() or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < step:
            break
        offset += step
    return rows


def export_data():
    """
    由 Supabase 抓取 stock_notes、stock_patterns 全表，回傳可 JSON 序列化嘅 dict。
    """
    notes = _fetch_all_table(REST_BASE, "symbol.asc,date.desc,id.asc")
    patterns = _fetch_all_table(PATTERNS_BASE, "id.asc")
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "stock_notes": notes,
        "stock_patterns": patterns,
    }


def export_and_send_email():
    """
    匯出 Supabase 全表 → 格式化 JSON → 以附件經 Resend 寄去收件人。
    手動觸發同排程共用。
    """
    api_key = get_resend_api_key()
    to_addr = get_backup_receiver_email()
    if not api_key:
        _log_resend_key_status("寄信前")
        raise ValueError(
            "未設定 RESEND_API_KEY。請喺與 app.py 同一層嘅 .env 加入：RESEND_API_KEY=re_你的金鑰（Terminal 會顯示係咪 None）"
        )

    payload = export_data()

    raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"stock_backup_{ts}.json"
    b64 = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")

    n_notes = len(payload.get("stock_notes") or [])
    n_pat = len(payload.get("stock_patterns") or [])
    exported = payload.get("exported_at", "")

    html_body = (
        "<p><strong>股票筆記自動備份</strong></p>"
        "<p>詳細資料見附件 JSON。</p>"
        "<ul>"
        f"<li><code>stock_notes</code>：{n_notes} 筆</li>"
        f"<li><code>stock_patterns</code>：{n_pat} 筆</li>"
        f"<li>匯出時間（UTC）：{exported}</li>"
        f"<li>附件檔名：<code>{filename}</code></li>"
        "</ul>"
    )
    text_body = (
        f"股票筆記自動備份\n"
        f"stock_notes: {n_notes} 筆，stock_patterns: {n_pat} 筆\n"
        f"匯出時間: {exported}\n"
        f"請開啟附件 {filename}（UTF-8 JSON）。\n"
    )

    resend.api_key = api_key
    try:
        result = resend.Emails.send(
            {
                "from": RESEND_FROM,
                "to": [to_addr],
                "subject": "股票筆記自動備份",
                "html": html_body,
                "text": text_body,
                "attachments": [{"filename": filename, "content": b64}],
            }
        )
    except Exception as e:
        raise RuntimeError(f"Resend 寄信失敗：{e}") from e

    email_id = getattr(result, "id", None)
    if isinstance(result, dict):
        email_id = result.get("id", email_id)

    return {
        "message": "備份已寄出（附件）",
        "email_id": email_id,
        "attachment": filename,
        "counts": {"stock_notes": n_notes, "stock_patterns": n_pat},
    }


def _normalize_row(row, aliases, allowed_keys):
    """
    1. 將別名欄位名轉為 Supabase 標準名（例如 ticker → symbol）。
    2. 只保留 allowed_keys 內嘅欄位。
    3. 回傳 (cleaned_dict, list_of_warnings)。
    """
    if not isinstance(row, dict):
        return None, ["列唔係 dict，跳過"]
    warnings = []
    renamed = {}
    for k, v in row.items():
        k_lower = k.lower()
        canonical = aliases.get(k_lower, k_lower)
        if canonical != k_lower:
            warnings.append(f"欄位重命名：{k!r} → {canonical!r}")
        renamed[canonical] = v
    cleaned = {k: renamed[k] for k in allowed_keys if k in renamed}
    missing = [k for k in (allowed_keys - {"id", "source", "image_url", "next_review_date", "created_at"})
               if k not in cleaned]
    if missing:
        warnings.append(f"缺少欄位：{missing}")
    return cleaned, warnings


def _detect_rows(raw, note_keys=("stock_notes", "notes", "data"),
                 pattern_keys=("stock_patterns", "patterns")):
    """
    格式自動適應：接受
      - {"stock_notes":[…], "stock_patterns":[…]}  ← 標準備份
      - {"notes":[…], "patterns":[…]}               ← 簡短鍵名
      - [{"symbol":…, "category":…}, …]             ← 直接陣列（視為 notes）
    """
    if isinstance(raw, list):
        return raw, []
    if not isinstance(raw, dict):
        return [], []
    for k in note_keys:
        if isinstance(raw.get(k), list):
            notes = raw[k]
            patterns = []
            for pk in pattern_keys:
                if isinstance(raw.get(pk), list):
                    patterns = raw[pk]
                    break
            return notes, patterns
    for pk in pattern_keys:
        if isinstance(raw.get(pk), list):
            return [], raw[pk]
    return [], []


def _strip_id(row):
    """永遠移除 id 欄位：Supabase id 係 GENERATED ALWAYS，唔可以手動傳入。"""
    return {k: v for k, v in row.items() if k != "id"}


def _fetch_existing_note_keys():
    """
    拉出資料庫現有 stock_notes 嘅 (symbol, date, category) 組合。
    用嚟去重，避免重複插入。
    """
    existing = set()
    try:
        resp = requests.get(
            REST_BASE,
            params={"select": "symbol,date,category", "limit": "10000"},
            headers=SERVICE_HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            for r in resp.json() or []:
                key = (
                    str(r.get("symbol") or "").upper(),
                    str(r.get("date") or ""),
                    str(r.get("category") or ""),
                )
                existing.add(key)
    except Exception as e:
        print(f"  [warn] 無法取得現有 notes 做去重比對：{e}")
    return existing


def _fetch_existing_pattern_keys():
    """
    拉出資料庫現有 stock_patterns 嘅 (name, pattern_type) 組合。
    """
    existing = set()
    try:
        resp = requests.get(
            PATTERNS_BASE,
            params={"select": "name,pattern_type", "limit": "10000"},
            headers=SERVICE_HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            for r in resp.json() or []:
                key = (
                    str(r.get("name") or "").strip(),
                    str(r.get("pattern_type") or "").strip(),
                )
                existing.add(key)
    except Exception as e:
        print(f"  [warn] 無法取得現有 patterns 做去重比對：{e}")
    return existing


def _insert_rows_verbose(base_url, rows, label):
    """
    逐筆 INSERT（不帶 id）；重複判斷交由呼叫方做。
    成功/失敗都 print 到 Terminal。
    回傳 (success_count, error_list)。
    """
    ok = 0
    errors = []
    for i, row in enumerate(rows):
        display_key = row.get("symbol") or row.get("name") or f"idx:{i}"
        row_clean = _strip_id(row)
        try:
            resp = requests.post(
                base_url,
                json=[row_clean],
                headers={
                    **SERVICE_HEADERS,
                    "Prefer": "return=minimal",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code in (200, 201, 204):
                print(f"  Success: Imported {label} [{display_key}]")
                ok += 1
            else:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text[:300]
                msg = f"  Error: {label} [{display_key}] → HTTP {resp.status_code}: {detail}"
                print(msg)
                errors.append(msg)
        except Exception as e:
            msg = f"  Error: {label} [{display_key}] → Exception: {e}"
            print(msg)
            errors.append(msg)
    return ok, errors


def import_backup_payload(raw):
    """
    格式自動適應 + 欄位別名映射 + 移除 id + 去重 + 逐行 debug + INSERT。
    """
    print("\n[import] ── 開始匯入 ──────────────────────────────")

    # ── 1. 自動偵測格式 ──
    notes_raw, patterns_raw = _detect_rows(raw)
    print(f"[import] 偵測到 stock_notes: {len(notes_raw)} 筆  stock_patterns: {len(patterns_raw)} 筆")

    if not notes_raw and not patterns_raw:
        raise ValueError(
            "JSON 格式無法識別。請確認頂層鍵名含 stock_notes 或 stock_patterns（或直接傳入陣列）。"
        )

    # ── 2. 規範化欄位（alias 映射）+ debug ──
    notes_clean, patterns_clean = [], []

    for i, row in enumerate(notes_raw):
        orig_id = row.get("id", "?")
        cleaned, warns = _normalize_row(row, NOTE_FIELD_ALIASES, NOTE_IMPORT_KEYS)
        if cleaned is None:
            print(f"  [skip] note[{i}] orig_id={orig_id}: {warns}")
            continue
        for w in warns:
            print(f"  [warn] note[{i}] orig_id={orig_id}: {w}")
        if not cleaned.get("symbol") or not cleaned.get("content"):
            print(f"  [skip] note[{i}] orig_id={orig_id}: 缺少必填欄位 symbol/content")
            continue
        notes_clean.append(cleaned)

    for i, row in enumerate(patterns_raw):
        orig_id = row.get("id", "?")
        cleaned, warns = _normalize_row(row, PATTERN_FIELD_ALIASES, PATTERN_IMPORT_KEYS)
        if cleaned is None:
            print(f"  [skip] pattern[{i}] orig_id={orig_id}: {warns}")
            continue
        for w in warns:
            print(f"  [warn] pattern[{i}] orig_id={orig_id}: {w}")
        if not cleaned.get("name") or not cleaned.get("content"):
            print(f"  [skip] pattern[{i}] orig_id={orig_id}: 缺少必填欄位 name/content")
            continue
        patterns_clean.append(cleaned)

    print(f"[import] 規範化後 notes: {len(notes_clean)} 筆  patterns: {len(patterns_clean)} 筆")

    # ── 3. 去重（比對資料庫現有記錄）──
    note_new, note_dup = [], []
    if notes_clean:
        existing_notes = _fetch_existing_note_keys()
        print(f"[import] 資料庫現有 notes: {len(existing_notes)} 筆")
        for r in notes_clean:
            key = (
                str(r.get("symbol") or "").upper(),
                str(r.get("date") or ""),
                str(r.get("category") or ""),
            )
            if key in existing_notes:
                note_dup.append(r)
                print(f"  [dup]  note symbol={r.get('symbol')} date={r.get('date')} cat={r.get('category')} 已存在，略過")
            else:
                note_new.append(r)

    pat_new, pat_dup = [], []
    if patterns_clean:
        existing_patterns = _fetch_existing_pattern_keys()
        print(f"[import] 資料庫現有 patterns: {len(existing_patterns)} 筆")
        for r in patterns_clean:
            key = (
                str(r.get("name") or "").strip(),
                str(r.get("pattern_type") or "").strip(),
            )
            if key in existing_patterns:
                pat_dup.append(r)
                print(f"  [dup]  pattern name={r.get('name')} type={r.get('pattern_type')} 已存在，略過")
            else:
                pat_new.append(r)

    print(f"[import] 新增 notes: {len(note_new)} 筆（略過重複 {len(note_dup)} 筆）")
    print(f"[import] 新增 patterns: {len(pat_new)} 筆（略過重複 {len(pat_dup)} 筆）")

    # ── 4. INSERT（id 已移除） ──
    all_errors = []
    note_ok = pat_ok = 0

    if note_new:
        print("[import] 寫入 stock_notes…")
        note_ok, errs = _insert_rows_verbose(REST_BASE, note_new, "note")
        all_errors.extend(errs)

    if pat_new:
        print("[import] 寫入 stock_patterns…")
        pat_ok, errs = _insert_rows_verbose(PATTERNS_BASE, pat_new, "pattern")
        all_errors.extend(errs)

    print(f"[import] 完成：notes 新增={note_ok} 重複={len(note_dup)}  patterns 新增={pat_ok} 重複={len(pat_dup)}  errors={len(all_errors)}")
    print("[import] ────────────────────────────────────────────\n")

    msg = (
        f"匯入完成：股票筆記新增 {note_ok} 筆（略過重複 {len(note_dup)} 筆）、"
        f"Pattern 新增 {pat_ok} 筆（略過重複 {len(pat_dup)} 筆）"
        + (f"｜⚠️ {len(all_errors)} 筆失敗，詳見 Terminal" if all_errors else "")
    )

    return {
        "message": msg,
        "imported": {
            "stock_notes": note_ok,
            "stock_notes_skipped": len(note_dup),
            "stock_patterns": pat_ok,
            "stock_patterns_skipped": len(pat_dup),
            "errors": len(all_errors),
            "error_details": all_errors[:10],
        },
    }


@app.route("/api/backup/send", methods=["POST"])
def backup_send():
    try:
        out = export_and_send_email()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(out), 200


@app.route("/api/backup/import", methods=["POST"])
def backup_import():
    raw = None
    ct = (request.content_type or "").lower()
    if "multipart/form-data" in ct:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "請選擇 JSON 備份檔"}), 400
        try:
            raw = json.loads(f.read().decode("utf-8"))
        except json.JSONDecodeError as e:
            return jsonify({"error": f"檔案唔係有效 JSON：{e}"}), 400
        except UnicodeDecodeError as e:
            return jsonify({"error": f"檔案編碼無法讀取：{e}"}), 400
    else:
        raw = request.get_json(silent=True)
        if raw is None:
            return jsonify({"error": "請提供 JSON body，或使用 multipart 上傳欄位 file"}), 400

    try:
        out = import_backup_payload(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(out), 200


# ── 每週日凌晨 3:00（預設 Asia/Hong_Kong）自動備份寄信 ──

backup_scheduler = BackgroundScheduler(timezone=BACKUP_TZ)


def _scheduled_backup_email():
    try:
        export_and_send_email()
        print(
            f"[backup scheduler] 已寄出備份 {datetime.now(timezone.utc).isoformat()} UTC"
        )
    except Exception as e:
        print(f"[backup scheduler] 失敗：{e}")


backup_scheduler.add_job(
    _scheduled_backup_email,
    "cron",
    day_of_week="sun",
    hour=3,
    minute=0,
    id="weekly_stock_backup_email",
    replace_existing=True,
)


def _start_backup_scheduler():
    if os.environ.get("DISABLE_BACKUP_SCHEDULER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        print("[backup scheduler] 已停用（DISABLE_BACKUP_SCHEDULER）")
        return
    try:
        backup_scheduler.start()
        print(
            f"[backup scheduler] 已啟動：每週日 03:00（{BACKUP_TZ}）寄送備份至 {get_backup_receiver_email()}"
        )
    except Exception as e:
        print(f"[backup scheduler] 無法啟動：{e}")


# 避免 Flask debug 父行程重複啟動；正式環境直接啟動
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    _start_backup_scheduler()

atexit.register(lambda: backup_scheduler.shutdown(wait=False))


if __name__ == "__main__":
    app.run(debug=True)
