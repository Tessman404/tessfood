"""Démarrage dans Home Assistant : lit les réglages de l'extension puis lance Tessfood."""
import json, os, secrets

try:
    with open("/data/options.json", encoding="utf-8") as f:
        o = json.load(f)
except OSError:
    o = {}

os.environ["TESSFOOD_DATA"] = "/data/tessfood"
os.makedirs("/data/tessfood", exist_ok=True)
token = (o.get("scan_token") or "").strip()
if not token:  # pas de jeton saisi : on en crée un et on le garde
    path = "/data/tessfood/scan_token"
    try:
        token = open(path).read().strip()
    except OSError:
        token = secrets.token_urlsafe(12)
        open(path, "w").write(token)
os.environ["SCAN_TOKEN"] = token
os.environ["ANTHROPIC_API_KEY"] = (o.get("anthropic_api_key") or "").strip()
if os.environ.get("SUPERVISOR_TOKEN"):  # accès interne à Home Assistant, sans jeton à créer
    os.environ["HA_URL"] = "http://supervisor/core"
    os.environ["HA_TOKEN"] = os.environ["SUPERVISOR_TOKEN"]

import server
server.main()
