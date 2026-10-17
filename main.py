import csv
import io
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = (
    "https://exam.sanand.workers.dev/questionData"
    "?email=25ds3000164%40ds.study.iitm.ac.in"
    "&quizSign=zwUtkXYQToqb%2FZubajz%2Bz0xqvV4xZmmD7SIV5%2BFHwb7zQlvNhOntMHVfpvCU0dKeYaHqWf0eQZxfzitrJ7nX8PhyfmbMD6OnSSL4OeboA4fNaREZLHyLND8nYEhwiJ9cqw41lmeyNThxrENFmddIwmyqzMPikv%2FNpbRX2RuhP9cSlJjJO6b0bxj71ENg1nvEyGkbLUSJepvpgQgYffAv6QLjq53DhD%2FWx6OHJ601VY%2BU5rWRJ8Dli%2FSDFL5gvSrSr%2FFPR7Ddz32onRBIQhw9UGzoGOEsAzinktGndPvDTiUF2UUFnUVNjDbW7X0V1bHXkjs7ar9UxmOo8CFQ8yUDzw%3D%3D"
    "&questionId=q-ledger-agent-server"
)

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATE = {"rows": [], "rates": {"USD": 1}, "loaded": False, "error": None, "info": {}}


# ---------------------------------------------------------------- loading
def api_get(path, page=None):
    url = BASE + "&path=" + quote(path, safe="")
    if page is not None:
        url += f"&page={page}"
    r = requests.get(url, timeout=40)
    r.raise_for_status()
    return r.text


def extract_rows(obj):
    """Find dicts that look like orders anywhere in a JSON structure."""
    out = []
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict) and "id" in x and ("amount" in x or "status" in x):
                out.append(x)
            else:
                out.extend(extract_rows(x))
    elif isinstance(obj, dict):
        if "id" in obj and ("amount" in obj or "status" in obj) and "currency" in obj:
            out.append(obj)
        else:
            for v in obj.values():
                out.extend(extract_rows(v))
    return out


def parse_text_rows(text):
    text = text.strip()
    if not text:
        return []
    try:
        return extract_rows(json.loads(text))
    except Exception:
        pass
    rows = []
    ok = True
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            rows.extend(extract_rows(json.loads(line)))
        except Exception:
            ok = False
            break
    if ok and rows:
        return rows
    try:
        rd = csv.DictReader(io.StringIO(text))
        out = []
        for r in rd:
            for k in ("qty", "unit_price", "amount"):
                if k in r and r[k] not in (None, ""):
                    try:
                        r[k] = float(r[k])
                    except ValueError:
                        pass
            out.append(r)
        return [r for r in out if "id" in r]
    except Exception:
        return []


def fetch_paged():
    rows, page, last_first = [], 1, None
    while page <= 500:
        txt = api_get("/orders", page)
        try:
            obj = json.loads(txt)
        except Exception:
            obj = None
        r = extract_rows(obj) if obj is not None else parse_text_rows(txt)
        if not r:
            break
        first = r[0].get("id")
        if first == last_first:
            break
        last_first = first
        rows.extend(r)
        if isinstance(obj, dict):
            if obj.get("has_more") is False:
                break
            tp = obj.get("total_pages") or (obj.get("pagination") or {}).get("total_pages")
            if tp and page >= int(tp):
                break
        page += 1
    return rows


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load():
    try:
        rows = []
        info = {}
        try:
            ex = parse_text_rows(api_get("/export"))
            info["export_rows"] = len(ex)
            rows.extend(ex)
        except Exception as e:  # noqa
            info["export_error"] = str(e)[:200]
        try:
            pg = fetch_paged()
            info["paged_rows"] = len(pg)
            rows.extend(pg)
        except Exception as e:  # noqa
            info["paged_error"] = str(e)[:200]

        latest = {}
        for r in rows:
            k = r.get("id")
            if k is None:
                continue
            if k not in latest or parse_ts(r.get("updated_at")) >= parse_ts(latest[k].get("updated_at")):
                latest[k] = r
        rates = {"USD": 1}
        try:
            rj = json.loads(api_get("/rates"))
            rates = rj.get("usd_per_unit", rates)
        except Exception as e:  # noqa
            info["rates_error"] = str(e)[:200]

        for r in latest.values():
            r["_usd"] = Decimal(str(r.get("amount") or 0)) * Decimal(str(rates.get(r.get("currency"), 1)))
            r["_dt"] = parse_ts(r.get("created_at")).astimezone(IST)
            r["_status"] = str(r.get("status", "")).strip().lower()
            ref = Decimal(0)
            for k, v in r.items():
                if "refund" in k.lower() and isinstance(v, (int, float)) and not isinstance(v, bool):
                    ref += Decimal(str(v)) * Decimal(str(rates.get(r.get("currency"), 1)))
            r["_refund_field_usd"] = ref
        info["unique_orders"] = len(latest)
        STATE.update(rows=list(latest.values()), rates=rates, loaded=True, error=None, info=info)
    except Exception as e:  # noqa
        STATE["error"] = str(e)


def loader_loop():
    while True:
        load()
        time.sleep(600 if STATE["loaded"] else 20)


@app.on_event("startup")
def startup():
    threading.Thread(target=loader_loop, daemon=True).start()


# ---------------------------------------------------------------- question parsing
MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_MAP = {}
for i, m in enumerate(MONTH_NAMES, 1):
    MONTH_MAP[m] = i
    MONTH_MAP[m[:3]] = i
MONTH_MAP["sept"] = 9
MONTH_RE = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"


def money(d):
    return float(Decimal(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def infer_year(rows, month):
    cnt = defaultdict(int)
    for r in rows:
        if r["_dt"].month == month:
            cnt[r["_dt"].year] += 1
    if cnt:
        return max(cnt, key=cnt.get)
    ys = [r["_dt"].year for r in rows]
    return max(ys) if ys else datetime.now(IST).year


def parse_period(q, rows):
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        y, mo, d = map(int, m.groups())
        return (lambda dt: (dt.year, dt.month, dt.day) == (y, mo, d)), f"{y}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{4})-(\d{2})\b", q)
    if m:
        y, mo = map(int, m.groups())
        return (lambda dt: (dt.year, dt.month) == (y, mo)), f"{y}-{mo:02d}"
    m = re.search(
        r"(?:\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?)?\b" + MONTH_RE + r"\b(?:\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*\d))?,?\s*(?:(\d{4})\b)?",
        q,
    )
    if m and not (m.group(2) == "may" and not (m.group(1) or m.group(3) or m.group(4))):
        d1, mon, d2, yr = m.groups()
        mo = MONTH_MAP[mon]
        y = int(yr) if yr else infer_year(rows, mo)
        day = int(d1 or d2) if (d1 or d2) else None
        if day:
            return (lambda dt: (dt.year, dt.month, dt.day) == (y, mo, day)), "day"
        return (lambda dt: (dt.year, dt.month) == (y, mo)), "month"
    m = re.search(r"\bq([1-4])\b(?:\s*,?\s*(\d{4}))?", q) or re.search(r"\b(first|second|third|fourth)\s+quarter\b(?:\s+(?:of\s+)?(\d{4}))?", q)
    if m:
        qs = m.group(1)
        qn = {"first": 1, "second": 2, "third": 3, "fourth": 4}.get(qs) or int(qs)
        yrs = [r["_dt"].year for r in rows]
        y = int(m.group(2)) if m.group(2) else (max(yrs) if yrs else 2026)
        lo, hi = 3 * (qn - 1) + 1, 3 * qn
        return (lambda dt: dt.year == y and lo <= dt.month <= hi), "quarter"
    m = re.search(r"\b(20\d\d)\b", q)
    if m:
        y = int(m.group(1))
        return (lambda dt: dt.year == y), "year"
    return (lambda dt: True), None


def find_entities(q, rows, key):
    vals = sorted({str(r.get(key)) for r in rows if r.get(key) is not None}, key=len, reverse=True)
    found = []
    for v in vals:
        if re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?:s|es)?(?![a-z0-9])", q):
            found.append(v)
            q = q.replace(v.lower(), " ")
    return found


def answer_question(question):
    rows = STATE["rows"]
    q = question.lower().replace("’", "'")
    statuses = sorted({r["_status"] for r in rows})

    # --- single order lookup
    m = re.search(r"\bord-\d+\b", q)
    if m:
        oid = m.group(0).upper()
        for r in rows:
            if str(r["id"]).upper() == oid:
                if "status" in q:
                    return r["_status"]
                if "region" in q:
                    return r.get("region")
                if "product" in q:
                    return r.get("product")
                if "customer" in q:
                    return r.get("customer")
                if "currency" in q:
                    return r.get("currency")
                if "qty" in q or "quantity" in q or "units" in q:
                    return r.get("qty")
                return money(r["_usd"])
        return None

    period, plabel = parse_period(q, rows)
    regions = find_entities(q, rows, "region")
    products = find_entities(q, rows, "product")
    customers = [c.upper() for c in re.findall(r"\bc\d+\b", q)]

    def base_filter(r):
        if not period(r["_dt"]):
            return False
        if regions and r.get("region") not in regions:
            return False
        if products and r.get("product") not in products:
            return False
        if customers and str(r.get("customer")).upper() not in customers:
            return False
        return True

    sel = [r for r in rows if base_filter(r)]

    # --- metric
    asks_count = bool(re.search(r"how many|number of|count", q))
    asks_money = bool(re.search(r"amount|value|total|usd|\$|worth|dollar|how much", q))

    def status_filter():
        for s in statuses:
            stem = s[:-2] if s.endswith("ed") else s
            if s in q or (len(stem) > 4 and stem in q):
                return s
        if "canceled" in q and "cancelled" in statuses:
            return "cancelled"
        return None

    if "refund" in q:
        ref = [r for r in sel if "refund" in r["_status"] or r["_refund_field_usd"] > 0]
        metric = "refund_count" if (asks_count and not asks_money) else "refund_amount"
    elif re.search(r"average|avg|mean", q):
        metric = "aov"
    elif re.search(r"\bunits?\b|quantity|qty|pieces|items sold", q):
        metric = "units"
    elif re.search(r"(distinct|unique|different)\s+customers|(how many|number of|count of)\s+(distinct\s+|unique\s+|different\s+)?customers", q):
        metric = "distinct_customers"
    elif re.search(r"(distinct|unique|different)\s+products|(how many|number of|count of)\s+(distinct\s+|unique\s+|different\s+)?products", q):
        metric = "distinct_products"
    elif re.search(r"(distinct|unique|different)\s+regions|(how many|number of|count of)\s+(distinct\s+|unique\s+|different\s+)?regions", q):
        metric = "distinct_regions"
    elif asks_count and "order" in q:
        metric = "order_count"
    else:
        metric = "revenue"

    paid = [r for r in sel if r["_status"] == "paid"]

    def value_of(rs_all, metric):
        rs = [r for r in rs_all if r["_status"] == "paid"]
        if metric == "revenue":
            return sum((r["_usd"] for r in rs), Decimal(0))
        if metric == "units":
            return sum(int(r.get("qty") or 0) for r in rs)
        if metric == "aov":
            return (sum((r["_usd"] for r in rs), Decimal(0)) / len(rs)) if rs else Decimal(0)
        if metric == "order_count":
            sf = status_filter()
            if sf:
                return len([r for r in rs_all if r["_status"] == sf])
            return len(rs)
        if metric == "refund_amount":
            return sum(((r["_usd"] if "refund" in r["_status"] else r["_refund_field_usd"]) for r in rs_all if "refund" in r["_status"] or r["_refund_field_usd"] > 0), Decimal(0))
        if metric == "refund_count":
            return len([r for r in rs_all if "refund" in r["_status"] or r["_refund_field_usd"] > 0])
        if metric == "distinct_customers":
            return len({r.get("customer") for r in rs})
        if metric == "distinct_products":
            return len({r.get("product") for r in rs})
        if metric == "distinct_regions":
            return len({r.get("region") for r in rs})
        return 0

    # --- superlatives / group by
    sup_hi = re.search(r"\b(top|highest|most|best|largest|biggest|maximum|max|greatest|leading)\b", q)
    sup_lo = re.search(r"\b(lowest|least|smallest|fewest|worst|minimum|min|bottom)\b", q)
    if sup_hi or sup_lo:
        dim = None
        for word, key in (("customer", "customer"), ("product", "product"), ("region", "region")):
            if re.search(word, q):
                dim = key
                break
        if dim is None and "month" in q:
            dim = "month"
        if dim:
            groups = defaultdict(list)
            for r in sel:
                g = r["_dt"].strftime("%Y-%m") if dim == "month" else r.get(dim)
                groups[g].append(r)
            m_ = metric if metric not in ("distinct_customers", "distinct_products", "distinct_regions") else "revenue"
            scored = {g: value_of(rs, m_) for g, rs in groups.items()}
            scored = {g: v for g, v in scored.items() if g is not None}
            if scored:
                rev = bool(sup_hi)
                order = sorted(scored.items(), key=lambda kv: (-kv[1] if rev else kv[1], str(kv[0])))
                nm = re.search(r"\b(?:top|bottom)\s+(\d+)\b", q)
                if nm and int(nm.group(1)) > 1:
                    return [g for g, _ in order[: int(nm.group(1))]]
                return order[0][0]

    v = value_of(sel, metric)
    if isinstance(v, Decimal):
        return money(v)
    return v


# ---------------------------------------------------------------- endpoints
@app.get("/")
def root():
    return {"ok": True, "loaded": STATE["loaded"]}


@app.post("/")
@app.post("/ask")
def ask(body: dict):
    t0 = time.time()
    while not STATE["loaded"] and time.time() - t0 < 12:
        time.sleep(0.3)
    try:
        return {"answer": answer_question(str(body.get("question", "")))}
    except Exception as e:  # noqa
        return {"answer": None, "error": str(e)}


@app.get("/ask")
def ask_get(question: str):
    return ask({"question": question})


@app.get("/debug")
def debug():
    rows = STATE["rows"]
    keys = sorted({k for r in rows for k in r if not k.startswith("_")})
    dts = sorted(r["_dt"] for r in rows) if rows else []
    return {
        "loaded": STATE["loaded"],
        "error": STATE["error"],
        "info": STATE["info"],
        "rates": STATE["rates"],
        "keys": keys,
        "statuses": sorted({r["_status"] for r in rows}),
        "regions": sorted({str(r.get("region")) for r in rows}),
        "products": sorted({str(r.get("product")) for r in rows}),
        "currencies": sorted({str(r.get("currency")) for r in rows}),
        "first_date": str(dts[0]) if dts else None,
        "last_date": str(dts[-1]) if dts else None,
        "sample": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:3]],
    }
