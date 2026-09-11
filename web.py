"""
ASP Exam Checker - web UI cu conturi, monitor PARTAJAT si bot Telegram comun

Model (rescris 2026-07-22 pentru mai multi utilizatori):
  * UN SINGUR scan pentru toata lumea. Cat timp CEL PUTIN UN cont are monitorul
    pornit, scannerul ruleaza continuu si citeste locatiile DECA bifate de
    conturile active (din 18.08.2026 doar pe alea: fiecare citire consuma din
    cota zilnica a IDNP-ului de scanare).
    Al doilea utilizator care porneste nu declanseaza un al doilea scan - doar
    incepe sa primeasca notificari. Cand el opreste, scanul merge mai departe
    pentru ceilalti; se opreste abia cand nu mai ramane niciun cont pornit.
  * Fiecare cont e notificat DOAR pentru locatiile bifate de el (si lunile lui).
    Locatiile nebifate sunt scanate oricum, dar nu ajung la el.
  * UN SINGUR bot Telegram pentru toti (ASP_TG_TOKEN, setat de administrator).
    Utilizatorii nu mai introduc token/chat id: dau /start botului cu codul lor
    de conectare si atat.

Local:  python web.py                        -> http://127.0.0.1:8766
Cloud:  hostul seteaza PORT (Render)          -> bind 0.0.0.0 + Chromium headless
        ASP_TG_TOKEN=<token>       botul Telegram comun (de la @BotFather)
        ASP_REGISTER_CODE=<cod>    cod de invitatie pentru conturi noi (optional)
        ACCOUNTS_JSON=<json>       seed conturi pe disc efemer (auto-sincronizat)
        RENDER_API_KEY/RENDER_SERVICE_ID  persistenta conturilor intre restarturi

Migrare: la primul boot fara conturi, configul vechi (credentials.json /
CREDENTIALS_JSON) devine contul "admin" cu parola din ASP_UI_PASSWORD.
"""

import asyncio
import hashlib
import hmac
import html
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
USERS_DIR = os.path.join(HERE, "users")
LEGACY_CREDENTIALS = os.path.join(HERE, "credentials.json")
DEFAULT_PORT = 8766

# Botul Telegram comun. Nimeni nu-si mai pune token propriu in interfata:
# administratorul il seteaza o data aici, utilizatorii doar se conecteaza la el.
TG_TOKEN = (os.environ.get("ASP_TG_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
REGISTER_CODE = os.environ.get("ASP_REGISTER_CODE", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,20}$")
MAX_ACCOUNTS = 10

# ── incarca scrapper-ul (dev: dist/scrapper.py; copie deploy: scrapper.py) ──
scrapper = None
for _p in (os.path.join(HERE, "dist", "scrapper.py"), os.path.join(HERE, "scrapper.py")):
    if os.path.exists(_p):
        _spec = importlib.util.spec_from_file_location("scrapper", _p)
        if _spec and _spec.loader:
            scrapper = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(scrapper)
            break
if scrapper is None:
    raise RuntimeError("scrapper.py negasit (nici in dist/, nici langa web.py)")

# Aceleasi valori ca in app.py — pastreaza-le sincronizate
KNOWN_LOCATIONS = [
    "DECA Chișinău (str. Acad. S. Rădăuțanu, 1)",
    "DECA Chișinău (str. Calea Ieșilor, 14)",
    "DECA Chișinău (str. Salcâmilor, 28)",
]
DEFAULT_SETTINGS = {
    "idnp": "",
    "last_name": "",
    "first_name": "",
    "phone": "",
    "email": "",
    "id_series": "",
    "id_date_day": 1,
    "id_date_month": 1,
    "id_date_year": 2020,
    "medical_cert": "",
    # Telegram nu mai e in setari: botul e comun (TG_TOKEN) si chat-ul fiecarui
    # cont se leaga prin /start in bot (vezi ACCOUNTS["users"][name]["tg_chat"]).
    "interval_minutes": 5,
    "target_months": ["aprilie"],
    # Toate locatiile sunt SCANATE mereu; asta e doar lista pentru care contul
    # vrea sa fie notificat.
    "scrape_locations": list(KNOWN_LOCATIONS),
    "auto_update_enabled": False,
    "auto_update_idnp": "",
    "appointment_code": "",
    "request_number": "",
    "target_location": "",
    "current_appointment_date": "",
    "auto_update_mode": "earlier",
    # ── Prima programare (cerere noua, inca neprogramata) ────────────────────
    # Auto-update muta o programare EXISTENTA; asta prinde primul slot liber
    # pentru o cerere proaspata, folosind linkul unic dat de ASP dupa plata.
    # svc/loc sunt completate automat de /api/first-booking/probe si permit
    # verificarea calendarului fara browser (1 GET public).
    "first_booking_enabled": False,
    "first_booking_url": "",
    "first_booking_svc": "",
    "first_booking_loc": "",
    # Rezultatul se scrie o singura data, la succes, si opreste automat
    # cautarea - o cerere se programeaza o singura data.
    "first_booking_result": "",
}

# ── loguri: buffer global (server) + buffer per utilizator ───────────────────
class LogBuf:
    def __init__(self):
        self.lines, self.start, self.lock = [], 0, threading.Lock()

    def append(self, text):
        with self.lock:
            self.lines.append(text if text.endswith("\n") else text + "\n")
            overflow = len(self.lines) - 2000
            if overflow > 0:
                del self.lines[:overflow]
                self.start += overflow

    def since(self, n):
        with self.lock:
            i = max(0, n - self.start)
            return {"next": self.start + len(self.lines), "lines": self.lines[i:]}


GLOBAL_LOG = LogBuf()
USER_LOGS = {}          # username -> LogBuf
USER_LOGS_LOCK = threading.Lock()


def user_log(name):
    with USER_LOGS_LOCK:
        return USER_LOGS.setdefault(name, LogBuf())


BROADCAST = "*"  # ruta speciala: scanul partajat scrie in consola tuturor


class ThreadLogRouter:
    """stdout-ul unui thread merge in consola contului sau. Threadul scanului
    partajat foloseste ruta BROADCAST: acelasi scan e al tuturor, deci apare in
    consola fiecarui cont pornit."""

    def __init__(self, original):
        self.original = original
        self.routes = {}  # thread ident -> username | BROADCAST

    def write(self, text):
        if text.strip():
            name = self.routes.get(threading.get_ident())
            if name == BROADCAST:
                GLOBAL_LOG.append(text)
                for n in enabled_users():
                    user_log(n).append(text)
            elif name:
                user_log(name).append(text)
            else:
                GLOBAL_LOG.append(text)
        try:
            self.original.write(text)
            self.original.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass


ROUTER = ThreadLogRouter(sys.stdout)


def ulog(name, text):
    """Mesaj de control care apare in consola contului + logurile serverului."""
    user_log(name).append(text)
    try:
        ROUTER.original.write(f"[{name}] {text}\n")
        ROUTER.original.flush()
    except Exception:
        pass


def _tg_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Telegram: UN bot pentru toti utilizatorii ────────────────────────────────
_TG = {"username": "", "offset": 0}


def tg_call(method, payload=None, timeout=25):
    """Apel Telegram Bot API. Returneaza 'result' sau None (nu arunca)."""
    if not TG_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    try:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        body = json.loads(raw.decode("utf-8"))
        return body.get("result") if body.get("ok") else None
    except Exception:
        return None


def tg_send(chat_id, text):
    if not chat_id:
        return False
    return tg_call("sendMessage", {"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML"}) is not None


def tg_bot_username():
    if not _TG["username"] and TG_TOKEN:
        me = tg_call("getMe", timeout=15) or {}
        _TG["username"] = me.get("username", "")
    return _TG["username"]


def tg_chat_of(name):
    with ACCOUNTS_LOCK:
        u = ACCOUNTS["users"].get(name) or {}
        return u.get("tg_chat", "")


def tg_link_code(name):
    """Codul cu care contul se leaga de bot (/start <cod>). Se genereaza la
    cerere si se roteste la deconectare, ca un cod vazut de altcineva sa nu
    mai poata fi folosit."""
    with ACCOUNTS_LOCK:
        u = ACCOUNTS["users"].get(name)
        if u is None:
            return ""
        if not u.get("tg_code"):
            u["tg_code"] = secrets.token_urlsafe(9)
            code_changed = True
        else:
            code_changed = False
    if code_changed:
        persist_accounts()
    return ACCOUNTS["users"][name]["tg_code"]


def tg_set_chat(name, chat_id):
    with ACCOUNTS_LOCK:
        u = ACCOUNTS["users"].get(name)
        if u is None:
            return False
        u["tg_chat"] = str(chat_id) if chat_id else ""
        if not chat_id:
            u["tg_code"] = secrets.token_urlsafe(9)  # roteste codul la deconectare
    persist_accounts()
    return True


def tg_user_by_code(code):
    with ACCOUNTS_LOCK:
        for n, u in ACCOUNTS["users"].items():
            if code and u.get("tg_code") == code:
                return n
    return None


def tg_user_by_chat(chat_id):
    chat_id = str(chat_id)
    with ACCOUNTS_LOCK:
        for n, u in ACCOUNTS["users"].items():
            if u.get("tg_chat") == chat_id:
                return n
    return None


def notify_telegram(name, text):
    """Alerta de sistem pe Telegramul contului (monitor cazut, server repornit).
    Nu arunca niciodata."""
    chat = tg_chat_of(name)
    if not chat:
        return
    if not tg_send(chat, text):
        ulog(name, "[!] Alerta Telegram nelivrata.")


TG_HELP = ("Comenzi:\n"
           "/start &lt;cod&gt; - conecteaza acest chat la contul tau\n"
           "/status - starea monitorului tau\n"
           "/stop - deconecteaza chatul (nu mai primesti notificari)")


def _tg_handle_message(msg):
    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        if not arg:
            linked = tg_user_by_chat(chat_id)
            if linked:
                tg_send(chat_id, f"Chatul e deja conectat la contul "
                                 f"<b>{_tg_escape(linked)}</b>.\n\n{TG_HELP}")
            else:
                tg_send(chat_id,
                        "👋 Salut! Ca sa primesti notificari, deschide interfata "
                        "ASP Exam Checker, intra in contul tau si apasa "
                        "<b>Conecteaza Telegram</b>. Butonul te aduce inapoi aici "
                        "cu codul tau.\n\n" + TG_HELP)
            return
        name = tg_user_by_code(arg)
        if not name:
            tg_send(chat_id, "❌ Cod invalid sau expirat. Ia unul nou din "
                             "interfata web (butonul <b>Conecteaza Telegram</b>).")
            return
        previous = tg_user_by_chat(chat_id)
        if previous and previous != name:
            tg_set_chat(previous, "")  # un chat = un singur cont
        tg_set_chat(name, chat_id)
        ulog(name, "[TG] Telegram conectat.")
        tg_send(chat_id,
                f"✅ Conectat la contul <b>{_tg_escape(name)}</b>.\n"
                f"Aici primesti notificarile cand apar locuri la locatiile bifate "
                f"de tine.\n\n{TG_HELP}")
        return

    name = tg_user_by_chat(chat_id)
    if not name:
        tg_send(chat_id, "Chatul nu e conectat la niciun cont.\n\n" + TG_HELP)
        return

    if cmd == "/stop":
        tg_set_chat(name, "")
        tg_send(chat_id, f"🔌 Deconectat de la contul <b>{_tg_escape(name)}</b>. "
                         f"Nu mai primesti notificari aici.")
    elif cmd == "/status":
        s = load_settings(name)
        on = is_enabled(name)
        locs = s.get("scrape_locations") or []
        months = s.get("target_months") or []
        tg_send(chat_id,
                f"Cont: <b>{_tg_escape(name)}</b>\n"
                f"Monitor: <b>{'PORNIT' if on else 'oprit'}</b>"
                f"{' (scan partajat activ)' if monitor_running() else ''}\n"
                f"Luni urmarite: {_tg_escape(', '.join(months) or '-')}\n"
                f"Locatii: {_tg_escape(', '.join(locs) or 'niciuna bifata')}")
    else:
        tg_send(chat_id, TG_HELP)


def tg_poll_loop():
    """Long-polling getUpdates: singura cale prin care utilizatorii isi leaga
    chatul (nu exista webhook - serviciul free poate dormi)."""
    print(f"[i] Bot Telegram comun activ: @{tg_bot_username() or '?'}")
    while True:
        updates = tg_call("getUpdates",
                          {"offset": _TG["offset"], "timeout": 50,
                           "allowed_updates": ["message"]},
                          timeout=70)
        if updates is None:
            time.sleep(5)
            continue
        for upd in updates:
            _TG["offset"] = max(_TG["offset"], int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message")
            if msg:
                try:
                    _tg_handle_message(msg)
                except Exception as e:
                    print(f"[!] Eroare procesare mesaj Telegram: {e}")


# ── conturi ──────────────────────────────────────────────────────────────────
ACCOUNTS_LOCK = threading.RLock()
ACCOUNTS = {"_secret": "", "users": {}}


def user_path(name):
    return os.path.join(USERS_DIR, name + ".json")


def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def _check_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(_hash_pw(pw, salt), stored)
    except Exception:
        return False


def token_for(name):
    sig = hmac.new(ACCOUNTS["_secret"].encode(), name.encode(), "sha256").hexdigest()
    return f"{name}:{sig}"


def user_from_token(token):
    if ":" not in (token or ""):
        return None
    name, sig = token.rsplit(":", 1)
    with ACCOUNTS_LOCK:
        if name not in ACCOUNTS["users"]:
            return None
        good = hmac.new(ACCOUNTS["_secret"].encode(), name.encode(), "sha256").hexdigest()
    return name if hmac.compare_digest(sig, good) else None


def load_settings(name):
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(user_path(name), encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    s.pop("monitor_enabled", None)
    return s


def save_settings(name, body):
    """Aplica setarile primite peste cele existente. Returneaza None daca
    tab-ul care salveaza a fost incarcat inaintea unei modificari externe
    (alt tab/dispozitiv sau auto-update) - garda anti-suprascriere: un tab
    vechi a reintors o data corectata inapoi la valoarea veche."""
    s = load_settings(name)
    try:
        client_rev = int(body.get("_rev"))
    except (TypeError, ValueError):
        client_rev = None  # UI vechi (cache) fara _rev -> permite
    if client_rev is not None and client_rev != int(s.get("_rev", 0) or 0):
        return None
    old_url = (s.get("first_booking_url") or "").strip()
    for key in DEFAULT_SETTINGS:
        if key in body:
            s[key] = body[key]
    # Linkul schimbat => hash-urile serviciu/locatie de la cererea VECHE nu mai
    # sunt valide. Fara asta am rezerva pe calendarul altei cereri.
    if (s.get("first_booking_url") or "").strip() != old_url:
        s["first_booking_svc"] = ""
        s["first_booking_loc"] = ""
        s["first_booking_result"] = ""
    s["_rev"] = int(s.get("_rev", 0) or 0) + 1
    with open(user_path(name), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    persist_accounts()
    return s


def persist_accounts():
    """Impacheteaza conturi + setari si le scrie pe disc; sincronizeaza in
    env var-ul Render (discul e efemer - altfel restartul pierde conturile)."""
    with ACCOUNTS_LOCK:
        for name in ACCOUNTS["users"]:
            try:
                with open(user_path(name), encoding="utf-8") as f:
                    ACCOUNTS["users"][name]["settings"] = json.load(f)
            except Exception:
                pass
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(ACCOUNTS, f, indent=2, ensure_ascii=False)
        bundle = json.dumps(ACCOUNTS, ensure_ascii=False, separators=(",", ":"))
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        threading.Thread(target=_push_env_var, args=(bundle,), daemon=True).start()


_SYNC = {"lock": threading.Lock(), "pending": None, "alerted": False}


def _notify_all(text):
    with ACCOUNTS_LOCK:
        names = list(ACCOUNTS["users"])
    for n in names:
        notify_telegram(n, text)


def _push_env_var(bundle):
    """PUT + verificare prin recitire, cu retry. Un esec aici inseamna ca la
    urmatorul restart setarile revin silentios la valorile vechi - de aceea
    esecul repetat se anunta pe Telegram, iar revenirea la normal la fel."""
    _SYNC["pending"] = bundle
    with _SYNC["lock"]:  # serializeaza push-urile paralele (salvari rapide)
        bundle = _SYNC["pending"]
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/ACCOUNTS_JSON"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}",
                   "Content-Type": "application/json"}
        last_err = None
        for attempt in range(3):
            if attempt:
                time.sleep(3 * attempt)
            try:
                req = urllib.request.Request(
                    url, data=json.dumps({"value": bundle}).encode("utf-8"),
                    method="PUT", headers=headers)
                urllib.request.urlopen(req, timeout=20).read()
                # Recitire: un PUT "reusit" cu valoare trunchiata/nescrisa tot
                # revert la restart inseamna. Daca formatul difera, nu bloca.
                back = json.loads(urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers),
                    timeout=20).read().decode("utf-8"))
                stored = back.get("value") if isinstance(back, dict) else None
                if stored is not None and stored != bundle:
                    raise RuntimeError("valoarea recitita difera de cea trimisa")
                if _SYNC["alerted"]:
                    _SYNC["alerted"] = False
                    _notify_all("✅ <b>ASP Monitor</b>: salvarea persistenta a "
                                "setarilor functioneaza din nou.")
                print("[i] Conturi sincronizate in Render (persistente la restart)")
                return
            except Exception as e:
                last_err = e
        print(f"[!] Sync conturi->Render esuat (3 incercari): {last_err}")
        if not _SYNC["alerted"]:
            _SYNC["alerted"] = True
            _notify_all(
                f"⚠️ <b>ASP Monitor</b>: setarile NU au putut fi salvate persistent "
                f"({_tg_escape(last_err)}).\n"
                f"Dupa un restart al serverului vor reveni valorile vechi. "
                f"Verifica RENDER_API_KEY in Render → Environment.")


def _verify_persistence_target():
    """Verifica la boot ca RENDER_SERVICE_ID chiar e serviciul care ruleaza.

    Capcana concreta: cand serviciul e RECREAT in alt workspace/cont, oamenii
    copiaza variabilele vechi cu 'Add from .env' - inclusiv RENDER_SERVICE_ID si
    RENDER_API_KEY. Sync-ul ar reusi frumos... scriind ACCOUNTS_JSON in serviciul
    VECHI, iar cel nou ar pierde tot la fiecare restart, in tacere.
    RENDER_SERVICE_ID e oricum injectat automat de Render - nu trebuie setat de
    mana. Daca cel setat nu se potriveste, oprim sync-ul si anuntam.
    """
    global RENDER_API_KEY
    expected = os.environ.get("RENDER_SERVICE_NAME", "")
    if not (RENDER_API_KEY and RENDER_SERVICE_ID and expected):
        return
    try:
        req = urllib.request.Request(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"})
        info = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
        actual = info.get("name", "")
    except Exception as e:
        print(f"[!] Nu am putut verifica serviciul din RENDER_SERVICE_ID: {e}")
        return
    if actual and actual != expected:
        RENDER_API_KEY = ""  # opreste sync-ul: mai bine deloc decat in alt serviciu
        msg = (f"RENDER_SERVICE_ID arata spre '{actual}' dar serviciul care ruleaza "
               f"e '{expected}'")
        print(f"[!] EROARE CONFIG: {msg}. Sincronizarea setarilor a fost OPRITA "
              f"(altfel ar scrie in serviciul gresit). Sterge RENDER_SERVICE_ID din "
              f"Environment - Render il injecteaza automat cu valoarea corecta.")
        _notify_all(
            f"⚠️ <b>ASP Monitor - configuratie gresita</b>\n"
            f"{_tg_escape(msg)}.\n"
            f"Salvarea persistenta e OPRITA ca sa nu scrie in serviciul vechi. "
            f"Sterge <code>RENDER_SERVICE_ID</code> din Environment (Render il pune "
            f"singur) si redeploy.")
    else:
        print(f"[i] Persistenta verificata: sync catre '{actual or expected}'")


def create_account(name, password, settings=None, enabled=False):
    with ACCOUNTS_LOCK:
        if name in ACCOUNTS["users"]:
            return False, "Utilizatorul exista deja."
        if len(ACCOUNTS["users"]) >= MAX_ACCOUNTS:
            return False, "Limita de conturi atinsa."
        ACCOUNTS["users"][name] = {"pw": _hash_pw(password),
                                   "monitor_enabled": bool(enabled),
                                   "settings": settings or dict(DEFAULT_SETTINGS)}
        with open(user_path(name), "w", encoding="utf-8") as f:
            json.dump(ACCOUNTS["users"][name]["settings"], f, indent=2, ensure_ascii=False)
    persist_accounts()
    return True, "Cont creat."


def _fetch_bundle_from_render():
    """Valoarea CURENTA a lui ACCOUNTS_JSON, citita din API-ul Render.

    ⛔ CAPCANA (a costat setarile de doua ori in iulie si iar pe 18.08.2026):
    `os.environ["ACCOUNTS_JSON"]` e SNAPSHOT-UL DE LA ULTIMUL DEPLOY. Un restart
    care nu e deploy - OOM, crash, spin-down - reinjecteaza exact acel snapshot,
    nu valoarea scrisa intre timp de `_push_env_var`. Rezultatul: dupa restartul
    din 18.08 (OOM in timpul reprogramarii) setarile au sarit inapoi la Calea
    Iesilor / 15.08, adica monitorul a ramas orb - filtra dupa alta locatie si
    dupa o data din trecut, fara sa spuna nimic.

    API-ul Render tine mereu ultima valoare (o si recitim la fiecare push), deci
    la boot ea e sursa de adevar, iar env-ul ramane doar rezerva.
    """
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        return ""
    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/ACCOUNTS_JSON"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {RENDER_API_KEY}"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    except Exception as e:
        print(f"[!] Nu am putut citi ACCOUNTS_JSON din API-ul Render: {e}")
        return ""
    # Formatul raspunsului difera intre versiunile API-ului ({value} vs {envVar}).
    value = ""
    if isinstance(data, dict):
        value = data.get("value") or (data.get("envVar") or {}).get("value") or ""
    return value if isinstance(value, str) else ""


def boot_accounts():
    global ACCOUNTS
    os.makedirs(USERS_DIR, exist_ok=True)
    remote = _fetch_bundle_from_render()
    if remote:
        try:
            json.loads(remote)  # nu suprascriem discul cu ceva ne-JSON
        except ValueError as e:
            print(f"[!] ACCOUNTS_JSON din API e invalid ({e}) - folosesc env/disc.")
        else:
            stale = os.environ.get("ACCOUNTS_JSON", "")
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                f.write(remote)
            if stale and stale != remote:
                print("[i] ACCOUNTS_JSON din API difera de snapshotul de la deploy - "
                      "am pastrat valoarea din API (altfel setarile ar fi revenit).")
            else:
                print("[i] accounts.json luat din API-ul Render (valoarea curenta)")
    if not os.path.exists(ACCOUNTS_FILE) and os.environ.get("ACCOUNTS_JSON"):
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            f.write(os.environ["ACCOUNTS_JSON"])
        print("[i] accounts.json creat din variabila ACCOUNTS_JSON")
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                ACCOUNTS = json.load(f)
        except Exception as e:
            print(f"[!] Eroare citire accounts.json: {e}")
    ACCOUNTS.setdefault("users", {})
    if not ACCOUNTS.get("_secret"):
        ACCOUNTS["_secret"] = secrets.token_hex(16)
    # Migrare bot-per-utilizator -> bot comun: chat-ul salvat in setari devine
    # legatura contului, iar token-ul personal dispare (nu mai e folosit).
    for name, u in ACCOUNTS["users"].items():
        s = u.get("settings") or {}
        if not u.get("tg_chat") and s.get("telegram_chat_id"):
            u["tg_chat"] = str(s["telegram_chat_id"])
            print(f"[i] {name}: chat Telegram preluat din setarile vechi")
        s.pop("telegram_token", None)
        s.pop("telegram_chat_id", None)
        u["settings"] = s
    # scrie fisierele per-utilizator (hot-reload-ul scrapper-ului citeste de acolo)
    for name, u in ACCOUNTS["users"].items():
        try:
            with open(user_path(name), "w", encoding="utf-8") as f:
                json.dump(u.get("settings", {}), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[!] Nu am putut scrie setarile pentru {name}: {e}")

    # migrare: configul vechi mono-utilizator -> contul "admin"
    if not ACCOUNTS["users"]:
        if not os.path.exists(LEGACY_CREDENTIALS) and os.environ.get("CREDENTIALS_JSON"):
            with open(LEGACY_CREDENTIALS, "w", encoding="utf-8") as f:
                f.write(os.environ["CREDENTIALS_JSON"])
        if os.path.exists(LEGACY_CREDENTIALS):
            try:
                with open(LEGACY_CREDENTIALS, encoding="utf-8") as f:
                    legacy = json.load(f)
                enabled = bool(legacy.pop("monitor_enabled", False))
                legacy_chat = str(legacy.pop("telegram_chat_id", "") or "")
                legacy.pop("telegram_token", None)
                pw = os.environ.get("ASP_UI_PASSWORD") or secrets.token_urlsafe(9)
                if not os.environ.get("ASP_UI_PASSWORD"):
                    print(f"[i] Parola generata pentru contul admin: {pw}")
                create_account("admin", pw, settings=legacy, enabled=enabled)
                if legacy_chat:
                    tg_set_chat("admin", legacy_chat)
                print("[i] Cont 'admin' creat din configul existent (parola = ASP_UI_PASSWORD)")
            except Exception as e:
                print(f"[!] Migrare config vechi esuata: {e}")
    persist_accounts()


def _watch_settings():
    """Prinde scrierile facute direct de scrapper (_persist_current_date)."""
    last = {}
    while True:
        time.sleep(10)
        changed = False
        try:
            for fn in os.listdir(USERS_DIR):
                p = os.path.join(USERS_DIR, fn)
                m = os.path.getmtime(p)
                if fn in last and m != last[fn]:
                    changed = True
                last[fn] = m
        except OSError:
            continue
        if changed:
            persist_accounts()


# ── monitor PARTAJAT ─────────────────────────────────────────────────────────
# Un singur scan pentru toata lumea: site-ul e citit o data pe ciclu, indiferent
# cati utilizatori sunt porniti, iar rezultatul e filtrat separat pentru fiecare.
MONITOR = {"thread": None, "running": False, "stop": threading.Event(),
           "wake": threading.Event(), "users": {}, "fails": 0,
           # Bucla asyncio a scanului, cat timp ruleaza (vezi _run_async).
           "loop": None}
MONITOR_LOCK = threading.RLock()


# ── cota zilnica ASP ─────────────────────────────────────────────────────────
# 18.08.2026: ASP a pus o limita pe cate ori poate citi UN IDNP calendarul
# (POST qmatic/dates) intr-o zi. Masurat cu sonde din 3 puncte: 429 vine si de
# acasa, la prima cerere a zilei; un ALT IDNP de pe acelasi IP primeste 403, nu
# 429; iar Retry-After scade odata cu ceasul spre 00:00 UTC. Deci limita e pe
# IDNP, nu pe IP, si fereastra e ziua UTC (03:00 ora Chisinaului).
#
# Consecinta: scanul are un numar FIX de citiri pe zi. Ce se face cu ele e o
# ALEGERE, si utilizatorul a facut-o explicit pe 19.08.2026 ("make it work,
# dont change the 2min time"): ramanem la intervalul cerut de el si cheltuim
# bugetul cat tine, in loc sa-l intindem pe 24h cu scanari rare.
# Ce castigam totusi, fara sa atingem ritmul:
#   * o singura locatie citita (cea bifata) in loc de trei => de 3 ori mai
#     multe cicluri din acelasi buget; la 2 minute inseamna 720 citiri/zi;
#   * 429 nu mai e "pana": dormim pana la resetare, cu UN mesaj informativ,
#     in loc de alerte rosii din 10 in 10 cicluri (18.08.2026, 30 la rand);
#   * bugetul se INVATA (cate citiri a acceptat ASP), ca sa stim cat tine
#     ritmul asta si sa i-o putem spune omului.
# QUOTA_SPREAD=True ar imparti bugetul uniform pe fereastra (scanari mai rare,
# dar acoperire pana seara). Lasat pe False: e decizia utilizatorului.
QUOTA_SPREAD = False
QUOTA_FILE = os.path.join(HERE, "quota.json")
# Punctul de plecare, pana invata singur: pe 18.08.2026 scanul a mers de la
# resetarea de la 03:00 pana pe la 08:50 cu 3 locatii la fiecare 2 minute -
# adica ~500 de citiri inainte de 429. Pornim vizibil sub estimarea aia (o
# citire la 5 minute) si urcam cu 50% pe zi cat timp nu lovim peretele.
QUOTA_START_TARGET = 288
QUOTA_MIN_TARGET = 12
QUOTA_MAX_TARGET = 720       # peste asta oricum n-are rost (o citire/2 min)


def _utc_day(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")


def _quota_reset_at(ts=None):
    """Momentul (epoch) urmatoarei resetari: 00:00 UTC."""
    now = datetime.fromtimestamp(ts or time.time(), timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def _quota_load():
    try:
        with open(QUOTA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _quota_save(q):
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=1)
    except Exception:
        pass  # disc efemer pe Render: fara fisier doar reinvatam de la zero


def quota_state():
    """Starea cotei pentru fereastra curenta, rotita cand se schimba ziua UTC."""
    q = MONITOR.get("quota") or _quota_load()
    today = _utc_day()
    if q.get("window") != today:
        target = int(q.get("target") or QUOTA_START_TARGET)
        # Fereastra trecuta s-a terminat fara 429 desi ne-am cheltuit tinta =>
        # ASP accepta mai mult decat am incercat. Crestem cu 50%.
        if q and not q.get("exhausted") and q.get("spent", 0) >= 0.9 * target:
            target = min(int(target * 1.5), QUOTA_MAX_TARGET)
        q = {"window": today, "spent": 0, "exhausted": False, "resume_at": 0.0,
             "target": max(QUOTA_MIN_TARGET, target),
             "budget": q.get("budget"),
             "reads_per_cycle": max(1, int(q.get("reads_per_cycle") or 1))}
        _quota_save(q)
    MONITOR["quota"] = q
    return q


def enabled_users():
    with ACCOUNTS_LOCK:
        return [n for n, u in ACCOUNTS["users"].items() if u.get("monitor_enabled")]


def scan_identity(users):
    """Identitatea cu care interogam calendarul comun: (idnp, serie, data emiterii).

    Din 2026-08-17 endpointul qmatic/dates (POST) cere IDNP + serie buletin +
    data emiterii, altfel 403. Raspunsul NU depinde de identitate atata timp cat
    e valida (aceleasi zile pentru toti), deci scanul ramane unul singur pentru
    toti - luam prima identitate COMPLETA dintre conturile active (toate trei
    campurile, ca ASP sa nu refuze scanul). issueDate = 'YYYY-MM-DDT00:00:00'.
    """
    for name in users:
        s = load_settings(name)
        idnp = (s.get("idnp") or "").strip()
        seria = (s.get("id_series") or "").strip()
        issue = scrapper.iso_issue_date(s.get("id_date_day"),
                                        s.get("id_date_month"),
                                        s.get("id_date_year"))
        if idnp and seria and issue:
            return idnp, seria, issue
    # Nicio identitate completa: intoarce ce avem (idnp-ul primului) ca eroarea
    # din scanner sa numeasca exact ce lipseste.
    for name in users:
        idnp = (load_settings(name).get("idnp") or "").strip()
        if idnp:
            return idnp, "", ""
    return "", "", ""


def is_enabled(name):
    with ACCOUNTS_LOCK:
        u = ACCOUNTS["users"].get(name) or {}
        return bool(u.get("monitor_enabled"))


def monitor_running():
    return bool(MONITOR["running"])


def is_running(name):
    """Pentru interfata: contul primeste notificari (scanul e partajat)."""
    return is_enabled(name) and monitor_running()


def set_enabled(name, value):
    with ACCOUNTS_LOCK:
        if name in ACCOUNTS["users"]:
            if ACCOUNTS["users"][name].get("monitor_enabled") == value:
                return
            ACCOUNTS["users"][name]["monitor_enabled"] = value
    persist_accounts()


def _user_state(name):
    """Stare per cont in cadrul scanului partajat (mesajul de prima verificare
    si alertele de pana se dau per utilizator, nu global)."""
    return MONITOR["users"].setdefault(
        name, {"first_done": False, "issue_active": False})


def start_monitor(name):
    if is_enabled(name) and monitor_running():
        return False, "Monitorul tau ruleaza deja."
    if not TG_TOKEN:
        return False, ("Botul Telegram nu e configurat pe server "
                       "(ASP_TG_TOKEN lipseste) - anunta administratorul.")
    if not tg_chat_of(name):
        return False, ("Conecteaza-te intai la botul de Telegram "
                       "(butonul 'Conecteaza Telegram'), altfel nu ai unde primi notificari.")
    if not (load_settings(name).get("scrape_locations") or []):
        return False, ("Nu ai bifata nicio locatie - nu ai primi nicio notificare. "
                       "Bifeaza cel putin una si incearca din nou.")
    MONITOR["users"].pop(name, None)  # reporneste = primesti iar prima verificare
    set_enabled(name, True)
    ulog(name, "[>] Monitor pornit - primesti notificari pentru locatiile bifate.")
    ensure_monitor()
    MONITOR["wake"].set()  # scan imediat, ca sa vina repede prima verificare
    return True, "Monitor pornit."


def stop_monitor(name):
    if not is_enabled(name):
        return False, "Monitorul nu ruleaza."
    set_enabled(name, False)
    MONITOR["users"].pop(name, None)
    others = len(enabled_users())
    ulog(name, "[X] Monitor oprit - nu mai primesti notificari.")
    if others:
        return True, (f"Oprit. Scanul continua pentru ceilalti {others} "
                      f"utilizatori activi, dar tu nu mai esti notificat.")
    MONITOR["stop"].set()
    MONITOR["wake"].set()
    return True, "Oprit - niciun alt utilizator activ, scanul se opreste."


def ensure_monitor():
    """Porneste threadul de scan daca nu ruleaza deja."""
    with MONITOR_LOCK:
        if MONITOR["running"]:
            return
        MONITOR["stop"].clear()
        MONITOR["wake"].clear()
        MONITOR["running"] = True
        MONITOR["fails"] = 0
        t = threading.Thread(target=_monitor_thread, daemon=True)
        MONITOR["thread"] = t
        t.start()


def _monitor_thread():
    ROUTER.routes[threading.get_ident()] = BROADCAST
    error = None
    try:
        asyncio.run(_shared_loop())
    except Exception as e:
        error = e
        print(f"[!] EROARE scan partajat: {e}")
    finally:
        MONITOR["running"] = False
        MONITOR["loop"] = None  # bucla a murit; _run_async trebuie sa faca alta
        ROUTER.routes.pop(threading.get_ident(), None)
        print("[X] Scan partajat oprit.")
        # Oprire NECERUTA -> conturile inca pornite cred ca sunt monitorizate.
        if not MONITOR["stop"].is_set():
            reason = f"eroare: {_tg_escape(error)}" if error else "cauza necunoscuta"
            for n in enabled_users():
                ulog(n, f"[!] Scanul s-a oprit NEASTEPTAT ({reason}).")
                notify_telegram(
                    n,
                    f"🛑 <b>ASP Monitor s-a OPRIT neasteptat!</b>\n"
                    f"Cauza: {reason}\n"
                    f"Nu mai verific locurile - deschide site-ul si porneste-l din nou.")


def _pretty(months):
    return ", ".join(m.capitalize() for m in months)


def _escape(text, limit: int = 300) -> str:
    """Text strain (erori) pregatit pentru mesajele Telegram parse_mode=HTML.

    Corpul unei erori poate fi chiar HTML (pagina de eroare nginx la 402); daca
    nu il escapam, Telegram respinge mesajul si alerta nu mai pleaca deloc.
    """
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return html.escape(text)


async def _notify_user(name, settings, raw, scan_failed, diagnostics):
    """Filtreaza rezultatul scanului comun pentru UN cont si il anunta.

    Locatiile nebifate au fost scanate (o data, pentru toti), dar nu ajung
    niciodata in mesajul lui.
    """
    st = _user_state(name)
    chat = tg_chat_of(name)

    async def send(text):
        await asyncio.to_thread(tg_send, chat, text)

    # ── pana de scanare: alerta la primul esec + la fiecare 10 la rand ──
    if scan_failed:
        if not st["issue_active"] or MONITOR["fails"] % 10 == 0:
            # Fara textul erorii toate cauzele arata identic ("Eroare=DA") si
            # diagnosticul cere logurile serverului - vezi pana din 04.08.2026.
            why = _escape(diagnostics.get("error_text") or "")
            await send(
                "🛑 <b>MONITORUL NU POATE SCANA SITE-UL ASP!</b>\n"
                f"Verificari esuate la rand: <b>{MONITOR['fails']}</b>.\n"
                "⚠️ NU primesti notificari de locuri cat timp dureaza asta - "
                "NU inseamna ca nu sunt locuri libere!\n"
                f"Detalii: Locatii={diagnostics['locations_found']}, "
                f"Calendar indisponibil={diagnostics['calendar_unavailable_count']}, "
                f"Eroare={'DA' if diagnostics['had_check_error'] else 'NU'}\n"
                + (f"Cauza: <code>{why}</code>\n" if why else "")
                + f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            st["issue_active"] = True
    elif st["issue_active"]:
        await send("✅ <b>Scanarea functioneaza din nou!</b>\n"
                   "Monitorul citeste iar calendarul - notificarile merg normal.\n"
                   f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        st["issue_active"] = False

    months = [str(m).lower() for m in (settings.get("target_months") or [])]
    # legacy_fallback=False: nebifat = nenotificat, fara exceptii.
    labels = scrapper.filter_locations(list(raw), settings.get("scrape_locations"),
                                       legacy_fallback=False)
    results = {lab: scrapper.days_by_month(raw[lab], months) for lab in labels}

    # Cand auto-update e activ, notificarile arata doar zilele mai devreme decat
    # programarea curenta (restul sunt zgomot cand deja ai o programare).
    au_on = bool(settings.get("auto_update_enabled"))
    au_mode = (settings.get("auto_update_mode") or "earlier").strip().lower()
    current_dt = (scrapper.parse_current_appointment_date(
        settings.get("current_appointment_date", "")) if au_on else None)
    in_watched = (current_dt is not None
                  and scrapper.RO_MONTHS_FULL[current_dt.month - 1] in months)
    cutoff = None if (au_mode == "any" and not in_watched) else current_dt

    per_month = {m: [] for m in months}
    for loc, by_month in results.items():
        for month, days in by_month.items():
            days = scrapper._earlier_than(days, month, cutoff)
            if days:
                per_month[month].append((loc, days))

    ulog(name, f"[i] {name}: {sum(len(v) for v in per_month.values())} locatii cu zile "
               f"in lunile urmarite ({_pretty(months) or '-'}), "
               f"din {len(labels)} locatii bifate.")

    if not st["first_done"]:
        st["first_done"] = True
        lines = ["🔍 <b>ASP Monitor - Prima verificare</b>", ""]
        if any(per_month.values()):
            for month in months:
                for loc, days in per_month[month]:
                    lines.append(f"📍 <b>{loc}</b> - {month.capitalize()}")
                    lines += [f"   • {d}" for d in days]
                    lines.append("")
        elif scan_failed:
            # NU spune "nicio zi disponibila" cand de fapt scanarea a esuat.
            lines.append("🛑 Prima verificare a ESUAT (problema tehnica, nu lipsa de locuri).")
            lines.append("Vezi alerta separata - reincerc automat.")
        elif not labels:
            lines.append("⚠️ Nu ai bifata nicio locatie - nu am ce sa-ti anunt.")
            lines.append("Bifeaza cel putin o locatie in interfata web.")
        elif diagnostics.get("identity_bad"):
            # Identitate incompleta/gresita != "nicio zi": nu putem citi, deci
            # nu pretindem ca nu-s locuri.
            lines.append("⚠️ Nu pot citi calendarul: IDNP + seria buletinului + "
                         "data emiterii trebuie completate/corecte in setari.")
            lines.append("Corecteaza-le si repornesc verificarea. 👀")
        else:
            lines.append("Nicio zi disponibila momentan.")
            lines.append(f"Te notific cand apare ceva in: {_pretty(months)}. 👀")
        lines.append(f"\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        await send("\n".join(lines))
    elif any(per_month.values()):
        lines = ["🚨 <b>LOCURI DISPONIBILE IN LUNILE URMARITE!</b>", ""]
        for month in months:
            if not per_month[month]:
                continue
            lines.append(f"📅 <b>{month.capitalize()}</b>")
            for loc, days in per_month[month]:
                lines.append(f"📍 <b>{loc}</b>")
                lines += [f"   • {d}" for d in days]
                lines.append("")
        lines.append(f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        lines.append("🔗 https://eservicii.gov.md/asp/dimtcca/cerere/apo01")
        await send("\n".join(lines))

    return results, current_dt, au_mode, in_watched


async def _auto_update_user(name, settings, results, current_dt, au_mode, in_watched):
    """Reprogramare automata pentru UN cont (singurul pas care mai are nevoie
    de Chromium). Ruleaza secvential, dupa scanul comun."""
    chat = tg_chat_of(name)

    async def send(text):
        await asyncio.to_thread(tg_send, chat, text)

    earliest = scrapper.find_earliest_available(results, settings.get("target_location", ""))
    if not current_dt:
        ulog(name, "  [AU] 'Data programare curenta' lipseste sau invalida - skip auto-update.")
        return
    if not earliest:
        return
    if au_mode == "any" and not in_watched:
        should = earliest[1] != current_dt
    else:
        should = earliest[1] < current_dt
    if not should:
        ulog(name, f"  [AU] Cea mai devreme data ({earliest[1].strftime('%d.%m.%Y')}) "
                   f"nu e mai buna decat cea curenta ({current_dt.strftime('%d.%m.%Y')}) "
                   f"[mod={au_mode}].")
        return

    loc_label, new_dt = earliest
    ulog(name, f"  [AU] Data mai buna gasita: {new_dt.strftime('%d.%m.%Y')} la {loc_label}. "
               f"Pornesc auto-update...")
    await send(f"🔄 <b>Auto-update pornit</b>\n"
               f"Data noua: <b>{new_dt.strftime('%d.%m.%Y')}</b>\n"
               f"Locatie: {loc_label}\n"
               f"Inlocuieste programarea din {current_dt.strftime('%d.%m.%Y')}.")

    # Marcaj persistent cat tine reprogramarea: Chromium + pagina Blazor cer ~670
    # MB, adica peste limita de 512 MB a instantei, deci procesul poate fi omorat
    # de OOM chiar aici (18.08.2026). Fara marcaj, un OOM arata pe Telegram exact
    # ca tacerea - dupa restart nu afla nimeni ce s-a intamplat cu reprogramarea.
    _patch_settings(name, au_in_progress=f"{new_dt.strftime('%d.%m.%Y')} | {loc_label}")

    ok, ctx, au_out = False, None, {}
    # ── Calea rapida: reprogramare din 6 apeluri HTTP, fara Chromium ──────
    # Browserul cere ~670 MB pe o instanta de 512 MB si de-aia a fost omorata
    # reprogramarea din 18.08.2026. Calea prin API nu foloseste RAM si e si mai
    # rapida, deci o incercam prima. Cadem pe browser DOAR daca API-ul crapa
    # tehnic (ruta mutata de ASP, releu picat) - un refuz de business ("ziua nu
    # mai are ore") si-a trimis deja mesajul, nu-l repetam cu al doilea flux.
    try:
        ok = await scrapper.api_rebook_appointment(settings, new_dt, loc_label,
                                                   notify=send, out=au_out)
        api_broke = False
    except Exception as e:
        api_broke = True
        ulog(name, f"  [AU] Calea API a esuat tehnic ({e}) - incerc prin browser.")

    if not ok and api_broke:
        try:
            ctx, _page = await scrapper.new_page()
            ok = await scrapper.auto_update_appointment(ctx, settings, new_dt,
                                                        notify=send, out=au_out)
        except Exception as e:
            ulog(name, f"  [AU] Browserul pentru auto-update nu a pornit: {e}")
            await send(f"❌ Auto-update: nici prin API, nici prin browser "
                       f"({_tg_escape(e)}). Reincerc la urmatoarea verificare.")
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
            await scrapper.close_browser()
    _patch_settings(name, au_in_progress="")

    if ok:
        # Data NOUA + perechea cod/numar NOUA (o reprogramare le schimba si le
        # invalideaza pe cele vechi) merg in setari automat, altfel urmatorul
        # auto-update cauta cu date moarte si nu gaseste nimic.
        new_str = new_dt.strftime("%d.%m.%Y")
        cfg = dict(settings)
        cfg["_credentials_file"] = user_path(name)
        scrapper._persist_current_date(cfg, new_str, au_out.get("code", ""),
                                       au_out.get("request_number", ""))
        persist_accounts()


async def _probe_cerere_job(url):
    """Un singur job async pentru validarea linkului (vezi _run_async)."""
    ctx = None
    try:
        ctx, _p = await scrapper.new_page()
        return await scrapper.probe_cerere(ctx, url)
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        if MONITOR.get("loop") is not None:
            # Aceeasi bucla ca monitorul - e destul sa eliberam Chromium.
            await scrapper.close_browser()
        else:
            # Bucla asta moare imediat dupa apel. Obiectele Playwright sunt
            # LEGATE de bucla pe care au fost create, iar _BROWSER["pw"] e
            # cache global: daca l-am lasa in viata, primul rebooking de mai
            # tarziu (alta bucla) ar primi un obiect mort. Oprim complet.
            await scrapper.shutdown_browser()


def _run_async(coro, timeout=300):
    """Ruleaza o corutina din firul HTTP (sincron).

    Daca monitorul merge, o trimitem pe bucla LUI - altfel am crea obiecte
    Playwright pe o bucla care moare imediat, iar cache-ul global _BROWSER
    le-ar servi mai tarziu monitorului (obiecte moarte -> rebooking crapat).
    """
    loop = MONITOR.get("loop")
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
    return asyncio.run(coro)


def _patch_settings(name, **changes):
    """Scrie direct in setarile contului, ocolind garda _rev (schimbarea vine
    de la server, nu de la un tab care ar putea fi vechi). Bump-uim _rev ca un
    tab deschis sa nu suprascrie inapoi rezultatul."""
    s = load_settings(name)
    s.update(changes)
    s["_rev"] = int(s.get("_rev", 0) or 0) + 1
    with open(user_path(name), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    persist_accounts()
    return s


async def _first_booking_user(name, settings):
    """Prima programare pentru UN cont: cerere noua, inca neprogramata.

    Nu foloseste scanul comun - o cerere e legata de o singura locatie si de
    propriul ei serviciu, deci intrebam direct calendarul ei (1 GET, fara
    browser). Chromium porneste doar cand chiar avem o zi de prins.
    """
    if settings.get("first_booking_result"):
        return  # deja programat - o cerere se programeaza o singura data
    url = (settings.get("first_booking_url") or "").strip()
    svc = (settings.get("first_booking_svc") or "").strip()
    loc = (settings.get("first_booking_loc") or "").strip()
    if not (url and svc and loc):
        ulog(name, "  [FB] Link de cerere neconfigurat sau nevalidat - skip.")
        return

    chat = tg_chat_of(name)

    async def send(text):
        await asyncio.to_thread(tg_send, chat, text)

    try:
        dates = await scrapper.fetch_cerere_dates(
            svc, loc, (settings.get("idnp") or "").strip())
    except Exception as e:
        ulog(name, f"  [FB] Nu am putut citi calendarul cererii: {e}")
        return

    months = [m.lower() for m in (settings.get("target_months") or [])]
    # sorted() explicit: vrem PRIMA zi libera, nu prima din ordinea in care
    # ne-a raspuns serverul (chiar daca azi raspunde sortat).
    wanted = sorted(d for d in dates
                    if not months or scrapper.RO_MONTHS_FULL[d.month - 1] in months)
    if not wanted:
        if dates:
            ulog(name, "  [FB] Zile libere doar in afara lunilor alese: "
                       + ", ".join(d.strftime("%d.%m.%Y") for d in dates))
        else:
            ulog(name, "  [FB] Nicio zi libera pentru cererea ta.")
        return

    target = wanted[0]
    ziua = target.strftime("%d.%m.%Y")
    ulog(name, f"  [FB] Zi libera gasita: {ziua}. Incerc programarea...")
    await send(f"🎯 <b>Loc liber gasit - incerc sa te programez</b>\nData: <b>{ziua}</b>")

    ok, info, ctx = False, {}, None
    try:
        ctx, _page = await scrapper.new_page()
        ok, info = await scrapper.first_time_booking(ctx, url, target, notify=send)
    except Exception as e:
        ulog(name, f"  [FB] Browserul nu a pornit: {e}")
        await send(f"❌ Prima programare: browserul nu a putut porni "
                   f"({_tg_escape(e)}). Reincerc la urmatoarea verificare.")
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        # Rezervarea e rara - eliberam cei ~200MB (Render free = 512MB).
        await scrapper.close_browser()

    if ok:
        rezumat = f"{ziua} {info.get('hour', '')}".strip()
        if info.get("code"):
            rezumat += f" (cod {info['code']})"
        _patch_settings(name,
                        first_booking_result=rezumat,
                        first_booking_enabled=False,
                        # Programarea proaspata devine tinta pentru auto-update,
                        # daca userul vrea sa o mute mai devreme mai tarziu.
                        current_appointment_date=ziua,
                        appointment_code=info.get("code", "")
                        or settings.get("appointment_code", ""),
                        request_number=info.get("request_number", "")
                        or settings.get("request_number", ""))
        ulog(name, f"  [FB] SUCCESS: {rezumat}")


async def _sleep_interruptible(seconds):
    """Somn intrerupt de stop (ultimul utilizator a oprit) sau de wake (un cont
    nou a pornit si asteapta prima verificare)."""
    for _ in range(max(1, int(seconds))):
        if MONITOR["stop"].is_set() or MONITOR["wake"].is_set():
            break
        await asyncio.sleep(1)
    MONITOR["wake"].clear()


async def _shared_loop():
    """Scaneaza locatiile urmarite o data pe ciclu si distribuie rezultatul."""
    print("=" * 60)
    print("  ASP Exam Checker - scan partajat (locatiile DECA urmarite)")
    print("=" * 60)
    # Firul HTTP are nevoie de bucla asta ca sa poata rula joburi Playwright
    # (validarea linkului de cerere) fara sa creeze obiecte pe alta bucla.
    MONITOR["loop"] = asyncio.get_running_loop()

    while not MONITOR["stop"].is_set():
        users = enabled_users()
        if not users:
            print("[i] Niciun utilizator activ - opresc scanul.")
            MONITOR["stop"].set()
            break

        # Cota zilnica arsa: pana la resetare (00:00 UTC) nicio cerere nu mai
        # intoarce nimic - si nici nu amana resetarea, dar ar umple Telegramul
        # cu "pene" inexistente. Deci dormim, pur si simplu.
        q = quota_state()
        if q.get("resume_at", 0) > time.time():
            left = int(q["resume_at"] - time.time())
            print(f"\n  [COTA] epuizata ({q.get('spent', 0)} citiri in fereastra "
                  f"asta) - reiau peste {left // 60} min, la 00:00 UTC.")
            await _sleep_interruptible(min(left + 5, 900))
            continue

        scan_idnp_v, scan_seria_v, scan_issue_v = scan_identity(users)
        # Doar locatiile bifate de conturile active: fiecare citire in plus taie
        # din numarul de scanari pe zi (vezi cota de mai sus).
        watched = []
        for _n in users:
            watched += list(load_settings(_n).get("scrape_locations") or [])
        raw, diagnostics = await scrapper.fetch_all_locations_dates(
            idnp=scan_idnp_v, seria=scan_seria_v, issue_date=scan_issue_v,
            only_locations=watched)
        # 2026-08-17: qmatic/dates (POST) cere identitatea completa (serie
        # buletin + data emiterii). Daca lipseste sau nu valideaza -> 403, ceea
        # ce e o eroare de CONFIGURARE a contului de scanare, nu o pana a
        # site-ului si nici un blocaj de IP - deci nu-l numaram ca esec (altfel
        # intram in backoff-ul gresit "protejam IP-ul"), doar anuntam o data
        # cauza. Vezi [[asp-qmatic-dates-gated-behind-wizard-2026-08-17]].
        identity_bad = diagnostics.get("identity_bad")
        # 2026-08-18: 429 = cota zilnica a IDNP-ului, nu o pana si nu ritmul
        # nostru (vezi quota_state + scrapper._RateLimited). Nu se numara ca
        # esec: altfel monitorul striga "NU POT SCANA SITE-UL" ore in sir
        # pentru o limita perfect normala - exact alertele din 18.08.2026.
        throttled = bool(diagnostics.get("rate_limited"))
        reads = int(diagnostics.get("calendar_reads") or 0)
        q["spent"] = int(q.get("spent", 0)) + reads
        if reads:
            q["reads_per_cycle"] = reads
        if throttled:
            # Cate citiri a acceptat ASP azi = bugetul real. Tinta ramane sub el,
            # ca maine sa nu mai lovim peretele.
            # ⚠️ Doar daca am apucat sa citim ceva in fereastra asta: la un
            # restart pe la mijlocul zilei contorul porneste de la 0 si primul
            # 429 ar "invata" un buget ridicol de mic, care apoi ar creste la
            # loc cu +50% pe zi. Fara citiri proprii nu stim nimic, deci nu
            # stricam cifra invatata.
            if q.get("spent", 0) > 0:
                q["budget"] = q["spent"]
                q["target"] = max(QUOTA_MIN_TARGET, int(q["budget"] * 0.85))
            q["exhausted"] = True
            wait_for = diagnostics.get("retry_after") or 0
            q["resume_at"] = (time.time() + wait_for + 60) if wait_for \
                else _quota_reset_at() + 60
            print(f"\n  [COTA] ASP a taiat calendarul dupa {q['spent']} citiri "
                  f"azi. Buget invatat: {q['budget']}, tinta de maine: "
                  f"{q['target']}. Reiau la 00:00 UTC.")
        _quota_save(q)
        scan_failed = (not identity_bad and not throttled
                       and (diagnostics["had_check_error"]
                            or diagnostics["locations_found"] == 0
                            or diagnostics["calendar_unavailable_count"] >= 2))
        MONITOR["fails"] = MONITOR["fails"] + 1 if scan_failed else 0
        if identity_bad and not MONITOR.get("identity_notified"):
            MONITOR["identity_notified"] = True
            why = diagnostics.get("error_text") or "identitate incompleta"
            print(f"\n  [i] Scanul nu poate citi calendarul: {why}")
            id_msg = (
                "⚠️ Monitorul nu poate citi calendarul ASP: " + why + ".\n"
                "Calendarul cere acum IDNP + seria buletinului + data emiterii "
                "(din 17.08.2026). Completeaza-le/corecteaza-le in setari, apoi "
                "reporneste monitorul.")
            for _n in users:
                notify_telegram(_n, id_msg)
        elif not identity_bad:
            MONITOR["identity_notified"] = False

        if throttled:
            # Un mesaj pe fereastra, informativ - nu alarma de pana. Textul
            # spune explicit ca NU e blocat nimic, ca sa nu para acelasi lucru
            # cu "MONITORUL NU POATE SCANA".
            if not q.get("notified"):
                q["notified"] = True
                _quota_save(q)
                when = datetime.fromtimestamp(q["resume_at"]).strftime("%H:%M")
                msg = ("⏸️ <b>Cota zilnica ASP e epuizata</b>\n"
                       f"Am citit calendarul de <b>{q['spent']}</b> ori azi - "
                       "ASP limiteaza cate citiri poate face un IDNP intr-o zi "
                       "(nu e o pana, nu e blocat nimic, site-ul merge).\n"
                       f"Reiau automat la <b>{when}</b>, cand se reseteaza.\n"
                       "⚠️ Pana atunci nu primesti notificari de locuri.\n"
                       "💡 Daca vrei sa ajunga pana seara, se poate rari scanul "
                       "(acum e la intervalul cerut de tine).")
                for _n in users:
                    notify_telegram(_n, msg)
            if not reads:
                # Nimic citit inainte de taiere => nimic de anuntat. Prima
                # programare are calendarul ei (alt serviciu), deci merge mai
                # departe chiar daca scanul comun a ramas fara cota.
                for _n in users:
                    _s = load_settings(_n)
                    if _s.get("first_booking_enabled"):
                        try:
                            await _first_booking_user(_n, _s)
                        except Exception as e:
                            print(f"[!] Eroare la prima programare ({_n}): {e}")
                await _sleep_interruptible(
                    min(max(60, int(q["resume_at"] - time.time())) + 5, 900))
                continue
            # Altfel am apucat sa citim o parte din locatii inainte de taiere:
            # zilele alea sunt reale si trebuie anuntate (si eventual prinse de
            # auto-update) ACUM, nu pierdute fiindca urmatoarea locatie a dat
            # 429. Deci mergem mai departe pe traseul normal si abia la final
            # dormim pana la resetare.

        intervals = []
        for name in users:
            settings = load_settings(name)
            try:
                intervals.append(max(1, int(settings.get("interval_minutes", 5) or 5)))
            except (TypeError, ValueError):
                intervals.append(5)
            if not tg_chat_of(name):
                ulog(name, "[!] Cont fara Telegram conectat - nu pot trimite notificari.")
                continue
            try:
                results, current_dt, au_mode, in_watched = await _notify_user(
                    name, settings, raw, scan_failed, diagnostics)
                if settings.get("auto_update_enabled") and results and not scan_failed:
                    await _auto_update_user(name, settings, results,
                                            current_dt, au_mode, in_watched)
                # Prima programare nu depinde de scanul comun: cererea are
                # propriul calendar (serviciu+locatie proprii), deci rulam si
                # cand scanul general a esuat.
                if settings.get("first_booking_enabled"):
                    await _first_booking_user(name, settings)
            except Exception as e:
                print(f"[!] Eroare la procesarea contului {name}: {e}")

        # Ritmul comun = cel mai nerabdator utilizator activ. Backoff: 3+ esecuri
        # la rand = probabil blocati de WAF/site cazut, verificarile dese doar
        # prelungesc blocarea.
        wait_minutes = min(intervals) if intervals else 5
        # Ritmul ramane cel cerut de utilizator (QUOTA_SPREAD=False). Cat mai
        # tine bugetul la ritmul asta ii spunem in log, ca sa nu fie surpriza
        # cand tace: e o cifra, nu o pana.
        per_cycle = max(1, int(q.get("reads_per_cycle") or 1))
        secs_left = max(60.0, _quota_reset_at() - time.time())
        if QUOTA_SPREAD:
            # Varianta "acoperire pana seara": citirile ramase, impartite
            # uniform pe cat a mai ramas din fereastra.
            reads_left = int(q.get("target") or QUOTA_START_TARGET) - int(q.get("spent", 0))
            if reads_left <= 0:
                q["resume_at"] = _quota_reset_at() + 60
                _quota_save(q)
                print(f"\n  [COTA] tinta de {q.get('target')} citiri s-a "
                      f"incheiat fara 429 - astept resetarea.")
                wait_minutes = 1
            else:
                paced = (secs_left / max(1.0, reads_left / per_cycle)) / 60.0
                if paced > wait_minutes:
                    print(f"\n  [COTA] {reads_left} citiri ramase pentru "
                          f"{secs_left / 3600:.1f}h -> scan la {paced:.0f} min.")
                    wait_minutes = paced
        elif q.get("budget"):
            burn = max(0, int(q["budget"]) - int(q.get("spent", 0)))
            hours = (burn / per_cycle) * wait_minutes / 60.0
            print(f"\n  [COTA] {q['spent']}/{q['budget']} citiri folosite azi - "
                  f"la {wait_minutes} min ajunge inca ~{hours:.1f}h.")
        if throttled:
            # Am anuntat ce am apucat sa citim; de aici incolo cota e arsa.
            wait_minutes = min(max(1.0, (q["resume_at"] - time.time()) / 60.0), 15)
        if MONITOR["fails"] >= 3:
            wait_minutes = max(wait_minutes, 15)
            print(f"\n  [BACKOFF] {MONITOR['fails']} scanari esuate la rand - "
                  f"astept {wait_minutes} minute (protejam IP-ul de blocare).")
        print(f"\n  Urmatoarea scanare in {wait_minutes:.0f} minute "
              f"({len(users)} utilizatori activi).")
        await _sleep_interruptible(wait_minutes * 60)

    await scrapper.shutdown_browser()


# ── HTTP ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        return user_from_token(self.headers.get("X-Auth", ""))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
            return
        if self.path == "/api/status":  # pt. keep-alive, fara date sensibile
            self._send(200, {"ok": True})
            return
        name = self._user()
        if not name:
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/api/config":
            bot = tg_bot_username()
            chat = tg_chat_of(name)
            self._send(200, {
                "config": load_settings(name), "locations": KNOWN_LOCATIONS,
                "running": is_running(name), "username": name,
                "shared_users": len(enabled_users()),
                "telegram": {
                    "configured": bool(TG_TOKEN),
                    "bot": bot,
                    "linked": bool(chat),
                    "link_url": (f"https://t.me/{bot}?start={tg_link_code(name)}"
                                 if bot and not chat else ""),
                    "code": "" if chat else tg_link_code(name),
                },
            })
        elif self.path.startswith("/api/logs"):
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    since = 0
            payload = user_log(name).since(since)
            payload["running"] = is_running(name)
            payload["shared_users"] = len(enabled_users())
            self._send(200, payload)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/api/login":
                name = str(body.get("username", "")).strip()
                with ACCOUNTS_LOCK:
                    u = ACCOUNTS["users"].get(name)
                if u and _check_pw(str(body.get("password", "")), u["pw"]):
                    self._send(200, {"ok": True, "token": token_for(name), "username": name})
                else:
                    self._send(200, {"ok": False, "message": "Utilizator sau parola gresita."})
                return

            if self.path == "/api/register":
                name = str(body.get("username", "")).strip()
                pw = str(body.get("password", ""))
                if REGISTER_CODE and str(body.get("code", "")).strip() != REGISTER_CODE:
                    self._send(200, {"ok": False, "message": "Cod de invitatie gresit."})
                    return
                if not USERNAME_RE.match(name):
                    self._send(200, {"ok": False, "message": "Utilizator invalid (3-20 caractere: litere, cifre, - _)."})
                    return
                if len(pw) < 4:
                    self._send(200, {"ok": False, "message": "Parola prea scurta (minim 4 caractere)."})
                    return
                ok, msg = create_account(name, pw)
                resp = {"ok": ok, "message": msg}
                if ok:
                    resp.update(token=token_for(name), username=name)
                self._send(200, resp)
                return

            name = self._user()
            if not name:
                self._send(401, {"error": "unauthorized"})
                return
            CONFLICT_MSG = ("Setarile de pe server s-au schimbat intre timp "
                            "(alt tab/dispozitiv sau auto-update). Reincarc "
                            "valorile actuale - verifica si salveaza din nou.")
            if self.path == "/api/config":
                s = save_settings(name, body)
                if s is None:
                    self._send(200, {"ok": False, "conflict": True, "message": CONFLICT_MSG})
                else:
                    self._send(200, {"ok": True, "_rev": s.get("_rev", 0)})
            elif self.path == "/api/first-booking/probe":
                # Valideaza linkul de cerere si afla serviciul+locatia lui, ca
                # monitorul sa poata apoi interoga calendarul fara browser.
                # Singurul loc unde pornim Chromium din interfata.
                raw = str(body.get("url", "")).strip()
                parsed = scrapper.parse_cerere_url(raw)
                if not parsed:
                    self._send(200, {"ok": False, "message":
                                     "Link invalid. Trebuie sa arate a "
                                     "https://eservicii.gov.md/asp/dimtcca/cerere/APO01/<cod>"})
                    return
                try:
                    found = _run_async(_probe_cerere_job(parsed["url"]))
                except Exception as e:
                    self._send(200, {"ok": False,
                                     "message": f"Nu am putut deschide cererea: {e}"})
                    return
                zile = [d.strftime("%d.%m.%Y") for d in found.get("dates", [])]
                _patch_settings(name,
                                first_booking_url=parsed["url"],
                                first_booking_svc=found["service_hash"],
                                first_booking_loc=found["location_id"],
                                first_booking_result="")
                self._send(200, {"ok": True, "dates": zile,
                                 "message": ("Link valid. Zile libere acum: "
                                             + (", ".join(zile) if zile
                                                else "niciuna (astept)."))})
            elif self.path == "/api/start":
                s = None
                if body:
                    s = save_settings(name, body)  # Start salveaza intai setarile
                    if s is None:
                        self._send(200, {"ok": False, "conflict": True,
                                         "message": CONFLICT_MSG,
                                         "running": is_running(name)})
                        return
                ok, msg = start_monitor(name)
                resp = {"ok": ok, "message": msg, "running": is_running(name)}
                if s is not None:
                    resp["_rev"] = s.get("_rev", 0)
                self._send(200, resp)
            elif self.path == "/api/stop":
                ok, msg = stop_monitor(name)
                self._send(200, {"ok": ok, "message": msg, "running": is_running(name)})
            elif self.path == "/api/telegram/unlink":
                chat = tg_chat_of(name)
                tg_set_chat(name, "")
                if chat:
                    tg_send(chat, "🔌 Chatul a fost deconectat din interfata web.")
                self._send(200, {"ok": True, "message": "Telegram deconectat."})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # nu crapa serverul pe un request prost
            self._send(500, {"error": str(e)})


def _keepalive_loop(url):
    """Self-ping ca serviciul free (Render) sa nu adoarma."""
    url = url.rstrip("/") + "/api/status"
    failures = 0
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=20).read(64)
            failures = 0
        except Exception as e:
            failures += 1
            print(f"[i] Keep-alive ping esuat ({failures} la rand): {e}")
            # 3 esecuri = ~30 min fara ping -> serviciul free urmeaza sa adoarma
            # si dupa aceea nu mai poate anunta nimic. Avertizeaza ACUM.
            if failures == 3:
                for n in enabled_users():
                    notify_telegram(
                        n,
                        "⚠️ <b>ASP Monitor</b>: keep-alive-ul a esuat de 3 ori la rand - "
                        "serviciul poate adormi in curand si verificarile s-ar opri "
                        "fara alt avertisment.")


def main():
    sys.stdout = ROUTER
    boot_accounts()

    cloud = "PORT" in os.environ
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    host = "0.0.0.0" if cloud else "127.0.0.1"
    print(f"ASP Exam Checker web UI pe {host}:{port} "
          f"({len(ACCOUNTS['users'])} conturi, scan partajat pentru toti)")

    if TG_TOKEN:
        threading.Thread(target=tg_poll_loop, daemon=True).start()
    else:
        print("[!] ATENTIE: ASP_TG_TOKEN lipseste - nimeni nu poate primi "
              "notificari Telegram. Creeaza un bot la @BotFather si pune "
              "token-ul in variabila de mediu ASP_TG_TOKEN.")

    keepalive_url = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if keepalive_url:
        threading.Thread(target=_keepalive_loop, args=(keepalive_url,), daemon=True).start()
        print(f"[i] Keep-alive activ: ping la fiecare 10 min pe {keepalive_url}")

    configured = bool(RENDER_API_KEY and RENDER_SERVICE_ID)
    if configured:
        _verify_persistence_target()   # poate dezactiva sync-ul (serviciu gresit)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        threading.Thread(target=_watch_settings, daemon=True).start()
        print("[i] Persistenta conturi activa (sync in Render env la orice schimbare)")
    elif configured:
        pass  # _verify_persistence_target a oprit sync-ul si a anuntat deja
    elif cloud:
        # Fara sync, discul efemer inseamna ca ORICE modificare (inclusiv data
        # programarii scrisa de auto-update) se pierde la restart - anunta tare.
        print("[!] ATENTIE: RENDER_API_KEY/RENDER_SERVICE_ID lipsesc - "
              "setarile NU supravietuiesc unui restart!")
        _notify_all("⚠️ <b>ASP Monitor</b>: salvarea persistenta a setarilor este "
                    "DEZACTIVATA (lipseste RENDER_API_KEY).\nOrice modificare se "
                    "pierde la urmatorul restart al serverului - seteaza cheia in "
                    "Render → Environment.")

    # Autostart: reia scanul partajat daca vreun cont il avea pornit inainte de
    # restart. Orice boot cu conturi "enabled" = a existat o pauza de
    # monitorizare (deploy, crash, spin-down) -> anunta pe Telegram, altfel
    # utilizatorii nu afla ca au ramas nemonitorizati.
    autostart = os.environ.get("ASP_AUTOSTART", "").strip().lower() in ("1", "true", "yes")
    wanted = enabled_users()

    # O reprogramare intrerupta de restart (tipic: OOM la pornirea Chromium) lasa
    # marcajul pus in _auto_update_user. Fara mesajul asta, singurul semn e
    # tacerea de dupa "Auto-update pornit".
    with ACCOUNTS_LOCK:
        interrupted = {n: (u.get("settings") or {}).get("au_in_progress", "")
                       for n, u in ACCOUNTS["users"].items()}
    for n, marker in interrupted.items():
        if not marker:
            continue
        _patch_settings(n, au_in_progress="")
        notify_telegram(
            n,
            f"⚠️ <b>Reprogramarea a fost INTRERUPTA</b> de un restart al serverului.\n"
            f"Incercam: <b>{_escape(marker)}</b>.\n"
            f"Programarea ta a ramas cea veche. Daca ziua mai e libera, "
            f"o reiau la urmatoarea verificare.")
    if wanted and autostart and TG_TOKEN:
        MONITOR["users"].clear()  # toata lumea primeste iar prima verificare
        ensure_monitor()
        print(f"[i] Autostart: scan partajat pornit pentru {len(wanted)} conturi.")
        for n in wanted:
            notify_telegram(
                n,
                "🔄 <b>Serverul ASP Monitor a repornit</b> (deploy/crash/spin-down) - "
                "verificarile au fost intrerupte intre timp.\n"
                "Monitorul a fost repornit automat.")
    elif wanted:
        cause = ("autostart dezactivat" if not autostart
                 else "botul Telegram nu e configurat (ASP_TG_TOKEN)")
        print(f"[!] {len(wanted)} conturi pornite dar scanul NU a repornit: {cause}")
        for n in wanted:
            notify_telegram(
                n,
                f"⚠️ <b>Serverul ASP Monitor a repornit</b> si monitorul "
                f"<b>NU ruleaza</b> ({cause}).\n"
                f"Verificarile au fost intrerupte - deschide site-ul si porneste-l.")

    if not cloud:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        print("Se deschide browserul... (Ctrl+C aici pentru oprire)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
