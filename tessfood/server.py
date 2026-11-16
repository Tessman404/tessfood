#!/usr/bin/env python3
"""Tessfood : stock du frigo / congélateur / placards, liste de courses, drive Intermarché.

- page web + API sur le port 8099 (ouvert dans Home Assistant via l'onglet latéral)
- port 8098 : uniquement pour le scanner de la cuisine (POST /scan avec le jeton)
Python standard uniquement.
"""
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TESSFOOD_DATA", os.path.join(BASE, "data"))
DB_PATH = os.path.join(DATA, "db.json")
LOCK = threading.RLock()
os.makedirs(DATA, exist_ok=True)

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
SCAN_TOKEN = os.environ.get("SCAN_TOKEN", "")

ZONES = ["frigo", "congelateur", "placard", "cellier", "maison"]
ZONE_LABEL = {"frigo": "Frigo", "congelateur": "Congélateur", "placard": "Placard", "cellier": "Cellier / boissons", "maison": "Maison (hygiène, entretien)"}

# Cartes à scanner pour changer d'action (imprimables depuis l'appli)
MODE_CARDS = {"TF-SORTIE": "consume", "TF-AJOUT": "add", "TF-OUVERT": "open", "TF-JETE": "waste", "TF-ANNULER": "undo"}
MODE_LABEL = {"consume": "Sortie du stock", "add": "Ajout au stock", "open": "Produit ouvert", "waste": "Jeté (périmé)"}
MODE_WINDOW = 90  # secondes pendant lesquelles une carte de mode reste active

DEFAULT_SETTINGS = {
    "notify_service": "",          # ex. mobile_app_iphone_de_thomas
    "alexa_entity": "",            # ex. todo.alexa_shopping_list
    "door_entity": "",             # ex. binary_sensor.porte_frigo
    "door_minutes": 2,
    "expiry_warn_days": 3,
    "check_weekday": 6,            # 0 = lundi ... 6 = dimanche
    "check_hour": 18,
    "drive_store": "Intermarché",
    "announce_entity": "",         # ex. media_player.echo_cuisine (annonce vocale après un scan, optionnel)
}


# ---------------------------------------------------------------- outils
def now():
    return dt.datetime.now()


def today():
    return dt.date.today()


def iso(d):
    return d.isoformat() if d else None


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def new_id():
    return uuid.uuid4().hex[:10]


def load_db():
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("products", {})
    d.setdefault("stock", [])
    d.setdefault("moves", [])
    d.setdefault("shopping", [])
    d.setdefault("orders", [])
    d.setdefault("unknown", [])
    d.setdefault("checks", {})       # product_id -> date de la dernière confirmation « encore là »
    d.setdefault("state", {})
    s = dict(DEFAULT_SETTINGS)
    s.update(d.get("settings") or {})
    d["settings"] = s
    return d


def save_db(d):
    d["moves"] = d["moves"][-3000:]
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_PATH)


def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- Home Assistant
def ha(method, path, payload=None, timeout=15):
    if not HA_URL or not HA_TOKEN:
        raise RuntimeError("Home Assistant non joignable (lancé hors Home Assistant)")
    req = urllib.request.Request(HA_URL + path, method=method,
                                 data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                                 headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


def ha_ok():
    return bool(HA_URL and HA_TOKEN)


def notify(title, message, tag="tessfood", url="/tessfood"):
    svc = load_db()["settings"].get("notify_service", "").strip()
    if not svc or not ha_ok():
        return False
    try:
        ha("POST", f"/api/services/notify/{svc}",
           {"title": title, "message": message, "data": {"tag": tag, "url": url, "clickAction": url}})
        return True
    except Exception as e:
        log("notification :", e)
        return False


def fire_event(data):
    if ha_ok():
        try:
            ha("POST", "/api/events/tessfood_scan", data, timeout=5)
        except Exception as e:
            log("événement :", e)


def announce(text):
    ent = load_db()["settings"].get("announce_entity", "").strip()
    if not ent or not ha_ok():
        return
    try:
        if ent.startswith("media_player.") and ("echo" in ent or "alexa" in ent):
            ha("POST", "/api/services/notify/alexa_media", {"message": text, "target": [ent], "data": {"type": "tts"}}, timeout=5)
        else:
            ha("POST", "/api/services/tts/speak", {"entity_id": "tts.home_assistant_cloud", "media_player_entity_id": ent,
                                                    "message": text}, timeout=5)
    except Exception as e:
        log("annonce :", e)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- Open Food Facts
OFF_ZONE = [("frozen", "congelateur"), ("surgel", "congelateur"), ("ice-cream", "congelateur"),
            ("dair", "frigo"), ("milk", "frigo"), ("yogurt", "frigo"), ("cheese", "frigo"), ("butter", "frigo"),
            ("cream", "frigo"), ("meat", "frigo"), ("ham", "frigo"), ("fish", "frigo"), ("seafood", "frigo"),
            ("fresh", "frigo"), ("eggs", "frigo"), ("desserts", "frigo"), ("salads", "frigo"),
            ("beverages", "cellier"), ("waters", "cellier"), ("wines", "cellier"), ("beers", "cellier")]


def off_lookup(code):
    url = f"https://world.openfoodfacts.org/api/v2/product/{urllib.parse.quote(code)}.json?fields=" \
          "product_name_fr,product_name,generic_name_fr,brands,image_front_small_url,image_front_url,categories_tags,quantity"
    req = urllib.request.Request(url, headers={"User-Agent": "Tessfood/1.0 (Home Assistant; contact via GitHub Tessman404)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
    except Exception as e:
        log("Open Food Facts :", e)
        return None
    p = d.get("product") if d.get("status") == 1 else None
    if not p:
        return None
    name = (p.get("product_name_fr") or p.get("product_name") or p.get("generic_name_fr") or "").strip()
    cats = " ".join(p.get("categories_tags") or [])
    zone = "placard"
    for key, z in OFF_ZONE:
        if key in cats:
            zone = z
            break
    cat = (p.get("categories_tags") or [""])[-1].split(":")[-1].replace("-", " ")
    return {"name": name, "brand": (p.get("brands") or "").split(",")[0].strip(), "quantity": p.get("quantity") or "",
            "image": p.get("image_front_small_url") or p.get("image_front_url") or "", "zone": zone, "category": cat,
            "cats": cats}


# ---------------------------------------------------------------- produits & stock
def product_by_code(d, code):
    for p in d["products"].values():
        if code in p.get("barcodes", []):
            return p
    return None


def product_by_name(d, name):
    n = norm(name)
    if not n:
        return None
    best = None
    for p in d["products"].values():
        pn = norm(p["name"])
        if pn == n:
            return p
        if n in pn.split() or (len(n) > 3 and (n in pn or pn in n)):
            best = best or p
    return best


# Durées habituelles (jours) : se garde après achat / une fois ouvert. Estimations, modifiables par produit.
SHELF = [
    (("hache", "steak", "volaille", "poulet", "dinde", "poisson", "saumon", "cabillaud", "fish", "poultry", "meat"), 3, 1),
    (("jambon", "lardon", "saucisse", "charcuterie", "ham", "bacon"), 10, 4),
    (("yaourt", "yogurt", "fromage blanc", "dessert lacte", "skyr", "creme dessert"), 21, 2),
    (("creme fraiche", "creme liquide", "cream"), 21, 4),
    (("beurre", "butter"), 45, 30),
    (("fromage", "cheese", "emmental", "comte", "camembert", "mozzarella"), 21, 10),
    (("lait", "milk"), 60, 4),
    (("oeuf", "eggs"), 21, 0),
    (("salade", "laitue", "epinard", "legume", "vegetables"), 5, 2),
    (("fruit", "pomme", "banane", "orange", "fraise"), 7, 0),
    (("pain", "baguette", "bread", "brioche"), 4, 3),
    (("jus", "juice"), 180, 5),
    (("ketchup", "mayonnaise", "moutarde", "sauce"), 0, 30),
]


def estimate_shelf(name, cats="", zone="placard"):
    if zone == "congelateur":
        return 180, 0
    if zone in ("cellier", "maison"):
        return 0, 0
    t = " " + norm(name) + " " + cats.replace("-", " ")
    for words, keep, opened in SHELF:
        if zone != "frigo" and words[0] not in ("pain", "fruit", "lait", "jus"):
            continue  # au placard, seuls pain, fruits, lait et jus ont une durée courte
        if any(w in t for w in words):
            return keep, opened
    return (10, 4) if zone == "frigo" else (0, 0)


def make_product(d, name, code=None, info=None, zone=None):
    info = info or {}
    pid = new_id()
    p = {"id": pid, "name": (name or info.get("name") or f"Produit {code}").strip(), "brand": info.get("brand", ""),
         "quantity": info.get("quantity", ""), "image": info.get("image", ""), "category": info.get("category", ""),
         "barcodes": [code] if code else [], "zone": zone or info.get("zone") or "placard",
         "min_qty": 0, "drive_name": "", "drive_url": "", "open_days": 0, "keep_days": 0,
         "created": iso(today())}
    p["keep_days"], p["open_days"] = estimate_shelf(p["name"], info.get("cats", ""), p["zone"])
    d["products"][pid] = p
    return p


def stock_qty(d, pid):
    return round(sum(s["qty"] for s in d["stock"] if s["product_id"] == pid), 2)


def add_move(d, pid, delta, kind, source):
    d["moves"].append({"ts": now().isoformat(timespec="seconds"), "product_id": pid, "delta": delta, "kind": kind,
                       "source": source})
    d["checks"].pop(pid, None) if kind in ("add", "purchase") else None


def add_stock(d, p, qty=1, zone=None, expiry=None, source="app", kind="add"):
    exp, est = parse_date(expiry), False
    if not exp and p.get("keep_days"):
        exp, est = today() + dt.timedelta(days=int(p["keep_days"])), True
    zone = zone or p.get("zone") or "placard"
    for s in d["stock"]:  # on regroupe avec une ligne identique (même zone, même date, non ouverte)
        if s["product_id"] == p["id"] and s["zone"] == zone and s.get("expiry") == iso(exp) and not s.get("opened"):
            s["qty"] = round(s["qty"] + qty, 2)
            break
    else:
        d["stock"].append({"id": new_id(), "product_id": p["id"], "qty": qty, "zone": zone, "expiry": iso(exp),
                           "opened": None, "added": iso(today()), "est": est})
    add_move(d, p["id"], qty, kind, source)
    # le produit est revenu : on le retire de la liste de courses
    for it in d["shopping"]:
        if not it.get("done") and it.get("product_id") == p["id"] and kind == "purchase":
            it["done"] = True


def _order(s):
    # d'abord les produits ouverts, puis la date la plus proche
    return (0 if s.get("opened") else 1, s.get("expiry") or "9999")


def take_stock(d, p, qty=1, kind="consume", source="app"):
    left = qty
    entries = sorted([s for s in d["stock"] if s["product_id"] == p["id"]], key=_order)
    for s in entries:
        if left <= 0:
            break
        t = min(s["qty"], left)
        s["qty"] = round(s["qty"] - t, 2)
        left = round(left - t, 2)
    d["stock"] = [s for s in d["stock"] if s["qty"] > 0]
    add_move(d, p["id"], -qty, kind, source)
    return left > 0  # True = il n'y en avait pas assez d'après l'appli


def open_stock(d, p, source="app"):
    entries = sorted([s for s in d["stock"] if s["product_id"] == p["id"] and not s.get("opened")], key=_order)
    if not entries:
        return False
    s = entries[0]
    if s["qty"] > 1:  # on sépare l'unité ouverte
        s["qty"] = round(s["qty"] - 1, 2)
        s = dict(s, id=new_id(), qty=1)
        d["stock"].append(s)
    s["opened"] = iso(today())
    if p.get("open_days"):
        lim = today() + dt.timedelta(days=int(p["open_days"]))
        if not parse_date(s.get("expiry")) or parse_date(s["expiry"]) > lim:
            s["expiry"] = iso(lim)
    add_move(d, p["id"], 0, "open", source)
    return True


def check_min(d, p):
    """Sous le minimum -> ajouté à la liste de courses (et à Alexa)."""
    mq = float(p.get("min_qty") or 0)
    if mq <= 0 or stock_qty(d, p["id"]) >= mq:
        return None
    if any(not i.get("done") and i.get("product_id") == p["id"] for i in d["shopping"]):
        return None
    it = {"id": new_id(), "product_id": p["id"], "name": p["name"], "qty": max(1, round(mq - stock_qty(d, p["id"]))),
          "source": "auto", "done": False, "added": now().isoformat(timespec="seconds"), "alexa_uid": None}
    d["shopping"].append(it)
    return it


# ---------------------------------------------------------------- scanner
def scan(code, source="scanner"):
    code = re.sub(r"\s+", "", str(code or ""))
    if not code:
        return {"ok": False, "message": "code vide"}
    with LOCK:
        d = load_db()
        st = d["state"]
        mode = st.get("mode", "consume")
        if time.time() - st.get("mode_ts", 0) > MODE_WINDOW:
            mode = "consume"
        card = MODE_CARDS.get(code.upper())
        if card == "undo":
            res = undo_last(d)
            save_db(d)
            return res
        if card:
            st["mode"], st["mode_ts"] = card, time.time()
            save_db(d)
            res = {"ok": True, "mode": card, "message": f"Mode : {MODE_LABEL[card]}"}
            fire_event(res)
            return res

        p = product_by_code(d, code)
        created = False
        if not p:
            save_db(d)
            info = off_lookup(code)  # hors verrou serait mieux, mais c'est rapide
            d = load_db()
            st = d["state"]
            p = product_by_code(d, code)
            if not p:
                p = make_product(d, info["name"] if info and info["name"] else f"Produit inconnu {code}", code, info)
                created = True
                if not info or not info.get("name"):
                    d["unknown"].append({"code": code, "product_id": p["id"], "ts": now().isoformat(timespec="seconds")})

        warn = ""
        if mode == "add":
            add_stock(d, p, 1, source=source)
        elif mode == "open":
            if not open_stock(d, p, source):
                add_stock(d, p, 1, source=source)
                open_stock(d, p, source)
                warn = "il n'était pas dans le stock, je l'ai ajouté"
        else:
            if take_stock(d, p, 1, "waste" if mode == "waste" else "consume", source):
                warn = "il n'était pas dans le stock"
            check_min(d, p)
        if mode != "consume":
            st["mode_ts"] = time.time()  # on prolonge la carte tant qu'on scanne
        left = stock_qty(d, p["id"])
        st["last_scan"] = {"ts": now().isoformat(timespec="seconds"), "code": code, "product_id": p["id"], "mode": mode}
        save_db(d)
        sync_alexa_soon()
    msg = f"{p['name']} : {MODE_LABEL[mode].lower()}, il en reste {fmt_qty(left)}"
    if warn:
        msg += f" ({warn})"
    res = {"ok": True, "mode": mode, "product": p["name"], "product_id": p["id"], "left": left, "new": created,
           "warning": warn, "message": msg}
    fire_event(res)
    announce(f"{p['name']}, il en reste {fmt_qty(left)}" if mode == "consume" else f"{p['name']}, {MODE_LABEL[mode].lower()}")
    return res


def fmt_qty(q):
    return str(int(q)) if float(q).is_integer() else str(q).replace(".", ",")


def undo_last(d):
    """Carte ANNULER : on défait le dernier mouvement venant du scanner."""
    for i in range(len(d["moves"]) - 1, -1, -1):
        m = d["moves"][i]
        if m["source"] != "scanner":
            continue
        p = d["products"].get(m["product_id"])
        d["moves"].pop(i)
        if p:
            if m["delta"] < 0:
                add_stock(d, p, -m["delta"], source="undo")
                d["moves"].pop()
            elif m["delta"] > 0:
                take_stock(d, p, m["delta"], source="undo")
                d["moves"].pop()
            elif m["kind"] == "open":
                for s in d["stock"]:
                    if s["product_id"] == p["id"] and s.get("opened") == iso(today()):
                        s["opened"] = None
                        break
        res = {"ok": True, "mode": "undo", "message": f"Annulé : {p['name'] if p else 'dernier scan'}"}
        fire_event(res)
        return res
    return {"ok": False, "message": "rien à annuler"}


# ---------------------------------------------------------------- habitudes : « probablement fini ? »
def usage_profile(d, pid):
    """Durée moyenne (jours) pour finir 1 unité, d'après les sorties passées."""
    outs = [dt.datetime.fromisoformat(m["ts"]) for m in d["moves"]
            if m["product_id"] == pid and m["kind"] == "consume"]
    if len(outs) < 3:
        return None
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(outs, outs[1:]) if (b - a).total_seconds() > 3600]
    if len(gaps) < 2:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]  # médiane : insensible aux absences / vacances


def doubtful(d):
    """Produits que l'appli croit en stock, mais qui sont sûrement finis (sortie oubliée)."""
    out = []
    for pid, p in d["products"].items():
        q = stock_qty(d, pid)
        if q <= 0:
            continue
        per = usage_profile(d, pid)
        last = [m for m in d["moves"] if m["product_id"] == pid]
        if not per or not last:
            continue
        since = (now() - dt.datetime.fromisoformat(last[-1]["ts"])).total_seconds() / 86400
        chk = parse_date(d["checks"].get(pid))
        if chk and (today() - chk).days < max(3, per):
            continue
        expected = per * q * 1.8 + 2
        if since > expected:
            out.append({"product_id": pid, "name": p["name"], "qty": q, "days": round(since),
                        "reason": f"d'habitude 1 dure ~{round(per)} j, aucun mouvement depuis {round(since)} j"})
    return out


def expiring(d, days=None):
    days = d["settings"]["expiry_warn_days"] if days is None else days
    lim = today() + dt.timedelta(days=int(days))
    out = []
    for s in d["stock"]:
        e = parse_date(s.get("expiry"))
        if e and e <= lim:
            p = d["products"].get(s["product_id"], {})
            out.append({"stock_id": s["id"], "product_id": s["product_id"], "name": p.get("name", "?"), "qty": s["qty"],
                        "expiry": iso(e), "days": (e - today()).days, "zone": s["zone"]})
    return sorted(out, key=lambda x: x["expiry"])


# ---------------------------------------------------------------- liste de courses & Alexa
_alexa_kick = threading.Event()


def sync_alexa_soon():
    _alexa_kick.set()


def alexa_items(ent):
    r = ha("POST", "/api/services/todo/get_items?return_response", {"entity_id": ent, "status": "needs_action"})
    resp = (r or {}).get("service_response", r) or {}
    return (resp.get(ent) or {}).get("items", [])


def sync_alexa():
    with LOCK:
        d = load_db()
        ent = d["settings"].get("alexa_entity", "").strip()
    if not ent or not ha_ok():
        return {"ok": False, "message": "liste Alexa non configurée"}
    remote = alexa_items(ent)
    to_add, to_done = [], []
    with LOCK:
        d = load_db()
        by_uid = {i.get("alexa_uid"): i for i in d["shopping"] if i.get("alexa_uid")}
        open_local = [i for i in d["shopping"] if not i.get("done")]
        seen = set()
        for r in remote:
            uid, summary = r.get("uid") or r.get("summary"), (r.get("summary") or "").strip()
            seen.add(uid)
            if uid in by_uid:
                continue
            match = next((i for i in open_local if not i.get("alexa_uid") and norm(i["name"]) == norm(summary)), None)
            if match:
                match["alexa_uid"] = uid
                continue
            p = product_by_name(d, summary)
            d["shopping"].append({"id": new_id(), "product_id": p["id"] if p else None, "name": summary, "qty": 1,
                                  "source": "alexa", "done": False, "added": now().isoformat(timespec="seconds"),
                                  "alexa_uid": uid})
        for i in d["shopping"]:
            if i.get("done") and i.get("alexa_uid") and i["alexa_uid"] in seen and not i.get("alexa_closed"):
                to_done.append(i)
            elif not i.get("done") and i.get("alexa_uid") and i["alexa_uid"] not in seen:
                i["done"], i["done_by"] = True, "alexa"  # coché ou supprimé côté Alexa
            elif not i.get("done") and not i.get("alexa_uid") and not i.get("alexa_pushed"):
                to_add.append(i)
        save_db(d)
    for i in to_add:
        try:
            ha("POST", "/api/services/todo/add_item", {"entity_id": ent, "item": i["name"]})
            mark(i["id"], alexa_pushed=True, alexa_uid=i["name"])
        except Exception as e:
            log("Alexa ajout :", e)
    for i in to_done:
        try:
            ha("POST", "/api/services/todo/update_item", {"entity_id": ent, "item": i["alexa_uid"], "status": "completed"})
        except Exception:
            try:
                ha("POST", "/api/services/todo/remove_item", {"entity_id": ent, "item": i["name"]})
            except Exception as e:
                log("Alexa coche :", e)
        mark(i["id"], alexa_closed=True)
    return {"ok": True, "message": f"Alexa synchronisée ({len(remote)} article(s) sur la liste)"}


def mark(item_id, **kw):
    with LOCK:
        d = load_db()
        for i in d["shopping"]:
            if i["id"] == item_id:
                i.update(kw)
        save_db(d)


def shopping_view(d):
    items = [i for i in d["shopping"] if not i.get("done")]
    out = []
    for i in items:
        p = d["products"].get(i.get("product_id") or "") or product_by_name(d, i["name"]) or {}
        q = p.get("drive_name") or i["name"]
        out.append(dict(i, image=p.get("image", ""), stock=stock_qty(d, p["id"]) if p else None,
                        drive_url=p.get("drive_url") or "https://www.intermarche.com/recherche/" + urllib.parse.quote(q)))
    # suggestions : sous le minimum (si pas déjà), produits « sûrement finis »
    listed = {i.get("product_id") for i in items}
    sugg = []
    for p in d["products"].values():
        if p["id"] in listed:
            continue
        mq = float(p.get("min_qty") or 0)
        if mq > 0 and stock_qty(d, p["id"]) < mq:
            sugg.append({"product_id": p["id"], "name": p["name"], "reason": "sous le minimum"})
    for x in doubtful(d):
        if x["product_id"] not in listed:
            sugg.append({"product_id": x["product_id"], "name": x["name"], "reason": "sûrement fini"})
    return out, sugg


def claude_text(d):
    items, _ = shopping_view(d)
    lines = [f"- {fmt_qty(float(i['qty']))} × {i['name']}" + (f" (réf. drive : {d['products'][i['product_id']]['drive_name']})"
             if i.get("product_id") and d["products"].get(i["product_id"], {}).get("drive_name") else "") for i in items]
    return ("Remplis mon panier sur le drive Intermarché (intermarche.com, mon magasin habituel) avec cette liste. "
            "Prends de préférence les produits que j'achète d'habitude, sinon le moins cher au kilo de qualité correcte. "
            "Ne valide PAS la commande et ne paie pas : je vérifie et je valide moi-même.\n\n" + "\n".join(lines))


# ---------------------------------------------------------------- commandes drive
def order_create(d):
    items, _ = shopping_view(d)
    if not items:
        raise ValueError("la liste de courses est vide")
    o = {"id": new_id(), "date": iso(today()), "status": "ordered", "store": d["settings"]["drive_store"],
         "items": [{"item_id": i["id"], "product_id": i.get("product_id"), "name": i["name"], "qty": i["qty"]} for i in items]}
    d["orders"].append(o)
    for i in d["shopping"]:
        if i["id"] in {x["item_id"] for x in o["items"]}:
            i["ordered"] = o["id"]
    return o


def order_receive(d, oid, received):
    """received : liste {item_id, qty, ok}. Les produits arrivent dans le stock, la liste se vide."""
    o = next((x for x in d["orders"] if x["id"] == oid), None)
    if not o:
        raise ValueError("commande introuvable")
    got = {r["item_id"]: r for r in received}
    for it in o["items"]:
        r = got.get(it["item_id"], {"ok": True, "qty": it["qty"]})
        if not r.get("ok", True):
            for s in d["shopping"]:  # manquant : il reste sur la liste
                if s["id"] == it["item_id"]:
                    s.pop("ordered", None)
            continue
        p = d["products"].get(it.get("product_id") or "") or product_by_name(d, it["name"]) \
            or make_product(d, it["name"])
        add_stock(d, p, float(r.get("qty") or it["qty"]), source="drive", kind="purchase")
        for s in d["shopping"]:
            if s["id"] == it["item_id"]:
                s["done"] = True
    o["status"] = "received"
    o["received"] = iso(today())


# ---------------------------------------------------------------- tableau & capteurs HA
def overview(d):
    zones = {z: [] for z in ZONES}
    for s in d["stock"]:
        p = d["products"].get(s["product_id"])
        if not p:
            continue
        e = parse_date(s.get("expiry"))
        zones.setdefault(s["zone"], []).append({
            "stock_id": s["id"], "product_id": p["id"], "name": p["name"], "brand": p.get("brand", ""),
            "image": p.get("image", ""), "qty": s["qty"], "expiry": s.get("expiry"), "opened": s.get("opened"), "est": s.get("est", False),
            "days": (e - today()).days if e else None, "min_qty": p.get("min_qty", 0)})
    for z in zones.values():
        z.sort(key=lambda x: (x["days"] if x["days"] is not None else 999, norm(x["name"])))
    items, sugg = shopping_view(d)
    st = d["state"]
    mode = st.get("mode", "consume") if time.time() - st.get("mode_ts", 0) <= MODE_WINDOW else "consume"
    last = st.get("last_scan")
    if last:
        last = dict(last, name=d["products"].get(last["product_id"], {}).get("name", "?"))
    return {"zones": zones, "zone_labels": ZONE_LABEL, "shopping": items, "suggestions": sugg,
            "expiring": expiring(d), "doubtful": doubtful(d), "orders": [o for o in d["orders"] if o["status"] == "ordered"],
            "unknown": [dict(u, name=d["products"].get(u["product_id"], {}).get("name", "")) for u in d["unknown"]],
            "mode": mode, "mode_label": MODE_LABEL[mode], "last_scan": last, "ha": ha_ok()}


def push_sensors():
    if not ha_ok():
        return
    with LOCK:
        d = load_db()
        ov = overview(d)
    n = sum(len(v) for v in ov["zones"].values())
    for eid, state, attrs in [
        ("sensor.tessfood_stock", n, {"friendly_name": "Tessfood stock", "unit_of_measurement": "produits", "icon": "mdi:fridge"}),
        ("sensor.tessfood_courses", len(ov["shopping"]), {"friendly_name": "Tessfood liste de courses", "icon": "mdi:cart",
                                                          "articles": [f"{i['qty']} × {i['name']}" for i in ov["shopping"]]}),
        ("sensor.tessfood_a_consommer", len(ov["expiring"]), {"friendly_name": "Tessfood à consommer vite", "icon": "mdi:timer-sand",
                                                              "produits": [f"{x['name']} ({x['days']} j)" for x in ov["expiring"]]}),
        ("sensor.tessfood_a_verifier", len(ov["doubtful"]), {"friendly_name": "Tessfood à vérifier", "icon": "mdi:help-circle",
                                                             "produits": [x["name"] for x in ov["doubtful"]]}),
    ]:
        try:
            ha("POST", f"/api/states/{eid}", {"state": state, "attributes": attrs}, timeout=5)
        except Exception as e:
            log("capteur :", e)
            return


# ---------------------------------------------------------------- tâches de fond
def fill_images(limit=8):
    """Ajoute les photos manquantes (Open Food Facts), quelques produits à la fois."""
    with LOCK:
        d = load_db()
        todo = [(p["id"], p["barcodes"][0]) for p in d["products"].values()
                if not p.get("image") and p.get("barcodes") and not p.get("img_tried")][:limit]
    for pid, code in todo:
        info = off_lookup(code) if len(code) >= 8 and not code.startswith("02") else None
        with LOCK:
            d = load_db()
            p = d["products"].get(pid)
            if p:
                p["img_tried"] = True
                if info and info.get("image"):
                    p["image"] = info["image"]
                if p["name"].startswith("Produit inconnu") and info and info.get("name"):
                    p["name"] = info["name"]
                save_db(d)
        time.sleep(1)


def scheduler():
    last_expiry, last_check, door_open_since, door_warned, last_alexa, last_sensors = None, None, None, False, 0, 0
    while True:
        try:
            with LOCK:
                d = load_db()
            s = d["settings"]
            n = now()
            # 1) Alexa toutes les 2 min (ou tout de suite après un scan)
            if s.get("alexa_entity") and (time.time() - last_alexa > 120 or _alexa_kick.is_set()):
                _alexa_kick.clear()
                last_alexa = time.time()
                try:
                    sync_alexa()
                except Exception as e:
                    log("Alexa :", e)
            # 2) produits qui périment : chaque jour à 9 h
            if n.hour >= 9 and last_expiry != iso(today()):
                last_expiry = iso(today())
                ex = expiring(d)
                if ex:
                    notify("Tessfood : à consommer vite",
                           ", ".join(f"{x['name']} ({'périmé' if x['days'] < 0 else 'aujourd’hui' if x['days'] == 0 else str(x['days']) + ' j'})"
                                     for x in ex[:8]), "tessfood-dlc")
            # 3) vérification de la semaine
            if n.weekday() == int(s["check_weekday"]) and n.hour >= int(s["check_hour"]) and last_check != iso(today()):
                last_check = iso(today())
                db = doubtful(d)
                if db:
                    notify("Tessfood : petite vérification",
                           f"{len(db)} produit(s) sûrement finis : " + ", ".join(x["name"] for x in db[:8])
                           + ". Ouvre Tessfood pour confirmer.", "tessfood-check")
            # 4) porte du frigo restée ouverte
            if s.get("door_entity") and ha_ok():
                try:
                    st = ha("GET", f"/api/states/{s['door_entity']}", timeout=5)["state"]
                except Exception:
                    st = None
                if st == "on":
                    door_open_since = door_open_since or time.time()
                    if not door_warned and time.time() - door_open_since > float(s["door_minutes"]) * 60:
                        door_warned = True
                        notify("Frigo ouvert", f"La porte du frigo est ouverte depuis plus de {s['door_minutes']} min.",
                               "tessfood-door")
                        announce("Attention, la porte du frigo est restée ouverte")
                else:
                    door_open_since, door_warned = None, False
            # 5) photos manquantes
            try:
                fill_images()
            except Exception as e:
                log("photos :", e)
            # 6) capteurs Home Assistant
            if time.time() - last_sensors > 300:
                last_sensors = time.time()
                push_sensors()
        except Exception as e:
            log("planificateur :", e)
        _alexa_kick.wait(20)


# ---------------------------------------------------------------- API
def api(method, path, q, body):
    with LOCK:
        d = load_db()
        if method == "GET":
            if path == "/api/overview":
                return overview(d)
            if path == "/api/products":
                ps = sorted(d["products"].values(), key=lambda p: norm(p["name"]))
                return [dict(p, stock=stock_qty(d, p["id"])) for p in ps]
            if path == "/api/settings":
                return dict(d["settings"], scan_token=SCAN_TOKEN, ha=ha_ok())
            if path == "/api/history":
                ms = d["moves"][-150:][::-1]
                return [dict(m, name=d["products"].get(m["product_id"], {}).get("name", "?")) for m in ms]
            if path == "/api/claude":
                return {"text": claude_text(d)}
            if path == "/api/search":
                t = norm(q.get("q", [""])[0])
                return [p for p in d["products"].values() if t and t in norm(p["name"] + " " + p.get("brand", ""))][:15]
            raise LookupError(path)

        # --- POST
        if path == "/api/scan":
            pass  # traité hors verrou ci-dessous
        elif path == "/api/stock/add":
            p = d["products"].get(body.get("product_id") or "")
            if not p:
                name = (body.get("name") or "").strip()
                if not name:
                    raise ValueError("nom manquant")
                p = product_by_name(d, name) if not body.get("force_new") else None
                p = p if p and norm(p["name"]) == norm(name) else make_product(d, name, zone=body.get("zone"))
            add_stock(d, p, float(body.get("qty") or 1), body.get("zone"), body.get("expiry"))
            save_db(d)
            return {"ok": True, "product_id": p["id"]}
        elif path == "/api/stock/take":
            p = d["products"][body["product_id"]]
            take_stock(d, p, float(body.get("qty") or 1), body.get("kind", "consume"))
            item = check_min(d, p)
            save_db(d)
            if item:
                sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/stock/open":
            open_stock(d, d["products"][body["product_id"]])
            save_db(d)
            return {"ok": True}
        elif path == "/api/stock/edit":
            s = next(x for x in d["stock"] if x["id"] == body["stock_id"])
            for k in ("zone", "expiry", "qty"):
                if k in body:
                    s[k] = float(body[k]) if k == "qty" else (body[k] or None)
            if "expiry" in body:
                s["est"] = False  # date lue sur l'emballage
            d["stock"] = [x for x in d["stock"] if x["qty"] > 0]
            save_db(d)
            return {"ok": True}
        elif path == "/api/product/save":
            p = d["products"][body["id"]]
            for k in ("name", "brand", "zone", "drive_name", "drive_url", "image"):
                if k in body:
                    p[k] = str(body[k]).strip()
            for k in ("min_qty", "open_days", "keep_days"):
                if k in body:
                    p[k] = float(body[k] or 0)
            if body.get("add_barcode"):
                code = re.sub(r"\s+", "", body["add_barcode"])
                other = product_by_code(d, code)
                if other and other["id"] != p["id"]:
                    other["barcodes"].remove(code)
                p["barcodes"].append(code)
            d["unknown"] = [u for u in d["unknown"] if u["product_id"] != p["id"] or p["name"].startswith("Produit inconnu")]
            save_db(d)
            return {"ok": True}
        elif path == "/api/product/import":
            # import de produits (ex. produits habituels du drive) : fiches seulement, sans toucher au stock
            n_new = n_upd = 0
            for x in body.get("items", []):
                code = re.sub(r"\D", "", str(x.get("ean", "")))
                p = product_by_code(d, code) if code else None
                if not p:
                    p = make_product(d, x.get("name") or code, code or None,
                                     {"brand": x.get("brand", ""), "quantity": x.get("qty", "")}, x.get("zone"))
                    n_new += 1
                else:
                    n_upd += 1
                for k in ("drive_name", "drive_url"):
                    if x.get(k):
                        p[k] = x[k]
            save_db(d)
            return {"ok": True, "new": n_new, "updated": n_upd}
        elif path == "/api/product/merge":
            # un « produit inconnu » scanné est en fait un produit qui existe déjà
            src, dst = d["products"].pop(body["from"]), d["products"][body["into"]]
            dst["barcodes"] = list(dict.fromkeys(dst["barcodes"] + src["barcodes"]))
            for coll in (d["stock"], d["moves"], d["shopping"]):
                for x in coll:
                    if x.get("product_id") == src["id"]:
                        x["product_id"] = dst["id"]
            d["unknown"] = [u for u in d["unknown"] if u["product_id"] != src["id"]]
            save_db(d)
            return {"ok": True}
        elif path == "/api/product/delete":
            pid = body["id"]
            d["products"].pop(pid, None)
            d["stock"] = [x for x in d["stock"] if x["product_id"] != pid]
            d["unknown"] = [u for u in d["unknown"] if u["product_id"] != pid]
            save_db(d)
            return {"ok": True}
        elif path == "/api/check":
            # vérification : still = encore là ; sinon fini
            p = d["products"][body["product_id"]]
            if body.get("still"):
                d["checks"][p["id"]] = iso(today())
            else:
                take_stock(d, p, stock_qty(d, p["id"]), "consume", "check")
                check_min(d, p)
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/shopping/add":
            name = (body.get("name") or "").strip()
            p = d["products"].get(body.get("product_id") or "") or (product_by_name(d, name) if name else None)
            name = name or (p["name"] if p else "")
            if not name:
                raise ValueError("nom manquant")
            d["shopping"].append({"id": new_id(), "product_id": p["id"] if p else None, "name": name if not p or body.get("name") is None else (p["name"] if norm(p["name"]) == norm(name) else name),
                                  "qty": float(body.get("qty") or 1), "source": "manual", "done": False,
                                  "added": now().isoformat(timespec="seconds"), "alexa_uid": None})
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/shopping/update":
            for i in d["shopping"]:
                if i["id"] == body["id"]:
                    for k in ("qty", "name", "done"):
                        if k in body:
                            i[k] = body[k]
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/order/create":
            o = order_create(d)
            save_db(d)
            return {"ok": True, "order": o}
        elif path == "/api/order/receive":
            order_receive(d, body["id"], body.get("items", []))
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/order/cancel":
            d["orders"] = [o for o in d["orders"] if o["id"] != body["id"]]
            for i in d["shopping"]:
                if i.get("ordered") == body["id"]:
                    i.pop("ordered")
            save_db(d)
            return {"ok": True}
        elif path == "/api/settings":
            for k, v in body.items():
                if k in DEFAULT_SETTINGS:
                    d["settings"][k] = type(DEFAULT_SETTINGS[k])(v) if v != "" or isinstance(DEFAULT_SETTINGS[k], str) else DEFAULT_SETTINGS[k]
            save_db(d)
            return {"ok": True}
        elif path == "/api/alexa/sync":
            pass
        elif path == "/api/test-notify":
            pass
        else:
            raise LookupError(path)
    # hors verrou (appels réseau)
    if path == "/api/scan":
        return scan(body.get("code"), source=body.get("source", "app"))
    if path == "/api/alexa/sync":
        return sync_alexa()
    if path == "/api/test-notify":
        return {"ok": notify("Tessfood", "Test : les notifications fonctionnent 👍")}


class Handler(BaseHTTPRequestHandler):
    scan_only = False

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            return {}
        raw = self.rfile.read(n) if n else b""
        try:
            d = json.loads(raw or b"{}")
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
        # le scanner peut envoyer juste le code en texte brut
        return {"code": raw.decode("utf-8", "ignore").strip()}

    def _route(self, method):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)
        try:
            if self.scan_only:
                if path in ("/scan", "/api/scan"):
                    body = self._body() if method == "POST" else {}
                    code = body.get("code") or q.get("code", [""])[0]
                    token = self.headers.get("X-Token") or body.get("token") or q.get("token", [""])[0]
                    if not SCAN_TOKEN or token != SCAN_TOKEN:
                        return self._send(403, {"ok": False, "message": "jeton du scanner incorrect"})
                    return self._send(200, scan(code, "scanner"))
                if path == "/health":
                    return self._send(200, {"ok": True})
                return self._send(404, {"ok": False})
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(BASE, "web", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and path == "/cartes":
                with open(os.path.join(BASE, "web", "cartes.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, {"ok": False})
            body = self._body() if method == "POST" else {}
            return self._send(200, api(method, path, q, body))
        except LookupError as e:
            return self._send(404, {"ok": False, "message": f"introuvable : {e}"})
        except (ValueError, KeyError, StopIteration) as e:
            return self._send(400, {"ok": False, "message": str(e) or "requête incorrecte"})
        except Exception as e:
            log("erreur :", repr(e))
            return self._send(500, {"ok": False, "message": str(e)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


class ScanHandler(Handler):
    scan_only = True


def main():
    threading.Thread(target=scheduler, daemon=True).start()
    if SCAN_TOKEN:
        sp = int(os.environ.get("SCAN_PORT", "8098"))
        threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", sp), ScanHandler).serve_forever, daemon=True).start()
        log(f"Tessfood : scanner sur le port {sp}")
    port = int(os.environ.get("PORT", "8099"))
    log(f"Tessfood : appli sur le port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
