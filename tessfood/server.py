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
MODE_CARDS = {"TF-SORTIE": "consume", "TF-AJOUT": "add", "TF-OUVERT": "open", "TF-JETE": "waste", "TF-FINI": "empty",
              "TF-ANNULER": "undo"}
MODE_LABEL = {"consume": "Sortie du stock", "add": "Ajout au stock", "open": "Produit ouvert", "waste": "Jeté (périmé)",
              "empty": "Tout est fini"}
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
    "auto_list": False,            # True = un produit sous son minimum part tout seul sur la liste (sinon : suggestion)
}


# ---------------------------------------------------------------- outils
def now():
    return dt.datetime.now()


def today():
    return dt.date.today()


def iso(d):
    return d.isoformat() if d else None


def norm(s):
    s = (s or "").lower().replace("œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def new_id():
    return uuid.uuid4().hex[:10]


UNDO = []  # instantanés de la base avant chaque modification (en mémoire, 20 derniers)
UNDO_LABEL = {"/api/recipes/cooked": "recette cuisinée", "/api/suggest/dismiss": "suggestion ignorée", "/api/stock/take": "sortie du stock", "/api/stock/add": "ajout au stock", "/api/stock/open": "produit ouvert",
              "/api/stock/edit": "modification", "/api/product/save": "fiche produit", "/api/product/delete": "suppression du produit",
              "/api/product/merge": "fusion de produits", "/api/scan/relink": "rattachement du code", "/api/check": "vérification", "/api/shopping/update": "liste de courses",
              "/api/shopping/add": "liste de courses", "/api/order/receive": "rangement des courses", "/api/recipes/cook": "recette cuisinée",
              "/api/recipes/delete": "suppression de recette", "/api/scan": "scan", "/api/order/create": "commande"}


def snapshot(path):
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            UNDO.append((path, f.read(), time.time()))
        del UNDO[:-20]
    except OSError:
        pass


def undo():
    if not UNDO:
        return {"ok": False, "message": "Rien à annuler"}
    path, raw, _ = UNDO.pop()
    with LOCK:
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, DB_PATH)
    return {"ok": True, "message": f"Annulé : {UNDO_LABEL.get(path, 'dernière action')}"}


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
    for x in d["stock"]:  # quantités en unités entières pour les paquets (ex. 9,1/12 -> 9/12)
        pr = d["products"].get(x["product_id"])
        u = units_of(pr) if pr else 1
        if u > 1:
            x["qty"] = round(round(x["qty"] * u) / u, 6)
    d["stock"] = [x for x in d["stock"] if x["qty"] > 1e-4]
    for x in d["stock"]:
        if x.get("zone") == "congelateur" and x.get("est"):
            x["expiry"], x["est"] = None, False
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


# ---------------------------------------------------------------- rayons
RAYONS = [
    ("Plats préparés", ("pizza", "lasagne", "risotto", "basquaise", "poelee", "feuillete", "tarte", "croqueta", "sandwich",
                        "ravioli", "tortellini", "quiche", "pate brisee", "pate feuilletee", "galettes de ble")),
    ("Yaourts & desserts", ("yaourt", "dessert", "liegeois", "mousse", "paturette", "frulos", "yab", "suiss", "fromage blanc", "fromage frais",
                            "paquito", "gourde", "compote", "creme dessert")),
    ("Fromages", ("fromage", "emmental", "comte", "camembert", "coulommiers", "cheddar", "mozzarella", "babybel",
                  "raclette", "morbier", "tomme", "felicien", "gouda", "mimolette")),
    ("Lait, beurre, crème & œufs", ("lait", "beurre", "creme", "oeuf")),
    ("Viandes & poissons", ("steak", "hache", "poulet", "dinde", "volaille", "porc", "boeuf", "poitrine", "saumon", "colin",
                            "poisson", "jacques", "boulette", "cordon", "nugget", "viande", "foie", "pot au feu", "epaule",
                            "thon", "surimi", "coraya")),
    ("Charcuterie", ("jambon", "lardon", "allumette", "saucisse", "saucisson", "fuet", "chorizo", "rillette", "knack", "terrine")),
    ("Fruits & légumes", ("salade", "mache", "mesclun", "tomate", "carotte", "oignon", "champignon", "concombre", "laitue",
                          "pomme de terre", "pommes de terre", "banane", "pomme golden", "brocoli", "chou", "epinard", "poivron", "poireau",
                          "haricot", "petits pois", "legume", "betterave", "mais", "flageolet", "macedoine", "ratatouille")),
    ("Pâtes, riz & féculents", ("pate", "spaghetti", "macaroni", "coquillette", "farfalle", "fusilli", "tagliatelle", "riz",
                                "couscous", "ble", "perles", "crozets", "gnocchi", "semoule", "nouille", "frites", "rosti",
                                "pommes noisettes", "cubes de pommes")),
    ("Sauces & condiments", ("sauce", "mayonnaise", "moutarde", "ketchup", "vinaigre", "huile", "tabasco", "chutney",
                             "cornichon", "citron", "soja", "bouillon", "fond de", "bechamel", "puree de tomates")),
    ("Épices & aides", ("epice", "paprika", "curry", "cumin", "curcuma", "poivre", "sel", "herbes", "thym", "muscade", "ail",
                        "baies", "levure", "farine", "sucre", "chapelure", "maizena", "pepites")),
    ("Petit-déjeuner & sucré", ("chocapic", "cereale", "coco pops", "tresor", "nesquik", "nutella", "chocolat", "cafe", "the ",
                                "brioche", "gache", "madeleine", "bretonne", "palet", "biscuit", "cracotte", "miel",
                                "confiture", "pain au chocolat", "moelleux", "ecolier", "rollos", "glace", "sorbet")),
    ("Pain", ("pain", "baguette", "viennoiserie", "wrap", "canape", "burger")),
    ("Apéritif & bonbons", ("chips", "crok", "tub s", "crackers", "cacahuete", "melange exotique", "gaufrette", "haribo",
                            "bonbon", "dragibus", "chewing", "sucette", "smarties", "maoam", "ourson", "fils")),
]
RAYONS_BOISSONS = [("Bières", ("biere", "leffe", "bud", "grimbergen", "affligem", "goudale", "pelforth", "desperados",
                                "stella", "tourtel", "softiz", "picon", "secret des moines")),
                   ("Vins & alcools", ("vin", "whisky", "rhum", "ricard", "gin", "porto", "cotes", "chinon", "listel")),
                   ("Softs & eaux", ("eau", "cola", "coca", "schweppes", "fanta", "jus", "sirop", "look", "pellegrino", "perrier"))]


def rayon_of(p):
    if p.get("rayon"):
        return p["rayon"]
    t = " " + norm(p.get("name", "") + " " + p.get("brand", "")) + " "
    z = p.get("zone")
    if z == "maison":
        return "Hygiène & entretien"
    table = RAYONS_BOISSONS + RAYONS if z == "cellier" else RAYONS + RAYONS_BOISSONS
    for r, words in table:
        if any(w in t for w in words):
            return r
    return "Divers"


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
    best, score = None, 0
    for p in d["products"].values():
        pn = norm(p["name"])
        if pn == n:
            return p
        # « oeufs » -> « Œufs frais » plutôt que « Tagliatelles aux œufs » ; on préfère ce qui est en stock
        sc = 0
        if pn.startswith(n + " ") or pn.startswith(n.rstrip("s") + " "):
            sc = 3
        elif n in pn.split() or n.rstrip("s") in [w.rstrip("s") for w in pn.split()]:
            sc = 2
        elif len(n) > 3 and (n in pn or pn in n):
            sc = 1
        if sc and stock_qty(d, p["id"]) > 0:
            sc += 0.5
        if sc > score:
            best, score = p, sc
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
        return 0, 0  # au congélateur, pas de date ni d'alerte
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


def guess_units(qty_text, name=""):
    """« 8 × 125 g » -> 8, « 20 œufs » -> 20, « 6 × 1 L » -> 6 ; sinon 1."""
    t = (qty_text or "") + " " + (name or "")
    m = re.search(r"(\d+)\s*[x×]\s*\d", t)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"(\d+)\s*(oeufs|œufs|pots|bouteilles|briques|canettes|gourdes|sachets|portions|cones|cônes|batonnets|bâtonnets)", t, re.I)
    return max(1, int(m.group(1))) if m else 1


def units_of(p):
    u = p.get("units")
    return int(u) if u else guess_units(p.get("quantity"), p.get("name"))


def stock_qty(d, pid):
    return round(sum(s["qty"] for s in d["stock"] if s["product_id"] == pid), 2)


def add_move(d, pid, delta, kind, source):
    d["moves"].append({"ts": now().isoformat(timespec="seconds"), "product_id": pid, "delta": delta, "kind": kind,
                       "source": source})
    d["checks"].pop(pid, None) if kind in ("add", "purchase") else None


def add_stock(d, p, qty=1, zone=None, expiry=None, source="app", kind="add", stock_id=None):
    line = next((s for s in d["stock"] if s["id"] == stock_id and s["product_id"] == p["id"]), None) if stock_id else None
    if not line and not expiry and units_of(p) > 1 and abs(qty * units_of(p) - round(qty * units_of(p))) < 1e-6 and qty < 1:
        # quelques unités (ex. 1 œuf) : on complète la boîte déjà là plutôt que d'en créer une nouvelle
        line = next((s for s in d["stock"] if s["product_id"] == p["id"] and s["zone"] == (zone or s["zone"])), None)
    if line:
        line["qty"] = round(line["qty"] + qty, 6)
        add_move(d, p["id"], qty, kind, source)
        return
    exp, est = parse_date(expiry), False
    if not exp and p.get("keep_days"):
        exp, est = today() + dt.timedelta(days=int(p["keep_days"])), True
    zone = zone or p.get("zone") or "placard"
    for s in d["stock"]:  # on regroupe avec une ligne identique (même zone, même date, non ouverte)
        if s["product_id"] == p["id"] and s["zone"] == zone and s.get("expiry") == iso(exp) and not s.get("opened"):
            s["qty"] = round(s["qty"] + qty, 6)
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
        s["qty"] = round(s["qty"] - t, 6)
        left = round(left - t, 6)
    d["stock"] = [s for s in d["stock"] if s["qty"] > 1e-4]
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


def family_qty(d, p):
    """Stock du produit + des produits identiques sous un autre format (ex. Ricard 70 cl et Ricard 1 L)."""
    key = base_name(p["name"])
    return sum(stock_qty(d, x["id"]) for x in d["products"].values() if x["id"] == p["id"] or base_name(x["name"]) == key)


def base_name(name):
    """Nom sans le format : « Ricard 70 cl » et « Ricard 1 L » -> « ricard »."""
    t = norm(name)
    t = re.sub(r"\b\d+([ ,.]\d+)?\s*(x|l|cl|ml|g|kg|pieces?|tranches?|pots?|bouteilles?|briques?)\b", " ", t)
    t = re.sub(r"\b\d+\b", " ", t)
    return " ".join(t.split())


def check_min(d, p):
    """Sous le minimum -> ajouté à la liste de courses (et à Alexa)."""
    mq = float(p.get("min_qty") or 0)
    q = family_qty(d, p)
    low = q < mq if mq > 0 else q <= 0
    if not low:
        return None
    if not (p.get("essential") or (mq > 0 and d["settings"].get("auto_list"))):
        return None  # pas un produit de base : il ira seulement dans les suggestions
    if any(not i.get("done") and i.get("product_id") == p["id"] for i in d["shopping"]):
        return None
    it = {"id": new_id(), "product_id": p["id"], "name": p["name"], "qty": max(1, round(mq - stock_qty(d, p["id"]))),
          "source": "auto", "done": False, "added": now().isoformat(timespec="seconds"), "alexa_uid": None}
    d["shopping"].append(it)
    return it


# ---------------------------------------------------------------- scanner
def scan(code, source="scanner", default_mode="consume", amount=None):
    default_mode = default_mode if default_mode in MODE_LABEL else "consume"
    code = re.sub(r"\s+", "", str(code or ""))
    if not code:
        return {"ok": False, "message": "code vide"}
    with LOCK:
        d = load_db()
        st = d["state"]
        mode = st.get("mode", default_mode)
        if time.time() - st.get("mode_ts", 0) > MODE_WINDOW:
            mode = default_mode
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

        if "://" in code or not re.fullmatch(r"[0-9A-Za-z\-]{4,40}", code):
            return {"ok": False, "message": "Ce n'est pas un code-barres produit (QR code ou lien) : vise le code-barres."}
        p = product_by_code(d, code)
        created = False
        if not p:
            save_db(d)
            info = off_lookup(code)  # hors verrou serait mieux, mais c'est rapide
            d = load_db()
            st = d["state"]
            p = product_by_code(d, code)
            if not p and info and info.get("name"):
                p = similar_in_stock(d, info["name"])
                if p:  # ex. une brique de crème vendue en pack : on rattache ce code au produit déjà en stock
                    p["barcodes"].append(code)
            if not p:
                p = make_product(d, info["name"] if info and info["name"] else f"Produit inconnu {code}", code, info)
                created = True
                if not info or not info.get("name"):
                    d["unknown"].append({"code": code, "product_id": p["id"], "ts": now().isoformat(timespec="seconds")})

        warn = ""
        u = units_of(p)
        if amount not in ("unit", "pack", "all"):
            amount = "pack" if p.get("scan_out") == "pack" else "unit"
        step = 1 if amount == "pack" or u <= 1 else 1 / u
        if mode == "empty":
            mode, amount = "consume", "all"
        if mode == "add":
            add_stock(d, p, 1, source=source)  # un paquet acheté
        elif mode == "open":
            if not open_stock(d, p, source):
                add_stock(d, p, 1, source=source)
                open_stock(d, p, source)
                warn = "il n'était pas dans le stock, je l'ai ajouté"
        else:
            q = stock_qty(d, p["id"]) if amount == "all" else step
            if q <= 0 or take_stock(d, p, round(q, 6), "waste" if mode == "waste" else "consume", source):
                warn = "il n'était pas dans le stock"
            check_min(d, p)
        if mode != "consume":
            st["mode_ts"] = time.time()  # on prolonge la carte tant qu'on scanne
        left = stock_qty(d, p["id"])
        st["last_scan"] = {"ts": now().isoformat(timespec="seconds"), "code": code, "product_id": p["id"], "mode": mode}
        save_db(d)
        sync_alexa_soon()
    left_txt = f"{round(left * u)} {'unité' if round(left * u) <= 1 else 'unités'}" if u > 1 else fmt_qty(left)
    msg = f"{p['name']} : {MODE_LABEL[mode].lower()}, il en reste {left_txt}"
    if warn:
        msg += f" ({warn})"
    res = {"ok": True, "mode": mode, "product": p["name"], "product_id": p["id"], "left": left, "units": u,
           "left_units": round(left * u), "left_text": left_txt, "new": created,
           "warning": warn, "message": msg}
    fire_event(res)
    announce(f"{p['name']}, il en reste {left_txt}" if mode == "consume" else f"{p['name']}, {MODE_LABEL[mode].lower()}")
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


FORGOTTEN_DAYS = {"frigo": 21, "congelateur": 90, "placard": 120, "cellier": 180, "maison": 180}


def forgotten(d):
    """Produits en stock auxquels personne n'a touché depuis longtemps (même sans date de péremption)."""
    last = {}
    for m in d["moves"]:
        last[m["product_id"]] = m["ts"][:10]
    out = []
    for s_ in d["stock"]:
        p = d["products"].get(s_["product_id"])
        if not p:
            continue
        ref = max(filter(None, [last.get(p["id"]), s_.get("added"), d["checks"].get(p["id"])]), default=None)
        dref = parse_date(ref)
        if not dref:
            continue
        days = (today() - dref).days
        lim = FORGOTTEN_DAYS.get(s_["zone"], 120)
        if days >= lim and not any(x["product_id"] == p["id"] for x in out):
            out.append({"product_id": p["id"], "name": p["name"], "zone": s_["zone"], "days": days, "image": p.get("image", ""),
                        "since": dref.strftime("%d/%m/%Y")})
    return sorted(out, key=lambda x: -x["days"])


def expiring(d, days=None):
    days = d["settings"]["expiry_warn_days"] if days is None else days
    lim = today() + dt.timedelta(days=int(days))
    out = []
    for s in d["stock"]:
        e = parse_date(s.get("expiry"))
        if s.get("zone") == "congelateur":
            continue  # le congélateur ne déclenche jamais d'alerte
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


def match_in_stock(d, name, pid=None):
    """Produit en stock qui correspond à un article ajouté à la liste (« moutarde » -> Moutarde de Dijon)."""
    if pid and stock_qty(d, pid) > 0:
        return d["products"].get(pid)
    words = [w for w in norm(name).split() if len(w) > 2 and w not in ("des", "les", "une", "pour", "avec")]
    if not words:
        return None
    cands = []
    for p in d["products"].values():
        if stock_qty(d, p["id"]) <= 0:
            continue
        t = " " + norm(p["name"]) + " "
        if all((" " + w) in t or (" " + w.rstrip("s")) in t for w in words):
            cands.append(p)
    if not cands:
        return None
    # le plus proche : nom le plus court, puis le produit le plus récemment utilisé
    last = {m["product_id"]: m["ts"] for m in d["moves"]}
    cands.sort(key=lambda p: (len(norm(p["name"])), "" if p["id"] not in last else "0" + last[p["id"]]))
    return cands[0]


def similar_in_stock(d, name):
    words = {w for w in norm(name).split() if len(w) > 2}
    best, score = None, 0
    for p in d["products"].values():
        if stock_qty(d, p["id"]) <= 0:
            continue
        pw = {w for w in norm(p["name"]).split() if len(w) > 2}
        if not pw or not words:
            continue
        sc = len(words & pw) / min(len(words), len(pw))
        if sc > score:
            best, score = p, sc
    return best if score >= 0.75 else None


def finished_from_list(d, item):
    """Un article ajouté à la liste = il n'y en a plus : on met ce produit à 0 dans le stock."""
    pid = item.get("product_id")
    if pid and pid in d["products"] and stock_qty(d, pid) <= 0:
        return None  # ce produit précis est déjà à 0 : on ne touche pas à un autre format (ex. Ricard 70 cl)
    p = match_in_stock(d, item["name"], pid)
    if not p:
        return None
    take_stock(d, p, stock_qty(d, p["id"]), "consume", "liste")
    item["product_id"] = p["id"]
    item["zeroed"] = p["name"]
    return p["name"]


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
        zeroed_names = []
        open_local = [i for i in d["shopping"] if not i.get("done")]
        seen = set()
        for r in remote:
            uid, summary = r.get("uid") or r.get("summary"), (r.get("summary") or "").strip()
            seen.add(uid)
            if uid in by_uid:
                continue
            match = next((i for i in open_local if not i.get("alexa_uid") and norm(i["name"]) == norm(summary)), None)
            if not match:  # article créé ici puis coché avant d'être relié à Alexa : ce n'est pas un nouvel ajout
                match = next((i for i in d["shopping"] if i.get("done") and not i.get("alexa_uid") and norm(i["name"]) == norm(summary)), None)
            if match:
                match["alexa_uid"] = uid
                continue
            p = product_by_name(d, summary)
            it = {"id": new_id(), "product_id": p["id"] if p else None, "name": summary, "qty": 1,
                  "source": "alexa", "done": False, "added": now().isoformat(timespec="seconds"), "alexa_uid": uid}
            d["shopping"].append(it)
            zeroed = finished_from_list(d, it)
            if zeroed:
                zeroed_names.append(zeroed)
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
    if zeroed_names:
        notify("Tessfood : stock mis à jour", "Ajouté à la liste, donc plus en stock : " + ", ".join(zeroed_names)
               + ". Si c'est une erreur, corrige dans Tessfood.", "tessfood-liste")
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
    # suggestions (rien n'est ajouté tout seul) : finis récemment, sous le minimum, « sûrement finis »
    listed = {i.get("product_id") for i in items}
    dismissed = d.get("dismissed", {})
    last_out, last_in = {}, {}
    for m in d["moves"]:
        if m["kind"] in ("consume", "waste"):
            last_out[m["product_id"]] = m["ts"]
        elif m["kind"] in ("add", "purchase"):
            last_in[m["product_id"]] = m["ts"]
    sugg, seen = [], set()

    def add(p, reason, when):
        if p["id"] in listed or p["id"] in seen or (dismissed.get(p["id"]) or "") >= (when or ""):
            return
        seen.add(p["id"])
        sugg.append({"product_id": p["id"], "name": p["name"], "image": p.get("image", ""), "reason": reason,
                     "essential": bool(p.get("essential"))})
    limit = (now() - dt.timedelta(days=45)).isoformat(timespec="seconds")
    for pid, ts in sorted(last_out.items(), key=lambda x: x[1], reverse=True):
        p = d["products"].get(pid)
        if p and ts >= limit and family_qty(d, p) <= 0:
            add(p, "fini le " + ts[8:10] + "/" + ts[5:7], ts)
    for p in d["products"].values():
        mq = float(p.get("min_qty") or 0)
        if mq > 0 and family_qty(d, p) < mq:
            add(p, "sous le minimum", last_out.get(p["id"], ""))
    for x in doubtful(d):
        p = d["products"].get(x["product_id"])
        if p:
            add(p, "sûrement fini", last_in.get(p["id"], ""))
    return out, sugg


def claude_text(d):
    items, _ = shopping_view(d)
    lines = [f"- {fmt_qty(float(i['qty']))} × {i['name']}" + (f" (réf. drive : {d['products'][i['product_id']]['drive_name']})"
             if i.get("product_id") and d["products"].get(i["product_id"], {}).get("drive_name") else "") for i in items]
    return ("Remplis mon panier sur le drive Intermarché (intermarche.com, mon magasin habituel) avec cette liste. "
            "Prends de préférence les produits que j'achète d'habitude, sinon le moins cher au kilo de qualité correcte. "
            "Ne valide PAS la commande et ne paie pas : je vérifie et je valide moi-même.\n\n" + "\n".join(lines))


# ---------------------------------------------------------------- tâche Claude (remplir le panier sans copier-coller)
ROUTINE_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{rid}/fire"
FIRE_ERRORS = {400: "la tâche Claude est en pause ou la demande est refusée", 401: "jeton de la tâche Claude refusé (génère-en un nouveau)",
               403: "ton compte n'autorise pas ce déclenchement", 404: "tâche Claude introuvable (vérifie son identifiant)",
               429: "trop de lancements : réessaie dans un moment", 500: "erreur côté Claude : réessaie", 503: "Claude est surchargé : réessaie"}


def fire_routine(text):
    rid, token = os.environ.get("ROUTINE_ID", "").strip(), os.environ.get("ROUTINE_TOKEN", "").strip()
    if not rid or not token:
        return {"ok": False, "manual": True, "url": f"https://claude.ai/scheduled-task/{rid}" if rid else "https://claude.ai/code/routines",
                "message": "Appuie sur « Exécuter maintenant » : Claude lit ta liste et remplit le panier."}
    req = urllib.request.Request(ROUTINE_FIRE_URL.format(rid=urllib.parse.quote(rid, safe="")), method="POST",
                                 data=json.dumps({"text": text}).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {token}", "anthropic-version": "2026-10-21",
                                          "Content-Type": "application/json", "User-Agent": "Tessfood"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return {"ok": False, "message": FIRE_ERRORS.get(err.code, "erreur") + f" (code {err.code})"}
    except Exception as e:
        return {"ok": False, "message": f"connexion impossible ({str(e)[:60]})"}
    return {"ok": True, "session_url": res.get("claude_code_session_url"),
            "message": "Claude remplit ton panier Intermarché dans Chrome. Tu recevras un résumé sur ton téléphone."}


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


# ---------------------------------------------------------------- recettes
def _matches(stock_items, keywords):
    out = []
    for it in stock_items:
        n = " " + norm(it["name"]) + " "
        if any(all(w in n for w in norm(k).split()) for k in keywords if norm(k)):
            out.append(it)
    return out


def recipes_view(d):
    items = {}
    for s_ in d["stock"]:
        p = d["products"].get(s_["product_id"])
        if not p or s_["qty"] <= 0:
            continue
        e = parse_date(s_.get("expiry")) if s_.get("zone") != "congelateur" else None
        days = (e - today()).days if e else None
        it = items.setdefault(p["id"], {"product_id": p["id"], "name": p["name"], "qty": 0, "days": days,
                                        "units": units_of(p), "image": p.get("image", "")})
        it["qty"] = round(it["qty"] + s_["qty"], 3)
        if days is not None and (it["days"] is None or days < it["days"]):
            it["days"] = days
    stock_items = list(items.values())
    warn = int(d["settings"].get("expiry_warn_days", 3)) + 2
    out = []
    for r in d.get("recipes", []):
        ings, have, need, urgent = [], 0, 0, []
        for ing in r.get("ingredients", []):
            m = _matches(stock_items, ing.get("match") or [ing["name"]])
            ok = bool(m)
            if not ing.get("optional"):
                need += 1
                have += ok
            for x in m:
                if x["days"] is not None and x["days"] <= warn:
                    urgent.append(f"{x['name']} ({'périmé' if x['days'] < 0 else str(x['days']) + ' j'})")
            ings.append(dict(ing, have=ok, stock=[{"product_id": x["product_id"], "name": x["name"],
                                                    "units": x["units"], "left": round(x["qty"] * x["units"], 1)} for x in m[:3]]))
        score = (have / need if need else 1) + (0.3 if urgent else 0)
        # recette déjà cuisinée : cachée tant que ses produits ne sont pas revenus (rachetés après)
        cooked = r.get("cooked_at")
        restocked = False
        if cooked:
            pids = {x["product_id"] for i in ings for x in i["stock"]}
            restocked = (need - have) == 0 and any(
                m["product_id"] in pids and m["kind"] in ("add", "purchase") and m["ts"] > cooked for m in d["moves"])
        hidden = bool(cooked) and not restocked
        out.append(dict(r, ingredients=ings, have=have, need=need, missing=need - have, hidden=hidden,
                        urgent=list(dict.fromkeys(urgent)), score=round(score, 3)))
    out.sort(key=lambda x: (-x["score"], x.get("time", 99)))
    return {"recipes": out, "claude": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def stock_summary(d):
    lines = []
    for s_ in d["stock"]:
        p = d["products"].get(s_["product_id"])
        if p:
            e = parse_date(s_.get("expiry"))
            lines.append(f"- {p['name']} ({ZONE_LABEL.get(s_['zone'], s_['zone'])})"
                         + (f", à consommer avant {e.strftime('%d/%m')}" if e and (e - today()).days <= 7 else ""))
    return "\n".join(sorted(set(lines)))


def generate_recipes(body):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return recipes_from_book(body)
    with LOCK:
        d = load_db()
        summary = stock_summary(d)
    wish = (body.get("wish") or "").strip()
    prompt = ("Tu es un cuisinier familial français. Voici le contenu actuel de ma cuisine (frigo, congélateur, placards) :\n"
              f"{summary}\n\n"
              "Propose 6 recettes simples du quotidien pour 3 personnes, réalisables surtout avec ces produits, "
              "en utilisant en priorité ceux qui périment bientôt. Unités : uniquement kg, càs, càc ou un nombre de pièces "
              "(jamais g, ml, cup, oz)." + (f" Envie particulière : {wish}." if wish else "") +
              "\nRéponds UNIQUEMENT avec un tableau JSON, sans texte autour, de la forme : "
              '[{"title": "...", "time": 30, "servings": 3, "tags": ["rapide"], '
              '"ingredients": [{"name": "Lardons", "qty": "0,2 kg", "match": ["lardon"], "optional": false}], '
              '"steps": ["...", "..."]}]. Dans "match", mets 1 à 3 mots-clés simples (sans accents) qui se retrouvent '
              "dans le nom du produit en stock.")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
                                 data=json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
                                                  "max_tokens": 6000,
                                                  "messages": [{"role": "user", "content": prompt}]}).encode("utf-8"),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"Claude a répondu {e.code}"}
    except Exception as e:
        return {"ok": False, "message": f"Claude injoignable : {e}"}
    text = "".join(b.get("text", "") for b in resp.get("content", []))
    m = re.search(r"\[.*\]", text, re.S)
    try:
        recs = json.loads(m.group(0)) if m else []
    except ValueError:
        recs = []
    if not recs:
        return {"ok": False, "message": "Réponse de Claude illisible, réessaie."}
    with LOCK:
        d = load_db()
        n = import_recipes(d, recs, "claude")
        save_db(d)
    return {"ok": True, "message": f"{n} nouvelles recettes proposées par Claude"}


def recipes_from_book(body):
    """Sans clé : on pioche dans le carnet de recettes intégré celles qui collent le mieux au stock."""
    try:
        with open(os.path.join(BASE, "recettes_base.json"), encoding="utf-8") as f:
            book = json.load(f)
    except (OSError, ValueError):
        return {"ok": False, "message": "Carnet de recettes introuvable."}
    wish = [w for w in norm(body.get("wish") or "").split() if len(w) > 2]
    with LOCK:
        d = load_db()
        known = {norm(r["title"]) for r in d.get("recipes", [])}
        stock = [{"name": p["name"]} for p in d["products"].values() if stock_qty(d, p["id"]) > 0]
        scored = []
        for r in book:
            if norm(r["title"]) in known:
                continue
            need = [i for i in r["ingredients"] if not i.get("optional")]
            have = sum(1 for i in need if _matches(stock, i["match"]))
            txt = norm(r["title"] + " " + " ".join(r["tags"]) + " " + " ".join(i["name"] for i in r["ingredients"]))
            bonus = 3 if wish and all(w in txt for w in wish) else (-5 if wish else 0)
            scored.append((have / max(1, len(need)) * 10 + bonus, r))
        scored.sort(key=lambda x: -x[0])
        picks = [r for _, r in scored[:6]]
        if not picks:
            return {"ok": True, "message": "Tu as déjà toutes les recettes du carnet."}
        n = import_recipes(d, picks, "carnet")
        save_db(d)
    return {"ok": True, "message": f"{n} nouvelles idées, choisies selon ton stock"}


def import_recipes(d, recs, source="claude"):
    d.setdefault("recipes", [])
    known = {norm(r["title"]) for r in d["recipes"]}
    n = 0
    for r in recs:
        if not r.get("title") or norm(r["title"]) in known:
            continue
        r = {"id": new_id(), "title": r["title"], "time": int(r.get("time") or 30), "servings": int(r.get("servings") or 3),
             "tags": r.get("tags") or [], "ingredients": r.get("ingredients") or [], "steps": r.get("steps") or [],
             "source": source, "added": iso(today()), "fav": False}
        d["recipes"].append(r)
        known.add(norm(r["title"]))
        n += 1
    # on garde les favorites + les 40 plus récentes
    favs = [r for r in d["recipes"] if r.get("fav")]
    rest = [r for r in d["recipes"] if not r.get("fav")][-40:]
    d["recipes"] = favs + rest
    return n


# ---------------------------------------------------------------- tableau & capteurs HA
def overview(d):
    zones = {z: [] for z in ZONES}
    for s in d["stock"]:
        p = d["products"].get(s["product_id"])
        if not p:
            continue
        e = parse_date(s.get("expiry")) if s["zone"] != "congelateur" else None
        zones.setdefault(s["zone"], []).append({
            "stock_id": s["id"], "product_id": p["id"], "name": p["name"], "brand": p.get("brand", ""),
            "image": p.get("image", ""), "qty": s["qty"], "expiry": s.get("expiry"), "opened": s.get("opened"), "est": s.get("est", False),
            "days": (e - today()).days if e else None, "min_qty": p.get("min_qty", 0), "rayon": rayon_of(p),
            "units": units_of(p), "units_left": round(s["qty"] * units_of(p))})
    for z in zones.values():
        z.sort(key=lambda x: (x["rayon"], norm(x["name"])))
    items, sugg = shopping_view(d)
    st = d["state"]
    mode = st.get("mode", "consume") if time.time() - st.get("mode_ts", 0) <= MODE_WINDOW else "consume"
    last = st.get("last_scan")
    if last:
        last = dict(last, name=d["products"].get(last["product_id"], {}).get("name", "?"))
    return {"zones": zones, "zone_labels": ZONE_LABEL, "shopping": items, "suggestions": sugg,
            "expiring": expiring(d), "doubtful": doubtful(d), "forgotten": forgotten(d), "orders": [o for o in d["orders"] if o["status"] == "ordered"],
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
                db, fg = doubtful(d), forgotten(d)
                if db or fg:
                    parts = []
                    if db:
                        parts.append(f"{len(db)} sûrement fini(s) : " + ", ".join(x["name"] for x in db[:5]))
                    if fg:
                        parts.append(f"{len(fg)} pas touché(s) depuis longtemps, pensez à les consommer : "
                                     + ", ".join(f"{x['name']} ({x['days']} j)" for x in fg[:5]))
                    notify("Tessfood : petite vérification", " · ".join(parts) + ". Ouvre Tessfood pour confirmer.",
                           "tessfood-check")
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


# ---------------------------------------------------------------- retour de Claude (panier drive)
REPORT_ENT = "todo.retour_claude"


def claude_report(method, path, body):
    """Claude écrit son compte rendu du panier dans la liste HA « Retour Claude » ; on l'affiche dans Courses."""
    if not ha_ok():
        return {"items": []}
    if method == "GET":
        try:
            r = ha("POST", "/api/services/todo/get_items?return_response", {"entity_id": REPORT_ENT, "status": "needs_action"})
        except Exception as e:
            return {"items": [], "error": str(e)}
        resp = (r or {}).get("service_response", r) or {}
        items = [{"uid": i.get("uid"), "text": i.get("summary", "")} for i in (resp.get(REPORT_ENT) or {}).get("items", [])
                 if not i.get("summary", "").startswith("TEST")]
        return {"items": items}
    if path == "/api/claude/report/clear":
        uids = body.get("uids") or [i["uid"] for i in claude_report("GET", "", {})["items"]]
        if uids:
            ha("POST", "/api/services/todo/remove_item", {"entity_id": REPORT_ENT, "item": uids})
        return {"ok": True}
    if path == "/api/claude/report/choose":
        # Thomas a choisi une des propositions : on la retient pour la prochaine fois
        article, choice = (body.get("article") or "").strip(), (body.get("choice") or "").strip()
        with LOCK:
            d = load_db()
            p = product_by_name(d, article)
            if p:
                p["drive_name"] = choice
                p["drive_url"] = "https://www.intermarche.com/recherche/" + urllib.parse.quote(choice)
                save_db(d)
            ent = d["settings"].get("alexa_entity", "").strip()
        if ent:  # on précise l'article sur la liste de courses pour que Claude le prenne au prochain passage
            try:
                for it in alexa_items(ent):
                    if norm(it.get("summary", "")) == norm(article) or norm(article) in norm(it.get("summary", "")):
                        ha("POST", "/api/services/todo/update_item",
                           {"entity_id": ent, "item": it["uid"], "rename": f"{it['summary']} (réf. drive : {choice})"})
                        break
            except Exception as e:
                log("choix drive :", e)
        if body.get("uid"):
            ha("POST", "/api/services/todo/remove_item", {"entity_id": REPORT_ENT, "item": [body["uid"]]})
        return {"ok": True, "message": f"Retenu : {choice}"}
    raise LookupError(path)


# ---------------------------------------------------------------- API
def api(method, path, q, body):
    if path.startswith("/api/claude/report"):
        return claude_report(method, path, body)
    if method == "POST" and path == "/api/undo":
        return undo()
    if method == "POST" and path in UNDO_LABEL:
        with LOCK:
            snapshot(path)
    with LOCK:
        d = load_db()
        if method == "GET":
            if path == "/api/overview":
                return overview(d)
            if path == "/api/products":
                ps = sorted(d["products"].values(), key=lambda p: norm(p["name"]))
                return [dict(p, stock=stock_qty(d, p["id"]), units=units_of(p), rayon=rayon_of(p)) for p in ps]
            if path == "/api/settings":
                return dict(d["settings"], scan_token=SCAN_TOKEN, ha=ha_ok(),
                            routine=bool(os.environ.get("ROUTINE_ID") and os.environ.get("ROUTINE_TOKEN")))
            if path == "/api/history":
                ms = d["moves"][-150:][::-1]
                return [dict(m, name=d["products"].get(m["product_id"], {}).get("name", "?")) for m in ms]
            if path == "/api/clientlog":
                return d["state"].get("clientlog", [])
            if path == "/api/claude":
                return {"text": claude_text(d)}
            if path == "/api/lookup":
                code = re.sub(r"\s+", "", q.get("code", [""])[0])
                p = product_by_code(d, code)
                if not p:
                    return {"known": False, "code": code}
                return {"known": True, "product_id": p["id"], "name": p["name"], "image": p.get("image", ""),
                        "units": units_of(p), "qty": stock_qty(d, p["id"]),
                        "left_units": round(stock_qty(d, p["id"]) * units_of(p))}
            if path == "/api/recipes":
                return recipes_view(d)
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
                p = p if p and (body.get("match") or norm(p["name"]) == norm(name)) else make_product(d, name, zone=body.get("zone"))
            qty = float(body["units"]) / units_of(p) if body.get("units") else float(body.get("qty") or 1)
            add_stock(d, p, round(qty, 6), body.get("zone"), body.get("expiry"), stock_id=body.get("stock_id"))
            save_db(d)
            return {"ok": True, "product_id": p["id"]}
        elif path == "/api/stock/take":
            p = d["products"][body["product_id"]]
            if body.get("all"):
                qty = stock_qty(d, p["id"])
            elif body.get("units"):
                qty = float(body["units"]) / units_of(p)
            else:
                qty = float(body.get("qty") or 1)
            if qty > 0:
                take_stock(d, p, round(qty, 6), body.get("kind", "consume"))
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
                    if k == "qty":
                        u = units_of(d["products"].get(s["product_id"], {}))
                        s[k] = round(round(float(body[k]) * u) / u, 6)  # toujours un nombre rond d'unités
                    else:
                        s[k] = body[k] or None
            if "expiry" in body:
                s["est"] = False  # date lue sur l'emballage
            d["stock"] = [x for x in d["stock"] if x["qty"] > 0]
            save_db(d)
            return {"ok": True}
        elif path == "/api/product/save":
            p = d["products"][body["id"]]
            for k in ("name", "brand", "zone", "drive_name", "drive_url", "image", "rayon"):
                if k in body:
                    p[k] = str(body[k]).strip()
            if "essential" in body:
                p["essential"] = bool(body["essential"])
                if p["essential"]:
                    check_min(d, p)
            if "units" in body:
                p["units"] = max(1, int(float(body["units"] or 1)))
            if "scan_out" in body:
                p["scan_out"] = "pack" if body["scan_out"] == "pack" else "unit"
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
        elif path == "/api/scan/relink":
            # un code scanné a créé un nouveau produit, mais c'est en fait un produit déjà en stock (ex. une Affligem)
            src, dst = d["products"].get(body["from"]), d["products"][body["into"]]
            mode = body.get("mode", "consume")
            if src and src["id"] != dst["id"]:
                dst["barcodes"] = list(dict.fromkeys(dst["barcodes"] + src["barcodes"]))
                d["stock"] = [x for x in d["stock"] if x["product_id"] != src["id"]]
                d["moves"] = [x for x in d["moves"] if x.get("product_id") != src["id"]]
                d["unknown"] = [u for u in d["unknown"] if u["product_id"] != src["id"]]
                for x in d["shopping"]:
                    if x.get("product_id") == src["id"]:
                        x["product_id"] = dst["id"]
                del d["products"][src["id"]]
            u = units_of(dst)
            if mode == "add":
                add_stock(d, dst, 1, source="camera")
            elif mode == "open":
                open_stock(d, dst, "camera")
            elif mode != "none" and stock_qty(d, dst["id"]) > 0:
                take_stock(d, dst, round(1 / u if u > 1 else 1, 6), "waste" if mode == "waste" else "consume", "camera")
                check_min(d, dst)
            left = stock_qty(d, dst["id"])
            save_db(d)
            sync_alexa_soon()
            return {"ok": True, "product": dst["name"], "product_id": dst["id"], "units": u,
                    "left_text": f"{round(left * u)} unité(s)" if u > 1 else fmt_qty(round(left, 3))}
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
            it = {"id": new_id(), "product_id": p["id"] if p else None, "name": name if not p or body.get("name") is None else (p["name"] if norm(p["name"]) == norm(name) else name),
                  "qty": float(body.get("qty") or 1), "source": "manual", "done": False,
                  "added": now().isoformat(timespec="seconds"), "alexa_uid": None}
            d["shopping"].append(it)
            zeroed = finished_from_list(d, it) if not body.get("keep_stock") else None
            save_db(d)
            sync_alexa_soon()
            return {"ok": True, "zeroed": zeroed}
        elif path == "/api/shopping/update":
            for i in d["shopping"]:
                if i["id"] == body["id"]:
                    for k in ("qty", "name", "done"):
                        if k in body:
                            i[k] = body[k]
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path == "/api/product/essentials":
            # marque d'un coup les produits de base d'après des mots-clés
            n = 0
            for p in d["products"].values():
                t = " " + norm(p["name"]) + " "
                if any(norm(k) in t for k in body.get("keywords", [])) and not p.get("essential"):
                    p["essential"] = True
                    n += 1
            save_db(d)
            return {"ok": True, "marked": n}
        elif path == "/api/suggest/dismiss":
            d.setdefault("dismissed", {})[body["product_id"]] = now().isoformat(timespec="seconds")
            save_db(d)
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
                    if isinstance(DEFAULT_SETTINGS[k], bool):
                        d["settings"][k] = v in (True, "true", "1", 1, "on")
                    else:
                        d["settings"][k] = type(DEFAULT_SETTINGS[k])(v) if v != "" or isinstance(DEFAULT_SETTINGS[k], str) else DEFAULT_SETTINGS[k]
            save_db(d)
            return {"ok": True}
        elif path == "/api/clientlog":
            d["state"].setdefault("clientlog", []).append(dict(body, ts=now().isoformat(timespec="seconds")))
            d["state"]["clientlog"] = d["state"]["clientlog"][-20:]
            save_db(d)
            return {"ok": True}
        elif path == "/api/recipes/import":
            n = import_recipes(d, body.get("recipes", []), body.get("source", "claude"))
            save_db(d)
            return {"ok": True, "added": n}
        elif path == "/api/recipes/delete":
            d["recipes"] = [r for r in d.get("recipes", []) if r["id"] != body["id"]]
            save_db(d)
            return {"ok": True}
        elif path == "/api/recipes/fav":
            for r in d.get("recipes", []):
                if r["id"] == body["id"]:
                    r["fav"] = not r.get("fav")
            save_db(d)
            return {"ok": True}
        elif path == "/api/recipes/missing":
            r = next(x for x in recipes_view(d)["recipes"] if x["id"] == body["id"])
            n = 0
            listed = {norm(i["name"]) for i in d["shopping"] if not i.get("done")}
            for ing in r["ingredients"]:
                if not ing["have"] and not ing.get("optional") and norm(ing["name"]) not in listed:
                    p = product_by_name(d, ing["name"])
                    d["shopping"].append({"id": new_id(), "product_id": p["id"] if p else None, "name": ing["name"],
                                          "qty": 1, "source": "manual", "done": False,
                                          "added": now().isoformat(timespec="seconds"), "alexa_uid": None})
                    n += 1
            save_db(d)
            sync_alexa_soon()
            return {"ok": True, "added": n}
        elif path == "/api/recipes/cooked":
            for r in d.get("recipes", []):
                if r["id"] == body["id"]:
                    r["cooked_at"] = None if body.get("reset") else now().isoformat(timespec="seconds")
            save_db(d)
            return {"ok": True}
        elif path == "/api/recipes/cook":
            for it in body.get("items", []):
                p = d["products"].get(it["product_id"])
                if not p:
                    continue
                if it.get("all"):
                    q = stock_qty(d, p["id"])
                else:
                    q = float(it.get("units") or 1) / units_of(p)
                if q > 0:
                    take_stock(d, p, round(q, 6), "consume", "recette")
                    check_min(d, p)
            save_db(d)
            sync_alexa_soon()
            return {"ok": True}
        elif path in ("/api/alexa/sync", "/api/recipes/generate", "/api/claude/cart", "/api/claude/test"):
            pass
        elif path == "/api/test-notify":
            pass
        else:
            raise LookupError(path)
    # hors verrou (appels réseau)
    if path == "/api/scan":
        if body.get("mode"):  # depuis l'appli : l'action choisie à l'écran s'applique directement
            with LOCK:
                d = load_db()
                d["state"]["mode"], d["state"]["mode_ts"] = body["mode"], time.time()
                save_db(d)
        return scan(body.get("code"), source=body.get("source", "app"), default_mode=body.get("mode") or "consume",
                    amount=body.get("amount"))
    if path == "/api/recipes/generate":
        return generate_recipes(body)
    if path == "/api/alexa/sync":
        return sync_alexa()
    if path == "/api/claude/cart":
        with LOCK:
            d = load_db()
            items, _ = shopping_view(d)
            text = claude_text(d)
        if not items:
            return {"ok": False, "message": "La liste de courses est vide."}
        res = fire_routine(text)
        if res.get("ok"):
            with LOCK:
                d = load_db()
                d["state"]["last_cart"] = {"ts": now().isoformat(timespec="seconds"), "url": res.get("session_url")}
                save_db(d)
        return res
    if path == "/api/claude/test":
        return fire_routine("TEST DE LIAISON depuis Tessfood, ce n'est pas une vraie demande.")
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
                    dm = body.get("mode") or q.get("mode", ["consume"])[0]
                    return self._send(200, scan(code, "scanner", dm))
                if path == "/health":
                    return self._send(200, {"ok": True})
                return self._send(404, {"ok": False})
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(BASE, "web", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and path == "/lib.js":
                with open(os.path.join(BASE, "web", "zxing.min.js"), "rb") as f:
                    return self._send(200, f.read(), "application/javascript; charset=utf-8")
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
