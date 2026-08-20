from flask import Flask, jsonify, render_template_string, request, Response, send_file
from flask_cors import CORS
import json
import os
import re
import time
import threading

import players_logic as pl

app = Flask(__name__)
CORS(app)

DB_FILE = "auction_db.json"
PLAYERS_FILE = "players_db.json"
AVATAR_DIR = "avatars"
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
os.makedirs(AVATAR_DIR, exist_ok=True)

# Lock unico che protegge sia auction_db.json sia players_db.json.
# Le due funzionalità (puntate e chiamata calciatori) toccano entrambi i file
# nella stessa operazione (es. chiudere un'asta aggiorna sia i crediti sia lo
# stato "chiamato" del calciatore), quindi un solo lock evita sia race
# condition sia deadlock fra due lock separati.
lock = threading.RLock()


# ============================================================
# DB ASTA (squadre, crediti, stato puntate)
# ============================================================

def create_initial_db():
    initial_data = {
        "config": {
            "initial_credits": 500,
            "countdown_seconds": 15,
            "teams": {
                "Squadra A": 500,
                "Squadra B": 500,
                "Squadra C": 500,
                "Squadra D": 500
            },
            "teams_initial": {
                "Squadra A": 500,
                "Squadra B": 500,
                "Squadra C": 500,
                "Squadra D": 500
            },
            "mode": "libera",  # "libera" | "calciatori" - le due modalita' restano separate
            "price_bands": dict(pl.DEFAULT_PRICE_BANDS),
            "requeue_unsold": True
        },
        "auction": {
            "is_active": False,
            "current_bid": 0,
            "highest_bidder": None,
            "total_bids_count": 0,
            "expires_at": 0,
            "current_player_id": None,
            "round_id": 0,
            "bluffs_used": [],
            "participants": []
        },
        "call": {
            "queue": [],
            "filters": {"ruolo": None, "fascia": None, "ordine": "casuale"}
        },
        "history": []
    }
    save_db(initial_data)
    return initial_data


def migrate_db(data):
    """Riempie con valori di default eventuali chiavi mancanti (compatibilita' con DB salvati da versioni precedenti)."""
    data.setdefault("config", {})
    data["config"].setdefault("initial_credits", 500)
    data["config"].setdefault("countdown_seconds", 15)
    data["config"].setdefault("teams", {})
    if "teams_initial" not in data["config"]:
        # migrazione da DB precedenti: ricostruisce i crediti di partenza per
        # squadra usando l'unico valore globale che esisteva prima
        data["config"]["teams_initial"] = {t: data["config"].get("initial_credits", 500) for t in data["config"]["teams"]}
    data["config"].setdefault("mode", "libera")
    data["config"].setdefault("price_bands", dict(pl.DEFAULT_PRICE_BANDS))
    data["config"].setdefault("requeue_unsold", True)

    data.setdefault("auction", {})
    data["auction"].setdefault("is_active", False)
    data["auction"].setdefault("current_bid", 0)
    data["auction"].setdefault("highest_bidder", None)
    data["auction"].setdefault("total_bids_count", 0)
    data["auction"].setdefault("expires_at", 0)
    data["auction"].setdefault("current_player_id", None)
    data["auction"].setdefault("round_id", 0)
    data["auction"].setdefault("bluffs_used", [])
    data["auction"].setdefault("participants", [])

    data.setdefault("call", {"queue": [], "filters": {"ruolo": None, "fascia": None, "ordine": "casuale"}})
    data.setdefault("history", [])
    return data


def load_db():
    if not os.path.exists(DB_FILE):
        return create_initial_db()
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            if "config" not in data or "teams" not in data["config"]:
                return create_initial_db()
            return migrate_db(data)
    except Exception:
        return create_initial_db()


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)


# ============================================================
# DB CALCIATORI
# ============================================================

def create_initial_players():
    data = {"players": {}, "next_id": 1, "imported_at": None, "source_filename": None}
    save_players(data)
    return data


def load_players():
    if not os.path.exists(PLAYERS_FILE):
        return create_initial_players()
    try:
        with open(PLAYERS_FILE, "r") as f:
            data = json.load(f)
            data.setdefault("players", {})
            data.setdefault("next_id", 1)
            data.setdefault("imported_at", None)
            data.setdefault("source_filename", None)
            return data
    except Exception:
        return create_initial_players()


def save_players(data):
    with open(PLAYERS_FILE, "w") as f:
        json.dump(data, f, indent=4)


def regenerate_players_markdown(players_dict, price_bands_config):
    md_text = pl.build_players_markdown(players_dict, price_bands_config)
    with open("players_list.md", "w", encoding="utf-8") as f:
        f.write(md_text)
    return md_text


# ============================================================
# LOGICA ASTA
# ============================================================

def check_auction_expiry(db, players):
    """Chiude automaticamente l'asta se il tempo e' scaduto. Ritorna True se ha chiuso qualcosa."""
    if not (db["auction"]["is_active"] and time.time() >= db["auction"]["expires_at"]):
        return False

    winner = db["auction"]["highest_bidder"]
    final_price = db["auction"]["current_bid"]
    mode = db["config"].get("mode", "libera")
    current_player_id = db["auction"].get("current_player_id")
    participants = list(db["auction"].get("participants", []))

    player_info = None
    if mode == "calciatori" and current_player_id and current_player_id in players["players"]:
        player_info = players["players"][current_player_id]

    if winner and winner in db["config"]["teams"]:
        db["config"]["teams"][winner] -= final_price
        entry = {"winner": winner, "price": final_price, "status": "Assegnato", "participants": participants}
        if player_info:
            entry["ruolo"] = player_info["ruolo"]
            entry["player_name"] = player_info["nome"]
            player_info["assegnato_a"] = winner
            player_info["prezzo"] = final_price
        db["history"].insert(0, entry)
    else:
        entry = {"winner": "Nessuno (Invenduto)", "price": 0, "status": "Invenduto", "participants": participants}
        if player_info:
            entry["ruolo"] = player_info["ruolo"]
            entry["player_name"] = player_info["nome"]
            if db["config"].get("requeue_unsold", True):
                player_info["chiamato"] = False  # rimesso disponibile per un secondo giro
        db["history"].insert(0, entry)

    db["auction"]["is_active"] = False
    db["auction"]["highest_bidder"] = None
    db["auction"]["current_bid"] = 0
    db["auction"]["total_bids_count"] = 0
    db["auction"]["current_player_id"] = None
    db["auction"]["bluffs_used"] = []
    db["auction"]["participants"] = []
    return True


def skip_pending_uncalled_player(db, players):
    """Se il banditore aveva chiamato un calciatore su cui non e' mai arrivata
    nessuna offerta (asta mai partita: is_active False ma current_player_id
    ancora valorizzato) e ora vuole chiamarne un altro, chiude quello in sospeso
    come invenduto prima di procedere. Senza questo, il calciatore resterebbe
    'chiamato' per sempre senza mai comparire nello storico ne' essere
    richiamabile in futuro."""
    pid = db["auction"].get("current_player_id")
    if db["auction"]["is_active"] or not pid or pid not in players["players"]:
        return False
    p = players["players"][pid]
    db["history"].insert(0, {
        "winner": "Nessuno (Invenduto)", "price": 0, "status": "Invenduto",
        "ruolo": p["ruolo"], "player_name": p["nome"], "participants": []
    })
    if db["config"].get("requeue_unsold", True):
        p["chiamato"] = False
    db["auction"]["current_player_id"] = None
    return True


# --- ROUTE: PANNELLO BANDITORE ---
@app.route("/", methods=["GET"])
def banditore_panel():
    return render_template_string(BANDITORE_HTML)


# --- ROUTE: HUB PARTECIPANTI ---
@app.route("/hub", methods=["GET"])
def participant_hub():
    return render_template_string(HUB_HTML)


# --- API: STATO CONDIVISO ---
@app.route("/state", methods=["GET"])
def get_state():
    my_team = request.args.get("team")

    with lock:
        db = load_db()
        players = load_players()
        closed = check_auction_expiry(db, players)
        if closed:
            save_db(db)
            save_players(players)

    now = time.time()
    time_left = 0
    if db["auction"]["is_active"]:
        time_left = max(0, int(db["auction"]["expires_at"] - now))

    mode = db["config"].get("mode", "libera")

    is_leading = None
    can_bluff = None
    # has_real_bid distingue un'asta con almeno un'offerta vera (highest_bidder
    # valorizzato) da un'asta avviata "a vuoto" tramite bluff (nessuno ha
    # davvero puntato): in quel caso non ha senso mostrare "stai vincendo/perdendo".
    has_real_bid = bool(db["auction"]["is_active"] and db["auction"]["highest_bidder"] is not None)

    if my_team:
        is_leading = bool(has_real_bid and db["auction"]["highest_bidder"] == my_team)
        if not db["auction"]["is_active"]:
            # Puo' avviare un'asta "finta" col bluff solo se in modalita' calciatori
            # c'e' gia' un calciatore in chiamata (stessa regola di una puntata vera).
            can_bluff = (mode != "calciatori") or bool(db["auction"].get("current_player_id"))
        else:
            can_bluff = bool(
                db["auction"]["highest_bidder"] != my_team
                and my_team not in db["auction"].get("bluffs_used", [])
            )

    current_player = None
    if mode == "calciatori":
        pid = db["auction"].get("current_player_id")
        if pid and pid in players["players"]:
            p = players["players"][pid]
            current_player = {
                "ruolo": p["ruolo"],
                "nome": p["nome"],
                "squadra_reale": p["squadra_reale"],
                "quotazione": p["quotazione"]
            }

    public_state = {
        "mode": mode,
        "auction": {
            "is_active": db["auction"]["is_active"],
            "current_bid": db["auction"]["current_bid"],
            "total_bids_count": db["auction"]["total_bids_count"],
            "time_left": time_left,
            "round_id": db["auction"].get("round_id", 0),
            "is_leading": is_leading,
            "can_bluff": can_bluff,
            "has_real_bid": has_real_bid,
            "current_player": current_player
        },
        "teams": db["config"]["teams"],
        "history": db["history"][:20],
        "config": {
            "mode": mode,
            "initial_credits": db["config"]["initial_credits"],
            "countdown_seconds": db["config"]["countdown_seconds"],
            "price_bands": db["config"].get("price_bands", pl.DEFAULT_PRICE_BANDS),
            "requeue_unsold": db["config"].get("requeue_unsold", True),
            "teams_initial": db["config"].get("teams_initial", {})
        }
    }
    return jsonify(public_state)


# --- API: EFFETTUA PUNTATA (AVVIA L'ASTA SE INATTIVA) ---
@app.route("/bid", methods=["POST"])
def place_bid():
    req = request.get_json(silent=True) or {}
    team = req.get("team")
    amount = req.get("amount")

    if not team or amount is None:
        return jsonify({"success": False, "error": "Parametri mancanti"}), 400

    try:
        amount = float(amount)
        if amount != int(amount):
            return jsonify({"success": False, "error": "L'offerta deve essere un numero intero"}), 400
        amount = int(amount)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Offerta non valida"}), 400

    with lock:
        db = load_db()
        players = load_players()
        check_auction_expiry(db, players)

        if team not in db["config"]["teams"]:
            return jsonify({"success": False, "error": "Squadra non riconosciuta"}), 400

        team_budget = db["config"]["teams"][team]
        if amount > team_budget:
            return jsonify({"success": False, "error": "Crediti insufficienti"}), 400

        # Una squadra gia' in testa non puo' rilanciare su se stessa: deve aspettare
        # che qualcun altro faccia un'offerta piu' alta prima di poter puntare di nuovo.
        if db["auction"]["is_active"] and db["auction"]["highest_bidder"] == team:
            return jsonify({"success": False, "error": "Sei gia' tu il miglior offerente: attendi un rilancio altrui prima di puntare di nuovo"}), 400

        mode = db["config"].get("mode", "libera")
        now = time.time()

        if not db["auction"]["is_active"]:
            if mode == "calciatori" and not db["auction"].get("current_player_id"):
                return jsonify({"success": False, "error": "Nessun calciatore in chiamata: il banditore deve chiamarne uno prima di aprire le offerte"}), 400
            if amount < 1:
                return jsonify({"success": False, "error": "Offerta minima non valida"}), 400
            db["auction"]["is_active"] = True
            db["auction"]["current_bid"] = amount
            db["auction"]["highest_bidder"] = team
            db["auction"]["total_bids_count"] = 1
            db["auction"]["expires_at"] = now + db["config"]["countdown_seconds"]
            db["auction"]["round_id"] = db["auction"].get("round_id", 0) + 1
            db["auction"]["bluffs_used"] = []
            db["auction"]["participants"] = [team]
            save_db(db)
            return jsonify({"success": True})

        if amount <= db["auction"]["current_bid"]:
            return jsonify({"success": False, "error": "Offerta troppo bassa"}), 400

        db["auction"]["current_bid"] = amount
        db["auction"]["highest_bidder"] = team
        db["auction"]["total_bids_count"] += 1
        db["auction"]["expires_at"] = now + db["config"]["countdown_seconds"]
        participants = db["auction"].setdefault("participants", [])
        if team not in participants:
            participants.append(team)
        save_db(db)

        return jsonify({"success": True})


# --- API: BLUFF ---
# Ha due comportamenti distinti a seconda dello stato dell'asta:
#  1) Asta NON attiva -> avvia un'asta "finta" (nessuna offerta reale, current_bid
#     resta 0, nessun credito impegnato). Se nessuno fa mai un'offerta vera prima
#     della scadenza, il calciatore NON viene assegnato (resta invenduto).
#  2) Asta gia' attiva -> allunga il countdown di 2 secondi (non lo resetta al
#     massimo). In questo caso il bluff si disattiva per quella squadra fino
#     alla prossima asta (un bluff "di estensione" a partita).
@app.route("/bid/bluff", methods=["POST"])
def bluff_bid():
    req = request.get_json(silent=True) or {}
    team = req.get("team")
    if not team:
        return jsonify({"success": False, "error": "Squadra mancante"}), 400

    with lock:
        db = load_db()
        players = load_players()
        check_auction_expiry(db, players)

        if team not in db["config"]["teams"]:
            return jsonify({"success": False, "error": "Squadra non riconosciuta"}), 400

        mode = db["config"].get("mode", "libera")

        if not db["auction"]["is_active"]:
            if mode == "calciatori" and not db["auction"].get("current_player_id"):
                return jsonify({"success": False, "error": "Nessun calciatore in chiamata: il banditore deve chiamarne uno prima di bluffare"}), 400

            now = time.time()
            db["auction"]["is_active"] = True
            db["auction"]["current_bid"] = 0
            db["auction"]["highest_bidder"] = None
            db["auction"]["total_bids_count"] = 1
            db["auction"]["expires_at"] = now + db["config"]["countdown_seconds"]
            db["auction"]["round_id"] = db["auction"].get("round_id", 0) + 1
            db["auction"]["bluffs_used"] = []
            db["auction"]["participants"] = [team]
            save_db(db)
            return jsonify({"success": True, "azione": "avvio"})

        if db["auction"]["highest_bidder"] == team:
            return jsonify({"success": False, "error": "Sei gia' in testa: non puoi bluffare"}), 400

        bluffs_used = db["auction"].setdefault("bluffs_used", [])
        if team in bluffs_used:
            return jsonify({"success": False, "error": "Hai gia' usato il tuo bluff su questa asta"}), 400

        # Il bluff aggiunge 2 secondi al tempo RIMANENTE (non lo riporta al massimo)
        # ed entra nel conteggio puntate come una offerta vera, cosi' resta
        # indistinguibile dall'esterno - ma non tocca l'offerta corrente ne' i crediti.
        bluffs_used.append(team)
        db["auction"]["expires_at"] += 2
        db["auction"]["total_bids_count"] += 1
        participants = db["auction"].setdefault("participants", [])
        if team not in participants:
            participants.append(team)
        save_db(db)
        return jsonify({"success": True, "azione": "estensione"})


# --- API: AVATAR SQUADRA ---
@app.route("/avatar/upload", methods=["POST"])
def upload_avatar():
    team = request.form.get("team")
    file = request.files.get("file")
    if not team or not file or file.filename == "":
        return jsonify({"success": False, "error": "Parametri mancanti"}), 400

    with lock:
        db = load_db()
        if team not in db["config"]["teams"]:
            return jsonify({"success": False, "error": "Squadra non riconosciuta"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in AVATAR_EXTS:
        return jsonify({"success": False, "error": "Formato immagine non supportato (usa png, jpg, webp o gif)"}), 400

    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", team.strip())
    if not slug:
        return jsonify({"success": False, "error": "Nome squadra non valido"}), 400

    for old_ext in AVATAR_EXTS:
        old_path = os.path.join(AVATAR_DIR, slug + old_ext)
        if os.path.exists(old_path):
            os.remove(old_path)

    file.save(os.path.join(AVATAR_DIR, slug + ext))
    return jsonify({"success": True})


@app.route("/avatar/<team>", methods=["GET"])
def get_avatar(team):
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", team.strip())
    for ext in AVATAR_EXTS:
        path = os.path.join(AVATAR_DIR, slug + ext)
        if os.path.exists(path):
            return send_file(path)
    return jsonify({"error": "Nessun avatar caricato per questa squadra"}), 404


# --- API: CONFIGURAZIONE SQUADRE RIGA PER RIGA (nome + crediti + avatar) ---
@app.route("/config/teams", methods=["POST"])
def update_teams_config():
    teams_json = request.form.get("teams_json")
    if not teams_json:
        return jsonify({"success": False, "error": "Dati squadre mancanti"}), 400
    try:
        teams_list = json.loads(teams_json)
    except Exception:
        return jsonify({"success": False, "error": "Formato dati non valido"}), 400
    if not isinstance(teams_list, list) or not teams_list:
        return jsonify({"success": False, "error": "Inserisci almeno una squadra"}), 400

    cleaned = []
    seen_names = set()
    for idx, t in enumerate(teams_list):
        if not isinstance(t, dict):
            return jsonify({"success": False, "error": f"Riga {idx + 1} non valida"}), 400
        name = str(t.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "error": f"Nome mancante alla riga {idx + 1}"}), 400
        if name in seen_names:
            return jsonify({"success": False, "error": f"Nome squadra duplicato: {name}"}), 400
        seen_names.add(name)
        try:
            credits = int(t.get("credits"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": f"Crediti non validi per '{name}'"}), 400
        if credits <= 0:
            return jsonify({"success": False, "error": f"I crediti per '{name}' devono essere maggiori di 0"}), 400
        cleaned.append({"name": name, "credits": credits, "row_index": idx})

    with lock:
        db = load_db()
        if db["auction"]["is_active"]:
            return jsonify({"success": False, "error": "Termina o resetta l'asta in corso prima di modificare le squadre"}), 400

        new_teams = {t["name"]: t["credits"] for t in cleaned}
        new_teams_initial = {t["name"]: t["credits"] for t in cleaned}
        db["config"]["teams"] = new_teams
        db["config"]["teams_initial"] = new_teams_initial
        save_db(db)

    # Avatar caricati dal banditore per ciascuna riga (opzionali, campo avatar_<indice>)
    avatar_caricati = []
    for t in cleaned:
        file = request.files.get(f"avatar_{t['row_index']}")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in AVATAR_EXTS:
                slug = re.sub(r"[^a-zA-Z0-9_-]", "_", t["name"].strip())
                for old_ext in AVATAR_EXTS:
                    old_path = os.path.join(AVATAR_DIR, slug + old_ext)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                file.save(os.path.join(AVATAR_DIR, slug + ext))
                avatar_caricati.append(t["name"])

    return jsonify({"success": True, "totale_squadre": len(cleaned), "avatar_caricati": avatar_caricati})


# --- API: AGGIORNA CONFIGURAZIONE ---
@app.route("/config", methods=["POST"])
def update_config():
    req = request.get_json(silent=True) or {}

    with lock:
        db = load_db()

        if "initial_credits" in req:
            try:
                init_credits = int(req["initial_credits"])
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Crediti di partenza non validi"}), 400
            if init_credits <= 0:
                return jsonify({"success": False, "error": "I crediti di partenza devono essere maggiori di 0"}), 400
            db["config"]["initial_credits"] = init_credits

        if "countdown_seconds" in req:
            try:
                countdown = int(req["countdown_seconds"])
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Countdown non valido"}), 400
            if countdown <= 0:
                return jsonify({"success": False, "error": "Il countdown deve essere maggiore di 0"}), 400
            db["config"]["countdown_seconds"] = countdown

        if "teams_list" in req:
            if not isinstance(req["teams_list"], list):
                return jsonify({"success": False, "error": "Elenco squadre non valido"}), 400
            new_teams = {}
            new_teams_initial = {}
            init_cred = db["config"]["initial_credits"]
            for t in req["teams_list"]:
                t_clean = str(t).strip()
                if t_clean:
                    new_teams[t_clean] = init_cred
                    new_teams_initial[t_clean] = init_cred
            if not new_teams:
                return jsonify({"success": False, "error": "Inserisci almeno una squadra"}), 400
            db["config"]["teams"] = new_teams
            db["config"]["teams_initial"] = new_teams_initial

        if "mode" in req:
            if req["mode"] not in ("libera", "calciatori"):
                return jsonify({"success": False, "error": "Modalita' non valida"}), 400
            if db["auction"]["is_active"]:
                return jsonify({"success": False, "error": "Termina o resetta l'asta in corso prima di cambiare modalita'"}), 400
            db["config"]["mode"] = req["mode"]

        if "requeue_unsold" in req:
            db["config"]["requeue_unsold"] = bool(req["requeue_unsold"])

        if "price_bands" in req:
            if not isinstance(req["price_bands"], dict):
                return jsonify({"success": False, "error": "Formato fasce di prezzo non valido"}), 400
            new_price_bands = dict(db["config"].get("price_bands", pl.DEFAULT_PRICE_BANDS))
            for ruolo in pl.VALID_ROLES:
                if ruolo not in req["price_bands"]:
                    continue
                entry = req["price_bands"][ruolo]
                if not isinstance(entry, dict) or entry.get("type") not in ("auto", "manual"):
                    return jsonify({"success": False, "error": f"Configurazione fasce non valida per ruolo {ruolo}"}), 400
                if entry["type"] == "manual":
                    th_raw = entry.get("thresholds", {})
                    if not isinstance(th_raw, dict):
                        return jsonify({"success": False, "error": f"Soglie non valide per ruolo {ruolo}"}), 400
                    th_clean = {}
                    for label in pl.FASCE_LABELS:
                        try:
                            th_clean[label] = int(th_raw.get(label, 0))
                        except (TypeError, ValueError):
                            return jsonify({"success": False, "error": f"Soglia '{label}' non valida per ruolo {ruolo}"}), 400
                    new_price_bands[ruolo] = {"type": "manual", "thresholds": th_clean}
                else:
                    new_price_bands[ruolo] = {"type": "auto"}
            db["config"]["price_bands"] = new_price_bands

        save_db(db)
        return jsonify({"success": True})


# --- API: RESET GENERALE ---
@app.route("/reset", methods=["POST"])
def reset_all():
    with lock:
        db = load_db()
        players = load_players()

        # Ogni squadra torna ai PROPRI crediti di partenza (possono differire da
        # squadra a squadra, impostati dal banditore nella configurazione).
        teams_initial = db["config"].get("teams_initial", {})
        for team in db["config"]["teams"]:
            db["config"]["teams"][team] = teams_initial.get(team, db["config"]["initial_credits"])

        db["auction"] = {
            "is_active": False,
            "current_bid": 0,
            "highest_bidder": None,
            "total_bids_count": 0,
            "expires_at": 0,
            "current_player_id": None,
            "round_id": 0,
            "bluffs_used": [],
            "participants": []
        }
        db["call"] = {"queue": [], "filters": {"ruolo": None, "fascia": None, "ordine": "casuale"}}
        db["history"] = []

        for p in players["players"].values():
            p["chiamato"] = False
            p["assegnato_a"] = None
            p["prezzo"] = None

        save_db(db)
        save_players(players)
        return jsonify({"success": True})


# ============================================================
# API CALCIATORI (import, markdown, fasce, coda di chiamata)
# ============================================================

@app.route("/players/upload", methods=["POST"])
def upload_players():
    with lock:
        db = load_db()
        if db["config"].get("mode") != "calciatori":
            return jsonify({"success": False, "error": "Passa alla modalita' 'Chiamata Calciatori' prima di caricare la lista"}), 400
        price_bands = db["config"].get("price_bands", pl.DEFAULT_PRICE_BANDS)

    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"success": False, "error": "Nessun file selezionato"}), 400
    if not file.filename.lower().endswith(".xlsx"):
        return jsonify({"success": False, "error": "Il file deve essere in formato .xlsx"}), 400

    try:
        players_dict, next_id, stats = pl.parse_players_xlsx(file)
    except Exception as e:
        return jsonify({"success": False, "error": f"Errore nella lettura del file: {e}"}), 400

    if not players_dict:
        return jsonify({"success": False, "error": "Nessun calciatore valido trovato nel file"}), 400

    with lock:
        players = {
            "players": players_dict,
            "next_id": next_id,
            "imported_at": time.time(),
            "source_filename": file.filename
        }
        save_players(players)
        regenerate_players_markdown(players_dict, price_bands)

    return jsonify({"success": True, "stats": stats, "totale": len(players_dict)})


@app.route("/players/markdown", methods=["GET"])
def get_players_markdown():
    with lock:
        players = load_players()
        db = load_db()
    price_bands = db["config"].get("price_bands", pl.DEFAULT_PRICE_BANDS)
    md_text = pl.build_players_markdown(players["players"], price_bands)
    return Response(
        md_text,
        mimetype="text/markdown",
        headers={"Content-Disposition": "attachment; filename=players_list.md"}
    )


@app.route("/players/reload_markdown", methods=["POST"])
def reload_players_markdown():
    file = request.files.get("file")
    if file:
        text = file.read().decode("utf-8", errors="replace")
    else:
        req = request.get_json(silent=True) or {}
        text = req.get("text", "")

    if not text.strip():
        return jsonify({"success": False, "error": "Nessun contenuto da importare"}), 400

    with lock:
        players = load_players()
        new_players_dict, next_id = pl.parse_players_markdown(text, existing=players["players"])
        if not new_players_dict:
            return jsonify({"success": False, "error": "Nessun calciatore riconosciuto nel markdown (verifica il formato)"}), 400
        players["players"] = new_players_dict
        players["next_id"] = next_id
        save_players(players)

    return jsonify({"success": True, "totale": len(new_players_dict)})


@app.route("/players/search", methods=["GET"])
def search_players():
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify({"success": False, "error": "Digita almeno 2 caratteri"}), 400

    with lock:
        players = load_players()

    matches = []
    for pid, p in players["players"].items():
        if not p["chiamato"] and q in p["nome"].lower():
            matches.append({"id": pid, "nome": p["nome"], "ruolo": p["ruolo"], "squadra_reale": p["squadra_reale"], "quotazione": p["quotazione"]})
    matches.sort(key=lambda m: m["nome"])
    return jsonify({"success": True, "matches": matches[:15]})


@app.route("/call/filters", methods=["POST"])
def set_call_filters():
    req = request.get_json(silent=True) or {}
    ruolo = req.get("ruolo") or None
    fascia = req.get("fascia") or None
    ordine = req.get("ordine", "casuale")

    if ruolo and ruolo not in pl.VALID_ROLES:
        return jsonify({"success": False, "error": "Ruolo non valido"}), 400
    if fascia and fascia not in pl.FASCE_LABELS:
        return jsonify({"success": False, "error": "Fascia non valida"}), 400
    if ordine not in ("casuale", "valutazione", "nome"):
        return jsonify({"success": False, "error": "Ordine non valido"}), 400

    with lock:
        db = load_db()
        players = load_players()
        if db["config"].get("mode") != "calciatori":
            return jsonify({"success": False, "error": "Funzione disponibile solo in modalita' Chiamata Calciatori"}), 400

        price_bands = db["config"].get("price_bands", pl.DEFAULT_PRICE_BANDS)
        queue = pl.build_call_queue(players["players"], ruolo, fascia, ordine, price_bands)
        db["call"] = {"queue": queue, "filters": {"ruolo": ruolo, "fascia": fascia, "ordine": ordine}}
        save_db(db)

    return jsonify({"success": True, "queue_length": len(queue)})


@app.route("/call/next", methods=["POST"])
def call_next():
    with lock:
        db = load_db()
        players = load_players()

        if db["config"].get("mode") != "calciatori":
            return jsonify({"success": False, "error": "Funzione disponibile solo in modalita' Chiamata Calciatori"}), 400
        if db["auction"]["is_active"]:
            return jsonify({"success": False, "error": "C'e' gia' un'asta in corso: attendi la chiusura"}), 400

        skip_pending_uncalled_player(db, players)

        pdict = players["players"]
        queue = db.get("call", {}).get("queue", [])
        while queue and (queue[0] not in pdict or pdict[queue[0]]["chiamato"]):
            queue.pop(0)

        if not queue:
            db.setdefault("call", {"filters": {"ruolo": None, "fascia": None, "ordine": "casuale"}})["queue"] = queue
            save_db(db)
            return jsonify({"success": False, "error": "Coda vuota: imposta nuovi filtri o cerca un calciatore manualmente"}), 400

        pid = queue.pop(0)
        pdict[pid]["chiamato"] = True
        db["call"]["queue"] = queue
        db["auction"]["current_player_id"] = pid
        save_db(db)
        save_players(players)

        p = pdict[pid]
        return jsonify({"success": True, "player": {"id": pid, "nome": p["nome"], "ruolo": p["ruolo"], "squadra_reale": p["squadra_reale"], "quotazione": p["quotazione"]}})


@app.route("/call/by_id", methods=["POST"])
def call_by_id():
    req = request.get_json(silent=True) or {}
    pid = req.get("player_id")
    if not pid:
        return jsonify({"success": False, "error": "player_id mancante"}), 400

    with lock:
        db = load_db()
        players = load_players()

        if db["config"].get("mode") != "calciatori":
            return jsonify({"success": False, "error": "Funzione disponibile solo in modalita' Chiamata Calciatori"}), 400
        if db["auction"]["is_active"]:
            return jsonify({"success": False, "error": "C'e' gia' un'asta in corso: attendi la chiusura"}), 400

        skip_pending_uncalled_player(db, players)

        pdict = players["players"]
        if pid not in pdict:
            return jsonify({"success": False, "error": "Calciatore non trovato"}), 400
        if pdict[pid]["chiamato"]:
            return jsonify({"success": False, "error": "Calciatore gia' chiamato in precedenza"}), 400

        pdict[pid]["chiamato"] = True
        db["auction"]["current_player_id"] = pid
        save_players(players)
        save_db(db)

        p = pdict[pid]
        return jsonify({"success": True, "player": {"id": pid, "nome": p["nome"], "ruolo": p["ruolo"], "squadra_reale": p["squadra_reale"], "quotazione": p["quotazione"]}})


@app.route("/call/queue_status", methods=["GET"])
def call_queue_status():
    with lock:
        db = load_db()
        players = load_players()

    queue = db.get("call", {}).get("queue", [])
    pdict = players["players"]
    remaining = [pid for pid in queue if pid in pdict and not pdict[pid]["chiamato"]]
    totale_disponibili = sum(1 for p in pdict.values() if not p["chiamato"])

    return jsonify({
        "success": True,
        "in_coda": len(remaining),
        "totale_disponibili": totale_disponibili,
        "totale_calciatori": len(pdict),
        "filters": db.get("call", {}).get("filters", {})
    })


# --- TEMPLATE HTML: PANNELLO BANDITORE ---
BANDITORE_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Banditore - Asta Fantacalcio</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: auto; }
        .card { background: #1e1e1e; padding: 20px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
        h1, h2 { color: #4caf50; margin-top: 0; }
        h3 { color: #ff9800; margin: 15px 0 8px 0; }
        input, select, button { width: 100%; padding: 10px; margin: 8px 0; border-radius: 6px; border: 1px solid #444; background: #2c2c2c; color: white; box-sizing: border-box; font-size: 16px; }
        button { background: #4caf50; font-weight: bold; cursor: pointer; border: none; }
        button:hover { background: #45a049; }
        button.danger { background: #d32f2f; }
        button.danger:hover { background: #c62828; }
        button.secondary { background: #555; }
        button.secondary:hover { background: #444; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #333; padding: 10px; text-align: left; }
        th { background: #2a2a2a; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
        .mode-toggle { display: flex; gap: 10px; margin-bottom: 10px; }
        .mode-toggle button { flex: 1; background: #2c2c2c; }
        .mode-toggle button.active { background: #4caf50; }
        .hidden { display: none !important; }
        .small-label { font-size: 12px; color: #aaa; margin: 2px 0; }
        .player-badge { background: #2a2a2a; border-radius: 8px; padding: 12px; margin: 10px 0; text-align: center; }
        .player-badge .nome { font-size: 22px; font-weight: bold; color: #ffeb3b; }
        .msg { font-size: 14px; margin-top: 6px; color: #4caf50; }
        .search-results { max-height: 200px; overflow-y: auto; }
        .search-result-item { background: #2c2c2c; padding: 8px; border-radius: 6px; margin: 4px 0; cursor: pointer; }
        .search-result-item:hover { background: #3a3a3a; }
        .team-row { display: flex; align-items: center; gap: 10px; margin: 10px 0; background: #262626; border-radius: 10px; padding: 8px; }
        .team-row-avatar { position: relative; flex-shrink: 0; }
        .team-row-avatar img { width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 2px solid #444; background: #333; display: block; }
        .team-row-avatar input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; margin: 0; padding: 0; width: 48px; height: 48px; }
        .team-row input[type=text] { flex: 2; margin: 0; }
        .team-row input[type=number] { flex: 1; margin: 0; }
        .team-row button.remove-row { width: auto; flex-shrink: 0; padding: 8px 12px; margin: 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Pannello Banditore (Host)</h1>

        <div class="card">
            <h2>Modalita' Sessione</h2>
            <div class="mode-toggle">
                <button id="modeBtnLibera" onclick="setMode('libera')">Puntate Libere</button>
                <button id="modeBtnCalciatori" onclick="setMode('calciatori')">Chiamata Calciatori</button>
            </div>
            <p class="small-label" id="modeHint"></p>
        </div>

        <div class="card">
            <h2>Countdown Asta</h2>
            <label>Secondi per ogni offerta:</label>
            <input type="number" id="countdownSec" value="15">
            <button onclick="saveCountdown()">Salva Countdown</button>
        </div>

        <div class="card">
            <h2>Squadre Partecipanti</h2>
            <p class="small-label">Una riga per squadra: avatar (facoltativo, se non caricato lo sceglie il giocatore), nome e crediti di partenza personalizzabili.</p>
            <div id="teamRowsContainer"></div>
            <div class="grid">
                <button class="secondary" type="button" onclick="addTeamRow()">+ Aggiungi Squadra</button>
                <button type="button" onclick="saveTeamsConfig()">Salva Configurazione Squadre</button>
            </div>
            <p class="msg" id="teamsConfigMsg"></p>
        </div>

        <div class="card" id="playersCard">
            <h2>Lista Calciatori</h2>
            <label>Carica listone (.xlsx):</label>
            <input type="file" id="xlsxFile" accept=".xlsx">
            <button onclick="uploadPlayers()">Importa Lista</button>
            <p class="msg" id="uploadMsg"></p>

            <div class="grid">
                <button class="secondary" onclick="downloadMarkdown()">Scarica lista (Markdown)</button>
                <div>
                    <input type="file" id="mdFile" accept=".md,.markdown,.txt">
                    <button class="secondary" onclick="reloadMarkdown()">Ricarica da Markdown</button>
                </div>
            </div>
            <p class="msg" id="reloadMsg"></p>
        </div>

        <div class="card" id="priceBandsCard">
            <h2>Fasce di Prezzo per Ruolo</h2>
            <p class="small-label">Automatica = calcolata sui percentili della quotazione per quel ruolo. Manuale = soglie minime fisse per le 4 fasce (Top, Semi-top, Buoni, Low-cost).</p>
            <div id="priceBandsForm"></div>
            <button onclick="savePriceBands()">Salva Fasce di Prezzo</button>
        </div>

        <div class="card" id="callEngineCard">
            <h2>Chiamata Calciatori</h2>
            <div class="grid">
                <div>
                    <label>Ruolo:</label>
                    <select id="callRuolo">
                        <option value="">Tutti</option>
                        <option value="P">Portieri</option>
                        <option value="D">Difensori</option>
                        <option value="C">Centrocampisti</option>
                        <option value="A">Attaccanti</option>
                    </select>
                </div>
                <div>
                    <label>Fascia:</label>
                    <select id="callFascia">
                        <option value="">Tutte</option>
                        <option value="Top">Top</option>
                        <option value="Semi-top">Semi-top</option>
                        <option value="Buoni">Buoni</option>
                        <option value="Low-cost">Low-cost</option>
                    </select>
                </div>
            </div>
            <label>Ordine di chiamata:</label>
            <select id="callOrdine">
                <option value="casuale">Casuale</option>
                <option value="valutazione">Per valutazione</option>
                <option value="nome">Per nome</option>
            </select>
            <label style="display:flex; align-items:center; gap:8px; font-size:14px;">
                <input type="checkbox" id="requeueUnsold" style="width:auto;" checked>
                Rimetti in coda i calciatori invenduti
            </label>
            <button onclick="prepareQueue()">Prepara Coda</button>
            <p class="small-label" id="queueStatus">Coda non ancora preparata</p>

            <button onclick="callNext()">Chiama il Prossimo</button>

            <label>Chiamata manuale (cerca per nome):</label>
            <input type="text" id="manualSearch" placeholder="Es. Lautaro" oninput="searchManual()">
            <div class="search-results" id="searchResults"></div>

            <div class="player-badge hidden" id="currentPlayerBadge">
                <div class="small-label" id="currentPlayerRuolo">-</div>
                <div class="nome" id="currentPlayerNome">-</div>
                <div class="small-label" id="currentPlayerInfo">-</div>
            </div>
        </div>

        <div class="card">
            <h2>Stato Live Asta</h2>
            <p>Offerta Attuale: <b id="currentBid" style="font-size: 24px; color: #ffeb3b;">0</b> crediti</p>
            <p>Puntate anonime ricevute: <b id="totalBids">0</b></p>
            <p>Tempo Rimasto: <b id="timeLeft" style="font-size: 24px; color: #ff9800;">--</b> secondi</p>
        </div>

        <div class="card">
            <h2>Storico Aste Effettuate</h2>
            <table>
                <thead id="historyHead">
                    <tr>
                        <th>Squadra Vincente</th>
                        <th>Prezzo d'Acquisto</th>
                    </tr>
                </thead>
                <tbody id="historyTable">
                    <tr><td colspan="2">Nessuna asta completata</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Azioni</h2>
            <button class="danger" onclick="resetAll()">Reset Totale / Nuova Sessione</button>
        </div>
    </div>

<script>
    const serverUrl = window.location.origin;
    let currentMode = "libera";
    const ROLE_NAMES = {P: "Portiere", D: "Difensore", C: "Centrocampista", A: "Attaccante"};
    const FASCE = ["Top", "Semi-top", "Buoni", "Low-cost"];

    function setMode(mode) {
        fetch(`${serverUrl}/config`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: mode})
        }).then(r => r.json()).then(data => {
            if (!data.success) { alert("Errore: " + data.error); return; }
            fetchState();
        });
    }

    function applyModeUI(mode) {
        currentMode = mode;
        document.getElementById("modeBtnLibera").classList.toggle("active", mode === "libera");
        document.getElementById("modeBtnCalciatori").classList.toggle("active", mode === "calciatori");
        document.getElementById("playersCard").classList.toggle("hidden", mode !== "calciatori");
        document.getElementById("priceBandsCard").classList.toggle("hidden", mode !== "calciatori");
        document.getElementById("callEngineCard").classList.toggle("hidden", mode !== "calciatori");
        document.getElementById("modeHint").innerText = mode === "calciatori"
            ? "Modalita' Chiamata Calciatori: le puntate sono sempre agganciate a un calciatore chiamato dal banditore."
            : "Modalita' Puntate Libere: aste anonime senza lista calciatori (come prima).";

        const historyHead = document.getElementById("historyHead");
        if (mode === "calciatori") {
            historyHead.innerHTML = "<tr><th>Ruolo</th><th>Calciatore</th><th>Squadra Vincente</th><th>Prezzo</th></tr>";
        } else {
            historyHead.innerHTML = "<tr><th>Squadra Vincente</th><th>Prezzo d'Acquisto</th></tr>";
        }
    }

    function renderPriceBandsForm(priceBands) {
        const container = document.getElementById("priceBandsForm");
        let html = "";
        for (const ruolo of ["P", "D", "C", "A"]) {
            const cfg = priceBands[ruolo] || {type: "auto"};
            const isManual = cfg.type === "manual";
            const th = cfg.thresholds || {};
            html += `<h3>${ROLE_NAMES[ruolo]}</h3>`;
            html += `<div class="mode-toggle">
                <button type="button" class="${!isManual ? 'active' : ''}" onclick="setBandType('${ruolo}','auto')">Automatica</button>
                <button type="button" class="${isManual ? 'active' : ''}" onclick="setBandType('${ruolo}','manual')">Manuale</button>
            </div>`;
            html += `<div class="grid4" id="bandThresholds_${ruolo}" style="${isManual ? '' : 'display:none;'}">`;
            for (const label of FASCE) {
                html += `<div><label class="small-label">${label}</label><input type="number" id="band_${ruolo}_${label}" value="${th[label] !== undefined ? th[label] : 0}"></div>`;
            }
            html += `</div>`;
        }
        container.innerHTML = html;
        for (const ruolo of ["P", "D", "C", "A"]) {
            container.dataset[`type_${ruolo}`] = (priceBands[ruolo] || {}).type || "auto";
        }
    }

    function setBandType(ruolo, type) {
        const container = document.getElementById("priceBandsForm");
        container.dataset[`type_${ruolo}`] = type;
        document.getElementById(`bandThresholds_${ruolo}`).style.display = type === "manual" ? "" : "none";
        renderModeButtonsHighlight();
    }

    function renderModeButtonsHighlight() {
        const container = document.getElementById("priceBandsForm");
        for (const ruolo of ["P", "D", "C", "A"]) {
            const type = container.dataset[`type_${ruolo}`];
            const box = document.getElementById(`bandThresholds_${ruolo}`);
            if (!box) continue;
            const wrapper = box.previousElementSibling;
            if (wrapper) {
                wrapper.children[0].classList.toggle("active", type === "auto");
                wrapper.children[1].classList.toggle("active", type === "manual");
            }
        }
    }

    async function savePriceBands() {
        const container = document.getElementById("priceBandsForm");
        const priceBands = {};
        for (const ruolo of ["P", "D", "C", "A"]) {
            const type = container.dataset[`type_${ruolo}`] || "auto";
            if (type === "manual") {
                const thresholds = {};
                for (const label of FASCE) {
                    thresholds[label] = parseInt(document.getElementById(`band_${ruolo}_${label}`).value) || 0;
                }
                priceBands[ruolo] = {type: "manual", thresholds: thresholds};
            } else {
                priceBands[ruolo] = {type: "auto"};
            }
        }
        const res = await fetch(`${serverUrl}/config`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({price_bands: priceBands})
        });
        const data = await res.json();
        if (data.success) alert("Fasce di prezzo salvate!"); else alert("Errore: " + data.error);
    }

    async function uploadPlayers() {
        const fileInput = document.getElementById("xlsxFile");
        if (!fileInput.files.length) { alert("Seleziona un file .xlsx"); return; }
        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        const res = await fetch(`${serverUrl}/players/upload`, {method: "POST", body: formData});
        const data = await res.json();
        const msgEl = document.getElementById("uploadMsg");
        if (data.success) {
            msgEl.innerText = `Importati ${data.stats.importati} calciatori (esclusi ${data.stats.esclusi_fuori_lista} fuori lista, ${data.stats.esclusi_non_validi} non validi).`;
        } else {
            msgEl.style.color = "#d32f2f";
            msgEl.innerText = "Errore: " + data.error;
        }
    }

    function downloadMarkdown() {
        window.open(`${serverUrl}/players/markdown`, "_blank");
    }

    async function reloadMarkdown() {
        const fileInput = document.getElementById("mdFile");
        if (!fileInput.files.length) { alert("Seleziona il file markdown corretto"); return; }
        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        const res = await fetch(`${serverUrl}/players/reload_markdown`, {method: "POST", body: formData});
        const data = await res.json();
        const msgEl = document.getElementById("reloadMsg");
        if (data.success) {
            msgEl.innerText = `Lista ricaricata: ${data.totale} calciatori (stato chiamate/assegnazioni preservato).`;
        } else {
            msgEl.style.color = "#d32f2f";
            msgEl.innerText = "Errore: " + data.error;
        }
    }

    async function prepareQueue() {
        const ruolo = document.getElementById("callRuolo").value;
        const fascia = document.getElementById("callFascia").value;
        const ordine = document.getElementById("callOrdine").value;
        const requeue = document.getElementById("requeueUnsold").checked;

        await fetch(`${serverUrl}/config`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({requeue_unsold: requeue})
        });

        const res = await fetch(`${serverUrl}/call/filters`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ruolo: ruolo, fascia: fascia, ordine: ordine})
        });
        const data = await res.json();
        const statusEl = document.getElementById("queueStatus");
        if (data.success) {
            statusEl.innerText = `Coda pronta: ${data.queue_length} calciatori disponibili con questi filtri.`;
        } else {
            statusEl.innerText = "Errore: " + data.error;
        }
    }

    async function callNext() {
        const res = await fetch(`${serverUrl}/call/next`, {method: "POST"});
        const data = await res.json();
        if (!data.success) { alert("Errore: " + data.error); return; }
        showCurrentPlayerBadge(data.player);
        fetchState();
    }

    let searchTimeout = null;
    function searchManual() {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(async () => {
            const q = document.getElementById("manualSearch").value.trim();
            const resultsEl = document.getElementById("searchResults");
            if (q.length < 2) { resultsEl.innerHTML = ""; return; }
            const res = await fetch(`${serverUrl}/players/search?q=${encodeURIComponent(q)}`);
            const data = await res.json();
            if (!data.success) { resultsEl.innerHTML = ""; return; }
            resultsEl.innerHTML = data.matches.map(m =>
                `<div class="search-result-item" onclick="callById('${m.id}')">${ROLE_NAMES[m.ruolo]} - ${m.nome} (${m.squadra_reale}) - ${m.quotazione}</div>`
            ).join("") || "<p class='small-label'>Nessun risultato</p>";
        }, 300);
    }

    async function callById(playerId) {
        const res = await fetch(`${serverUrl}/call/by_id`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({player_id: playerId})
        });
        const data = await res.json();
        if (!data.success) { alert("Errore: " + data.error); return; }
        document.getElementById("manualSearch").value = "";
        document.getElementById("searchResults").innerHTML = "";
        showCurrentPlayerBadge(data.player);
        fetchState();
    }

    function showCurrentPlayerBadge(player) {
        const badge = document.getElementById("currentPlayerBadge");
        badge.classList.remove("hidden");
        document.getElementById("currentPlayerRuolo").innerText = ROLE_NAMES[player.ruolo] || player.ruolo;
        document.getElementById("currentPlayerNome").innerText = player.nome;
        document.getElementById("currentPlayerInfo").innerText = `${player.squadra_reale} - Quotazione ${player.quotazione}`;
    }

    async function fetchState() {
        try {
            const res = await fetch(`${serverUrl}/state`);
            const data = await res.json();

            if (data.config.mode !== currentMode) {
                applyModeUI(data.config.mode);
            }
            if (data.config.mode === "calciatori" && !document.getElementById("priceBandsForm").dataset.loaded) {
                renderPriceBandsForm(data.config.price_bands);
                document.getElementById("priceBandsForm").dataset.loaded = "1";
            }
            loadTeamRowsFromState(data);
            const countdownInput = document.getElementById("countdownSec");
            if (document.activeElement !== countdownInput) {
                countdownInput.value = data.config.countdown_seconds;
            }

            if (data.auction.current_player) {
                showCurrentPlayerBadge(data.auction.current_player);
            } else if (!data.auction.is_active) {
                document.getElementById("currentPlayerBadge").classList.add("hidden");
            }

            document.getElementById("currentBid").innerText = data.auction.current_bid;
            document.getElementById("totalBids").innerText = data.auction.total_bids_count;
            document.getElementById("timeLeft").innerText = data.auction.is_active ? data.auction.time_left : "--";

            let histHtml = "";
            if (data.history.length === 0) {
                histHtml = data.config.mode === "calciatori"
                    ? "<tr><td colspan='4'>Nessuna asta completata</td></tr>"
                    : "<tr><td colspan='2'>Nessuna asta completata</td></tr>";
            } else {
                data.history.forEach(item => {
                    if (data.config.mode === "calciatori" && item.player_name) {
                        histHtml += `<tr><td>${ROLE_NAMES[item.ruolo] || item.ruolo}</td><td>${item.player_name}</td><td>${item.winner}</td><td>${item.price} crediti</td></tr>`;
                    } else if (data.config.mode === "calciatori") {
                        histHtml += `<tr><td colspan="4">${item.winner} - ${item.price} crediti</td></tr>`;
                    } else {
                        histHtml += `<tr><td>${item.winner}</td><td>${item.price} crediti</td></tr>`;
                    }
                });
            }
            document.getElementById("historyTable").innerHTML = histHtml;
        } catch (e) {
            console.error(e);
        }
    }

    async function saveCountdown() {
        const countdownSec = document.getElementById("countdownSec").value;
        const res = await fetch(`${serverUrl}/config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ countdown_seconds: countdownSec })
        });
        const data = await res.json();
        if (data.success) alert("Countdown salvato!"); else alert("Errore: " + data.error);
    }

    // ---- Righe squadra (avatar + nome + crediti di partenza per riga) ----
    let teamRows = [];         // [{name, credits, avatarFile, avatarUrl}]
    let teamRowsLoaded = false;

    function renderTeamRows() {
        const container = document.getElementById("teamRowsContainer");
        container.innerHTML = teamRows.map((row, idx) => `
            <div class="team-row" data-idx="${idx}">
                <div class="team-row-avatar">
                    <img id="teamRowAvatarImg_${idx}" src="${row.avatarUrl || ''}" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22><rect width=%2224%22 height=%2224%22 fill=%22%23333%22/><text x=%2212%22 y=%2216%22 font-size=%2212%22 text-anchor=%22middle%22 fill=%22%23888%22>?</text></svg>'">
                    <input type="file" accept="image/*" onchange="onTeamAvatarSelected(${idx}, this)">
                </div>
                <input type="text" placeholder="Nome squadra" value="${row.name.replace(/"/g, '&quot;')}" oninput="teamRows[${idx}].name = this.value">
                <input type="number" placeholder="Crediti" value="${row.credits}" oninput="teamRows[${idx}].credits = this.value">
                <button class="danger remove-row" type="button" onclick="removeTeamRow(${idx})">&#10005;</button>
            </div>
        `).join("");
    }

    function addTeamRow() {
        teamRows.push({ name: "", credits: 500, avatarFile: null, avatarUrl: null });
        renderTeamRows();
    }

    function removeTeamRow(idx) {
        teamRows.splice(idx, 1);
        renderTeamRows();
    }

    function onTeamAvatarSelected(idx, input) {
        const file = input.files[0];
        if (!file) return;
        teamRows[idx].avatarFile = file;
        const reader = new FileReader();
        reader.onload = (e) => {
            teamRows[idx].avatarUrl = e.target.result;
            document.getElementById(`teamRowAvatarImg_${idx}`).src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    async function saveTeamsConfig() {
        const msgEl = document.getElementById("teamsConfigMsg");
        const cleanRows = teamRows.filter(r => r.name.trim() !== "");
        if (cleanRows.length === 0) {
            msgEl.style.color = "#d32f2f";
            msgEl.innerText = "Inserisci almeno una squadra";
            return;
        }
        const payload = cleanRows.map(r => ({ name: r.name.trim(), credits: parseInt(r.credits) || 0 }));
        const formData = new FormData();
        formData.append("teams_json", JSON.stringify(payload));
        cleanRows.forEach((r, idx) => {
            if (r.avatarFile) formData.append(`avatar_${idx}`, r.avatarFile);
        });
        const res = await fetch(`${serverUrl}/config/teams`, { method: "POST", body: formData });
        const data = await res.json();
        if (data.success) {
            msgEl.style.color = "#4caf50";
            msgEl.innerText = `Salvate ${data.totale_squadre} squadre` + (data.avatar_caricati.length ? ` (avatar caricati: ${data.avatar_caricati.join(", ")})` : "") + ".";
        } else {
            msgEl.style.color = "#d32f2f";
            msgEl.innerText = "Errore: " + data.error;
        }
    }

    function loadTeamRowsFromState(data) {
        if (teamRowsLoaded) return;
        const initial = data.config.teams_initial || {};
        const names = Object.keys(data.teams);
        teamRows = names.map(name => ({
            name: name,
            credits: initial[name] !== undefined ? initial[name] : data.teams[name],
            avatarFile: null,
            avatarUrl: `${serverUrl}/avatar/${encodeURIComponent(name)}?t=${Date.now()}`
        }));
        if (teamRows.length === 0) {
            teamRows.push({ name: "", credits: 500, avatarFile: null, avatarUrl: null });
        }
        teamRowsLoaded = true;
        renderTeamRows();
    }

    async function resetAll() {
        if (confirm("Sei sicuro di voler resettare crediti, storico e stato chiamate?")) {
            await fetch(`${serverUrl}/reset`, { method: "POST" });
            document.getElementById("priceBandsForm").dataset.loaded = "";
            fetchState();
        }
    }

    setInterval(fetchState, 1000);
    fetchState();
</script>
</body>
</html>
"""

# --- TEMPLATE HTML: HUB PARTECIPANTI ---
HUB_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
    <title>Fanta-Asta</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Baloo+2:wght@500;700;800&family=Nunito:wght@600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --pitch-dark: #2f8f3e;
            --pitch-light: #37a047;
            --green: #35b34a;
            --green-dark: #279438;
            --yellow: #ffc93c;
            --yellow-dark: #e6a800;
            --orange: #ff8a3d;
            --orange-dark: #e56e1f;
            --red: #ff5c5c;
            --red-dark: #e03e3e;
            --blue: #3fa7ff;
            --blue-dark: #1f86e0;
            --ink: #2b2b3a;
            --card-bg: #ffffff;
            --card-border: #2b2b3a;
        }
        * { box-sizing: border-box; }
        html, body { height: 100%; margin: 0; padding: 0; }
        body {
            font-family: 'Nunito', sans-serif;
            color: var(--ink);
            background: repeating-linear-gradient(100deg, var(--pitch-dark) 0px, var(--pitch-dark) 60px, var(--pitch-light) 60px, var(--pitch-light) 120px);
            display: flex; flex-direction: column; align-items: center;
            padding: 16px 14px 60px 14px;
            position: relative;
            overflow-x: hidden;
            transition: background 0.4s ease;
        }
        body.state-winning { background: repeating-linear-gradient(100deg, #2fae44 0px, #2fae44 60px, #39c752 60px, #39c752 120px); }
        body.state-losing { background: linear-gradient(180deg, #ffcf40 0%, #ffb700 100%); }

        /* ---------- Decorazioni tema calcistico ---------- */
        .pitch-lines { position: fixed; inset: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; opacity: 0.35; }
        .corner-arc { position: fixed; width: 60px; height: 60px; border: 3px solid rgba(255,255,255,0.4); border-radius: 50%; pointer-events: none; z-index: 0; }
        .corner-arc.bl { bottom: -40px; left: -40px; }
        .corner-arc.br { bottom: -40px; right: -40px; }
        .ball-deco { position: fixed; font-size: 26px; opacity: 0.5; pointer-events: none; z-index: 0; animation: spin-slow 6s linear infinite; }
        .ball-deco.b1 { top: 22px; left: 6%; }
        .ball-deco.b2 { top: 60px; right: 8%; animation-duration: 8s; }
        @keyframes spin-slow { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

        h1, h2, h3 { font-family: 'Baloo 2', cursive; margin: 0; }

        .app { width: 100%; max-width: 460px; position: relative; z-index: 1; }
        .hidden { display: none !important; }

        /* ---------- Card cartoon di base ---------- */
        .card {
            background: var(--card-bg);
            border: 3px solid var(--card-border);
            border-radius: 22px;
            box-shadow: 5px 6px 0 rgba(43,43,58,0.18);
            padding: 16px;
            min-width: 0;
        }
        .card h2 { font-size: 18px; color: var(--ink); margin-bottom: 8px; }
        .card-title-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .card-title-row .emoji { font-size: 20px; }

        /* ---------- Bottoni cartoon ---------- */
        .btn {
            font-family: 'Baloo 2', cursive;
            font-size: 16px;
            font-weight: 700;
            color: #fff;
            border: 3px solid var(--card-border);
            border-radius: 16px;
            padding: 12px 8px;
            cursor: pointer;
            width: 100%;
            height: 100%;
            box-shadow: 0 5px 0 rgba(0,0,0,0.25);
            transition: transform 0.08s ease, box-shadow 0.08s ease;
            background: var(--blue);
            display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
        }
        .btn:active { transform: translateY(4px); box-shadow: 0 1px 0 rgba(0,0,0,0.25); }
        .btn:disabled { background: #b9bcc4 !important; color: #6d707a; cursor: not-allowed; box-shadow: 0 5px 0 rgba(0,0,0,0.12); }
        .btn:disabled:active { transform: none; }
        .btn-orange { background: var(--orange); }
        .btn-yellow { background: var(--yellow-dark); }
        .btn-purple { background: #9a6bff; }
        .btn .cap { font-family: 'Nunito', sans-serif; font-size: 9px; font-weight: 800; opacity: 0.85; text-transform: uppercase; }

        select, input[type=text], input[type=number], input[type=file] {
            width: 100%; padding: 11px; margin: 6px 0; border-radius: 14px;
            border: 3px solid var(--card-border); background: #fff; color: var(--ink);
            font-family: 'Nunito', sans-serif; font-size: 16px; font-weight: 700;
        }
        label { font-weight: 800; font-size: 13px; color: #555; }

        .rotate-hint { text-align: center; color: #fff; font-weight: 800; font-size: 12px; margin-bottom: 8px; text-shadow: 1px 1px 0 rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; gap: 6px; }

        /* ==================== GRID SCHERMATA 1 ==================== */
        .screen1-grid {
            display: grid;
            grid-template-columns: 1fr;
            grid-template-areas: "select" "credits" "avatar" "enter";
            gap: 14px;
            width: 100%;
        }
        .ga-select { grid-area: select; }
        .ga-credits1 { grid-area: credits; }
        .ga-avatar1 { grid-area: avatar; }
        .ga-enter { grid-area: enter; }

        #screen1 .logo-title { text-align: center; margin-bottom: 16px; }
        #screen1 .logo-title h1 { font-size: 30px; color: #fff; text-shadow: 2px 3px 0 rgba(0,0,0,0.3); }
        #screen1 .logo-title p { font-family: 'Baloo 2', cursive; color: #12401c; font-size: 13px; background: rgba(255,255,255,0.7); display: inline-block; padding: 2px 10px; border-radius: 10px; }

        .avatar-wrap { display: flex; flex-direction: column; align-items: center; gap: 8px; height: 100%; justify-content: center; }
        .avatar-circle {
            width: 100px; height: 100px; border-radius: 50%;
            border: 4px solid var(--card-border);
            background: linear-gradient(180deg, #cdeeff, #fff);
            display: flex; align-items: center; justify-content: center;
            overflow: hidden; box-shadow: 4px 5px 0 rgba(43,43,58,0.18);
            font-size: 44px; flex-shrink: 0;
        }
        .avatar-circle img { width: 100%; height: 100%; object-fit: cover; }
        .credits-badge {
            display: inline-flex; align-items: center; gap: 8px;
            background: linear-gradient(180deg, #d9f7de, #b9f0c4);
            border: 3px solid var(--green-dark);
            border-radius: 16px; padding: 10px 14px; font-family: 'Baloo 2', cursive;
            font-size: 18px; color: var(--green-dark); font-weight: 700;
        }
        .ga-credits1 .card, .ga-avatar1 .card { height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; }
        #enterScreen2Btn.circle-enter {
            width: 130px; height: 130px; border-radius: 50%; font-size: 17px; padding: 6px;
            margin: auto; box-shadow: 0 6px 0 rgba(0,0,0,0.25);
        }
        .ga-enter { display: flex; align-items: center; justify-content: center; }

        @media (orientation: landscape) {
            body { padding: 6px; overflow: hidden; }
            .app { max-width: 100%; height: 100dvh; }
            #screen1 { height: 100%; display: flex; flex-direction: column; min-height: 0; }
            #screen1 .logo-title { margin-bottom: 4px; }
            #screen1 .logo-title h1 { font-size: 17px; }
            #screen1 .logo-title p { font-size: 10px; padding: 1px 7px; }
            .screen1-grid {
                flex: 1;
                min-height: 0;
                gap: 6px;
                grid-template-columns: 160px 1fr;
                grid-template-rows: auto 1fr 1fr;
                grid-template-areas:
                    "select select"
                    "credits enter"
                    "avatar enter";
            }
            .screen1-grid .card { padding: 6px; min-height: 0; overflow: hidden; }
            .ga-select .card h2, .ga-credits1 .card h2, .ga-avatar1 .card h2 { font-size: 12px; }
            .ga-select .card .card-title-row, .ga-credits1 .card .card-title-row, .ga-avatar1 .card .card-title-row { margin-bottom: 2px; }
            .ga-select select { padding: 7px; margin: 2px 0; font-size: 13px; }
            .ga-credits1 .credits-badge { font-size: 13px; padding: 5px 9px; }
            .ga-avatar1 .avatar-circle { width: 46px; height: 46px; font-size: 22px; border-width: 3px; }
            .ga-avatar1 input[type=file] { font-size: 10px; padding: 4px; margin: 3px 0 0 0; }
            #enterScreen2Btn.circle-enter { width: 92px; height: 92px; font-size: 12px; }
        }

        /* ==================== GRID SCHERMATA 2 ==================== */
        .screen2-grid {
            display: grid;
            grid-template-columns: 1fr;
            grid-template-areas: "banner" "avatar" "statsactions" "sidecredits" "sidehistory";
            gap: 12px;
            width: 100%;
        }
        .ga-banner { grid-area: banner; }
        .ga-avatar2 { grid-area: avatar; }
        .ga-statsactions { grid-area: statsactions; }
        .ga-sidecredits { grid-area: sidecredits; }
        .ga-sidehistory { grid-area: sidehistory; }

        .player-banner {
            background: linear-gradient(180deg, #fff6da, #ffe9a8);
            border: 3px solid var(--yellow-dark);
        }
        .player-banner .ruolo-pill {
            display: inline-block; background: var(--orange); color: #fff; font-weight: 800;
            font-size: 10px; letter-spacing: 1px; text-transform: uppercase;
            padding: 3px 9px; border-radius: 10px; margin-bottom: 4px;
        }
        .player-banner .nome { font-family: 'Baloo 2', cursive; font-size: 21px; color: var(--ink); line-height: 1.1; }
        .player-banner .dettagli { font-size: 12px; color: #665; font-weight: 700; }
        .player-banner.empty { text-align: center; color: #8a7a3a; font-weight: 700; font-size: 13px; }

        .ga-avatar2 .card { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; text-align: center; }
        .team-header .avatar-circle { width: 64px; height: 64px; font-size: 26px; flex-shrink: 0; }
        .team-header .team-name { font-family: 'Baloo 2', cursive; font-size: 17px; line-height: 1.15; }
        .team-header .switch-link { font-size: 11px; color: var(--blue-dark); font-weight: 800; cursor: pointer; text-decoration: underline; }

        .face-row { text-align: center; margin-bottom: 6px; }
        .face-row .face { font-size: 26px; line-height: 1; }
        .face-row .txt { font-family: 'Baloo 2', cursive; font-size: 12px; }

        /* Griglia unica 3x3 che contiene sia i numeri sia i pulsanti azione */
        .stats-actions-grid {
            display: grid;
            gap: 8px;
            grid-template-columns: repeat(3, 1fr);
            grid-template-areas:
                "timer credits bid"
                "b1 b5 b10"
                "bfree bfree bfree"
                "bbluff bbluff bnext";
        }
        .ga-timer { grid-area: timer; } .ga-credits3 { grid-area: credits; } .ga-bid { grid-area: bid; }
        .ga-b1 { grid-area: b1; } .ga-b5 { grid-area: b5; } .ga-b10 { grid-area: b10; }
        .ga-bfree { grid-area: bfree; } .ga-bbluff { grid-area: bbluff; } .ga-bnext { grid-area: bnext; }

        .stat-box { border-radius: 16px; border: 3px solid var(--card-border); padding: 8px 4px; background: #fff; text-align: center; }
        .stat-box .num { font-family: 'Baloo 2', cursive; font-size: 26px; line-height: 1; }
        .stat-box .lbl { font-size: 9px; font-weight: 800; text-transform: uppercase; color: #777; margin-top: 2px; }
        .stat-box.countdown { background: linear-gradient(180deg,#ffe0e0,#fff); }
        .stat-box.countdown .num { color: var(--red-dark); }
        .stat-box.credits { background: linear-gradient(180deg,#dcf7e1,#fff); }
        .stat-box.credits .num { color: var(--green-dark); }
        .stat-box.bid { background: linear-gradient(180deg,#ffe6cf,#fff); }
        .stat-box.bid .num { color: var(--orange-dark); }

        .freebid-row { display: flex; gap: 6px; height: 100%; }
        .freebid-row input { flex: 1; margin: 0; min-width: 0; }
        .freebid-row button { width: auto; flex-shrink: 0; padding: 0 14px; }

        .team-credit-line { display: flex; justify-content: space-between; padding: 5px 4px; border-bottom: 2px dashed #e2e2e2; font-weight: 700; font-size: 13px; }
        .team-credit-line:last-child { border-bottom: none; }

        table.hist-table { width: 100%; border-collapse: collapse; font-size: 12px; }
        table.hist-table th { background: #f1f1f6; padding: 5px; text-align: left; border-bottom: 3px solid var(--card-border); position: sticky; top: 0; }
        table.hist-table td { padding: 6px; border-bottom: 2px solid #eee; font-weight: 700; }

        @media (orientation: landscape) {
            body { padding: 6px; overflow: hidden; }
            .app { max-width: 100%; height: 100dvh; }
            #screen2 { height: 100%; display: flex; flex-direction: column; min-height: 0; }
            .rotate-hint { display: none; }
            .screen2-grid {
                flex: 1;
                min-height: 0;
                grid-template-columns: 108px 1fr 170px;
                grid-template-rows: auto 1fr 1fr;
                grid-template-areas:
                    "banner banner banner"
                    "avatar statsactions sidecredits"
                    "avatar statsactions sidehistory";
                gap: 6px;
            }
            .screen2-grid .card { padding: 6px 8px; min-height: 0; overflow: hidden; }
            .player-banner { padding: 6px 10px; }
            .player-banner .nome { font-size: 15px; }
            .player-banner .dettagli { font-size: 10px; }
            .player-banner .ruolo-pill { font-size: 8px; padding: 2px 6px; }
            .player-banner.empty { font-size: 11px; }
            .ga-avatar2 .avatar-circle { width: 42px; height: 42px; font-size: 20px; border-width: 3px; }
            .ga-avatar2 .team-name { font-size: 12px; }
            .ga-avatar2 .switch-link { font-size: 9px; }
            .ga-sidecredits .card, .ga-sidehistory .card { height: 100%; display: flex; flex-direction: column; min-height: 0; }
            .ga-sidecredits .card h2, .ga-sidehistory .card h2 { font-size: 12px; }
            .ga-sidecredits .card > div:last-child, .ga-sidehistory .card > div:last-child, .ga-sidehistory .card table { flex: 1; overflow-y: auto; min-height: 0; }
            .team-credit-line { font-size: 10px; padding: 3px 2px; }
            table.hist-table th, table.hist-table td { padding: 3px; font-size: 9px; }
            .ga-statsactions .card { height: 100%; min-height: 0; }
            .face-row { margin-bottom: 3px; }
            .face-row .face { font-size: 18px; }
            .face-row .txt { font-size: 9px; }
            .stats-actions-grid { height: 100%; grid-template-rows: repeat(4, 1fr); gap: 5px; }
            .stat-box { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 2px; }
            .stat-box .num { font-size: 18px; }
            .stat-box .lbl { font-size: 7px; }
            .btn { font-size: 11px; padding: 2px; border-width: 2px; box-shadow: 0 3px 0 rgba(0,0,0,0.25); border-radius: 10px; }
            .btn .cap { font-size: 7px; }
            .freebid-row input { padding: 4px; font-size: 11px; margin: 0; }
            .freebid-row button { padding: 0 8px; font-size: 11px; }
        }

        /* ---------- Overlay risultato ---------- */
        .result-overlay {
            position: fixed; inset: 0; background: rgba(20,20,30,0.72);
            display: flex; align-items: center; justify-content: center; z-index: 999; padding: 20px;
        }
        .result-box {
            text-align: center; padding: 30px 24px; border-radius: 26px; max-width: 320px;
            border: 5px solid var(--card-border); box-shadow: 6px 8px 0 rgba(0,0,0,0.3);
            animation: pop 0.35s ease;
        }
        @keyframes pop { 0% { transform: scale(0.6); opacity: 0; } 70% { transform: scale(1.06); } 100% { transform: scale(1); opacity: 1; } }
        .result-box.win { background: linear-gradient(180deg,#c8f7cf,#8fe39a); }
        .result-box.lose { background: linear-gradient(180deg,#ffe9b0,#ffc93c); }
        .result-box .big-emoji { font-size: 58px; }
        .result-box h1 { font-size: 24px; margin: 10px 0 6px 0; color: var(--ink); }
        .result-box button { margin-top: 10px; }
    </style>
</head>
<body>
    <svg class="pitch-lines" viewBox="0 0 1000 600" preserveAspectRatio="xMidYMid slice">
        <line x1="500" y1="0" x2="500" y2="600" stroke="white" stroke-width="4"></line>
        <circle cx="500" cy="300" r="90" fill="none" stroke="white" stroke-width="4"></circle>
        <circle cx="500" cy="300" r="4" fill="white"></circle>
    </svg>
    <div class="corner-arc bl"></div>
    <div class="corner-arc br"></div>
    <div class="ball-deco b1">&#9917;</div>
    <div class="ball-deco b2">&#9917;</div>

    <div class="app">

        <!-- ==================== SCHERMATA 1 ==================== -->
        <div id="screen1">
            <div class="logo-title">
                <h1>&#127942; Fanta-Asta</h1>
                <p>Entra nella tua asta con gli amici!</p>
            </div>

            <div class="screen1-grid">
                <div class="ga-select">
                    <div class="card">
                        <div class="card-title-row"><span class="emoji">&#127943;</span><h2>La tua squadra</h2></div>
                        <select id="myTeamSelect">
                            <option value="">Scegli la tua squadra...</option>
                        </select>
                    </div>
                </div>

                <div class="ga-credits1">
                    <div class="card">
                        <div class="card-title-row" style="justify-content:center;"><span class="emoji">&#128176;</span><h2>Crediti di partenza</h2></div>
                        <div class="credits-badge">&#11088; <span id="initialCreditsVal">--</span> crediti</div>
                    </div>
                </div>

                <div class="ga-avatar1">
                    <div class="card">
                        <div class="card-title-row" style="justify-content:center;"><span class="emoji">&#128444;&#65039;</span><h2>Il tuo avatar</h2></div>
                        <div class="avatar-wrap">
                            <div class="avatar-circle" id="avatarCircle1">&#128512;</div>
                            <input type="file" id="avatarFileInput" accept="image/*">
                        </div>
                    </div>
                </div>

                <div class="ga-enter">
                    <button class="btn btn-orange circle-enter" id="enterScreen2Btn" disabled onclick="enterScreen2()">Entra<br>nell'asta &#8594;</button>
                </div>
            </div>
        </div>

        <!-- ==================== SCHERMATA 2 ==================== -->
        <div id="screen2" class="hidden">
            <p class="rotate-hint" id="rotateHint">&#128241;&#8635; Ruota il telefono per la vista completa</p>

            <div class="screen2-grid">

                <div class="ga-banner">
                    <div class="card player-banner" id="playerBannerCard">
                        <div class="empty" id="playerBannerEmpty">In attesa del prossimo calciatore...</div>
                        <div id="playerBannerContent" class="hidden">
                            <span class="ruolo-pill" id="pbRuolo">-</span>
                            <div class="nome" id="pbNome">-</div>
                            <div class="dettagli" id="pbDettagli">-</div>
                        </div>
                    </div>
                </div>

                <div class="ga-avatar2">
                    <div class="card">
                        <div class="team-header" style="display:flex; flex-direction:column; align-items:center; gap:6px;">
                            <div class="avatar-circle" id="avatarCircle2">&#128512;</div>
                            <div class="team-name" id="myTeamName">-</div>
                            <div class="switch-link" onclick="backToScreen1()">Cambia squadra</div>
                        </div>
                    </div>
                </div>

                <div class="ga-statsactions">
                    <div class="card">
                        <div class="face-row" id="faceRow" style="display:none;">
                            <div class="face" id="faceEmoji">&#128512;</div>
                            <div class="txt" id="faceText">-</div>
                        </div>
                        <div class="stats-actions-grid">
                            <div class="stat-box countdown ga-timer">
                                <div class="num" id="statTimer">--</div>
                                <div class="lbl">Countdown</div>
                            </div>
                            <div class="stat-box credits ga-credits3">
                                <div class="num" id="statCredits">--</div>
                                <div class="lbl">Crediti tuoi</div>
                            </div>
                            <div class="stat-box bid ga-bid">
                                <div class="num" id="statBid">0</div>
                                <div class="lbl">Puntata</div>
                            </div>

                            <button class="btn ga-b1" style="background:var(--blue);" onclick="placeBid(1)">+1</button>
                            <button class="btn ga-b5" style="background:var(--blue-dark);" onclick="placeBid(5)">+5</button>
                            <button class="btn ga-b10" style="background:#2a6fce;" onclick="placeBid(10)">+10</button>

                            <div class="ga-bfree freebid-row">
                                <input type="number" id="customBidInput" placeholder="Puntata libera">
                                <button class="btn btn-purple" style="width:auto;" onclick="placeCustomBid()">Invia</button>
                            </div>

                            <button class="btn btn-orange ga-bbluff" id="bluffBtn" onclick="placeBluff()">
                                &#127917; <span id="bluffLabel">Bluff!</span>
                                <span class="cap" id="bluffCaption"></span>
                            </button>
                            <button class="btn btn-yellow ga-bnext" id="nextAuctionBtn" disabled onclick="callNextAuction()">
                                &#9193; Asta<br>Successiva
                            </button>
                        </div>
                    </div>
                </div>

                <div class="ga-sidecredits">
                    <div class="card">
                        <div class="card-title-row"><span class="emoji">&#128202;</span><h2>Crediti squadre</h2></div>
                        <div id="teamCreditsList">Caricamento...</div>
                    </div>
                </div>

                <div class="ga-sidehistory">
                    <div class="card">
                        <div class="card-title-row"><span class="emoji">&#128220;</span><h2>Storico aste</h2></div>
                        <table class="hist-table">
                            <thead id="historyHead"><tr><th>Squadra</th><th>Prezzo</th></tr></thead>
                            <tbody id="historyTable"><tr><td colspan="2">Nessuna asta completata</td></tr></tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    </div>

<script>
    const serverUrl = window.location.origin;
    const ROLE_NAMES = {P: "Portiere", D: "Difensore", C: "Centrocampista", A: "Attaccante"};

    let myTeam = localStorage.getItem("fanta_myTeam") || "";
    let onScreen2 = false;
    let lastTeamsSignature = "";
    let teamSelectInteracting = false;

    const teamSelectEl = document.getElementById("myTeamSelect");
    teamSelectEl.addEventListener("focus", () => { teamSelectInteracting = true; });
    teamSelectEl.addEventListener("mousedown", () => { teamSelectInteracting = true; });
    teamSelectEl.addEventListener("blur", () => { teamSelectInteracting = false; });
    teamSelectEl.addEventListener("change", () => {
        teamSelectInteracting = false;
        myTeam = teamSelectEl.value;
        localStorage.setItem("fanta_myTeam", myTeam);
        document.getElementById("enterScreen2Btn").disabled = !myTeam;
        refreshAvatarPreview();
    });

    function refreshAvatarPreview() {
        if (!myTeam) return;
        const url = `${serverUrl}/avatar/${encodeURIComponent(myTeam)}?t=${Date.now()}`;
        for (const id of ["avatarCircle1", "avatarCircle2"]) {
            const el = document.getElementById(id);
            const img = new Image();
            img.onload = () => { el.innerHTML = ""; el.appendChild(img.cloneNode()); };
            img.onerror = () => { el.innerHTML = "&#128512;"; };
            img.src = url;
        }
    }

    document.getElementById("avatarFileInput").addEventListener("change", async (e) => {
        if (!myTeam) { alert("Seleziona prima la tua squadra!"); e.target.value = ""; return; }
        const file = e.target.files[0];
        if (!file) return;
        const fd = new FormData();
        fd.append("team", myTeam);
        fd.append("file", file);
        const res = await fetch(`${serverUrl}/avatar/upload`, { method: "POST", body: fd });
        const data = await res.json();
        if (data.success) {
            refreshAvatarPreview();
        } else {
            alert("Errore caricamento avatar: " + data.error);
        }
    });

    function enterScreen2() {
        if (!myTeam) return;
        document.getElementById("screen1").classList.add("hidden");
        document.getElementById("screen2").classList.remove("hidden");
        onScreen2 = true;
        document.getElementById("myTeamName").innerText = myTeam;
        refreshAvatarPreview();
        fetchState();
    }

    function backToScreen1() {
        document.getElementById("screen2").classList.add("hidden");
        document.getElementById("screen1").classList.remove("hidden");
        onScreen2 = false;
    }

    let lastRoundId = null;
    let lastIsActive = false;

    async function fetchState() {
        try {
            const url = myTeam ? `${serverUrl}/state?team=${encodeURIComponent(myTeam)}` : `${serverUrl}/state`;
            const res = await fetch(url);
            const data = await res.json();

            document.getElementById("initialCreditsVal").innerText =
                (myTeam && data.config.teams_initial && data.config.teams_initial[myTeam] !== undefined)
                    ? data.config.teams_initial[myTeam]
                    : data.config.initial_credits;

            const teamsSignature = JSON.stringify(data.teams);
            if (!teamSelectInteracting && teamsSignature !== lastTeamsSignature) {
                let optionsHtml = "<option value=''>Scegli la tua squadra...</option>";
                for (let team of Object.keys(data.teams)) {
                    optionsHtml += `<option value="${team}" ${team === myTeam ? 'selected' : ''}>${team}</option>`;
                }
                teamSelectEl.innerHTML = optionsHtml;
                lastTeamsSignature = teamsSignature;
                document.getElementById("enterScreen2Btn").disabled = !(myTeam && Object.prototype.hasOwnProperty.call(data.teams, myTeam));
            }

            let teamsListHtml = "";
            for (let [team, crediti] of Object.entries(data.teams)) {
                teamsListHtml += `<div class="team-credit-line"><span>${team === myTeam ? '&#11088; ' : ''}${team}</span><span>${crediti}</span></div>`;
            }
            document.getElementById("teamCreditsList").innerHTML = teamsListHtml;

            if (myTeam && Object.prototype.hasOwnProperty.call(data.teams, myTeam)) {
                document.getElementById("statCredits").innerText = data.teams[myTeam];
            }

            const historyHead = document.getElementById("historyHead");
            const historyTable = document.getElementById("historyTable");
            if (data.config.mode === "calciatori") {
                historyHead.innerHTML = "<tr><th>Ruolo</th><th>Calciatore</th><th>Squadra</th><th>Prezzo</th></tr>";
                historyTable.innerHTML = data.history.length === 0
                    ? "<tr><td colspan='4'>Nessuna asta completata</td></tr>"
                    : data.history.map(item => item.player_name
                        ? `<tr><td>${ROLE_NAMES[item.ruolo] || item.ruolo}</td><td>${item.player_name}</td><td>${item.winner}</td><td>${item.price}</td></tr>`
                        : `<tr><td colspan="4">${item.winner} - ${item.price}</td></tr>`).join("");
            } else {
                historyHead.innerHTML = "<tr><th>Squadra</th><th>Prezzo</th></tr>";
                historyTable.innerHTML = data.history.length === 0
                    ? "<tr><td colspan='2'>Nessuna asta completata</td></tr>"
                    : data.history.map(item => `<tr><td>${item.winner}</td><td>${item.price}</td></tr>`).join("");
            }

            if (!onScreen2) { return; }

            const bannerEmpty = document.getElementById("playerBannerEmpty");
            const bannerContent = document.getElementById("playerBannerContent");
            if (data.config.mode === "calciatori" && data.auction.current_player) {
                bannerEmpty.classList.add("hidden");
                bannerContent.classList.remove("hidden");
                const p = data.auction.current_player;
                document.getElementById("pbRuolo").innerText = ROLE_NAMES[p.ruolo] || p.ruolo;
                document.getElementById("pbNome").innerText = p.nome;
                document.getElementById("pbDettagli").innerText = `${p.squadra_reale} - Quotazione ${p.quotazione}`;
            } else {
                bannerEmpty.classList.remove("hidden");
                bannerContent.classList.add("hidden");
                bannerEmpty.innerText = data.config.mode === "calciatori" ? "In attesa del prossimo calciatore..." : "Modalita' puntate libere";
            }

            document.getElementById("statTimer").innerText = data.auction.is_active ? data.auction.time_left : "--";
            document.getElementById("statBid").innerText = data.auction.current_bid;

            const faceRow = document.getElementById("faceRow");
            document.body.classList.remove("state-winning", "state-losing");
            // Il colore/faccina compaiono solo se esiste un'offerta VERA (has_real_bid):
            // un'asta avviata col bluff (nessuna offerta reale) resta neutra.
            if (myTeam && data.auction.is_active && data.auction.has_real_bid) {
                faceRow.style.display = "block";
                if (data.auction.is_leading) {
                    document.getElementById("faceEmoji").innerText = "\\ud83d\\ude04";
                    document.getElementById("faceText").innerText = "Stai vincendo tu!";
                    document.body.classList.add("state-winning");
                } else {
                    document.getElementById("faceEmoji").innerText = "\\ud83d\\ude10";
                    document.getElementById("faceText").innerText = "In testa c'e' un'altra squadra";
                    document.body.classList.add("state-losing");
                }
            } else {
                faceRow.style.display = "none";
            }

            // Etichetta e stato del pulsante Bluff, che si comporta diversamente
            // a seconda che l'asta sia gia' attiva o meno.
            const bluffBtn = document.getElementById("bluffBtn");
            const bluffLabel = document.getElementById("bluffLabel");
            const bluffCaption = document.getElementById("bluffCaption");
            if (!data.auction.is_active) {
                bluffLabel.innerText = "Bluff!";
                bluffCaption.innerText = "Avvia un'asta finta";
            } else {
                bluffLabel.innerText = "Bluff! +2s";
                bluffCaption.innerText = "Allunga il countdown";
            }
            bluffBtn.disabled = !(myTeam && data.auction.can_bluff);

            const nextBtn = document.getElementById("nextAuctionBtn");
            nextBtn.disabled = !(data.config.mode === "calciatori" && !data.auction.is_active);
            nextBtn.classList.toggle("hidden", data.config.mode !== "calciatori");

            if (lastRoundId !== null && data.auction.round_id !== lastRoundId) {
                const existing = document.getElementById("resultOverlay");
                if (existing) existing.remove();
            }
            lastRoundId = data.auction.round_id;

            if (lastIsActive === true && data.auction.is_active === false && myTeam) {
                const last = data.history[0];
                if (last && Array.isArray(last.participants) && last.participants.includes(myTeam)) {
                    if (last.winner === myTeam) {
                        showResultOverlay("win", "BRAVO, HAI VINTO!", "\\ud83c\\udfc6");
                    } else {
                        showResultOverlay("lose", "HAI PERSO, RABBINO!", "\\ud83d\\ude22");
                    }
                }
            }
            lastIsActive = data.auction.is_active;
        } catch (e) {
            console.error(e);
        }
    }

    function showResultOverlay(kind, message, emoji) {
        const existing = document.getElementById("resultOverlay");
        if (existing) existing.remove();
        const overlay = document.createElement("div");
        overlay.className = "result-overlay";
        overlay.id = "resultOverlay";
        overlay.innerHTML = `<div class="result-box ${kind}"><div class="big-emoji">${emoji}</div><h1>${message}</h1><button class="btn" style="background:var(--card-border);" onclick="document.getElementById('resultOverlay').remove()">Chiudi</button></div>`;
        document.body.appendChild(overlay);
    }

    async function placeBid(increment) {
        const current = parseInt(document.getElementById("statBid").innerText) || 0;
        await sendBidToServer(current + increment);
    }

    async function placeCustomBid() {
        const val = parseInt(document.getElementById("customBidInput").value);
        if (isNaN(val)) return alert("Inserisci una cifra valida");
        await sendBidToServer(val);
        document.getElementById("customBidInput").value = "";
    }

    async function sendBidToServer(amount) {
        if (!myTeam) { alert("Seleziona prima la tua squadra!"); return; }
        const res = await fetch(`${serverUrl}/bid`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ team: myTeam, amount: amount })
        });
        const data = await res.json();
        if (!data.success) {
            alert("Errore: " + data.error);
        } else {
            fetchState();
        }
    }

    async function placeBluff() {
        if (!myTeam) { alert("Seleziona prima la tua squadra!"); return; }
        const res = await fetch(`${serverUrl}/bid/bluff`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ team: myTeam })
        });
        const data = await res.json();
        if (!data.success) {
            alert("Errore: " + data.error);
        } else {
            fetchState();
        }
    }

    async function callNextAuction() {
        const btn = document.getElementById("nextAuctionBtn");
        btn.disabled = true;
        const res = await fetch(`${serverUrl}/call/next`, { method: "POST" });
        const data = await res.json();
        if (!data.success) {
            alert(data.error);
        }
        fetchState();
    }

    setInterval(fetchState, 1000);
    fetchState();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # threaded=True permette al server di rispondere a piu' partecipanti che
    # pollano /state contemporaneamente senza mettersi in coda uno alla volta.
    # E' sicuro perche' tutte le operazioni di lettura/scrittura sui DB sono
    # protette dal lock globale.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)

