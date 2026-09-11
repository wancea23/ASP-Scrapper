"""
ASP Exam Checker - Auto-refresh la 15 minute + notificari Telegram
"""

import asyncio
import aiohttp
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Etichetele locatiilor contin diacritice (ș, ț, â). Pe o consola Windows
# (cp1252) un print cu diacritice arunca UnicodeEncodeError - si cand asta se
# intampla in mijlocul unei scanari, scanarea perfect valida e raportata ca
# ESUATA (si pleaca alerta "nu pot scana site-ul"). Afisarea nu are voie sa
# strice rezultatul.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

URL = "https://eservicii.gov.md/asp/dimtcca/cerere/apo01"
APPOINTMENTS_URL = "https://eservicii.gov.md/asp/dimtcca/APO/my-appointments"

# Global config variables - populated by run_with_config
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
INTERVAL_MINUTE = 5
FORM_DATA = {}

RO_MONTHS_SHORT = ["ian.", "feb.", "mar.", "apr.", "mai", "iun.",
                   "iul.", "aug.", "sept.", "oct.", "nov.", "dec."]
RO_MONTHS_FULL  = ["ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
                   "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie"]


async def send_telegram(message: str, token: str = None, chat_id: str = None):
    """Trimite mesaj pe Telegram.

    Fara argumente foloseste botul din config (mod standalone/GUI). Monitorul
    partajat din web.py trimite explicit token-ul botului comun + chat-ul
    fiecarui utilizator, deci nimeni nu mai are nevoie de token propriu.
    """
    token = token or TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID if chat_id is None else chat_id
    if not token or not chat_id or token == "PUNE_TOKEN_BOT_AICI":
        print("  [TG] Telegram neconfigurat - mesaj sarit")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    print("  [TG] Mesaj trimis pe Telegram!")
                else:
                    print(f"  [TG] Eroare trimitere: {resp.status}")
    except Exception as e:
        print(f"  [TG] Eroare conexiune Telegram: {e}")


# ── Chromium partajat (necesar DOAR pentru auto-update / fallback browser) ───
_BROWSER = {"pw": None, "browser": None}

# Headless Chromium se prezinta ca "HeadlessChrome" in User-Agent - exact
# ce filtreaza WAF-urile. Ne prezentam ca un Chrome normal + locale ro.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def is_headless() -> bool:
    """Cloud hosts (Render/Railway) seteaza PORT si nu au display -> headless.
    ASP_HEADLESS=1/0 forteaza explicit oricare varianta."""
    hl = os.environ.get("ASP_HEADLESS", "").strip().lower()
    if hl in ("1", "true", "yes"):
        return True
    if hl in ("0", "false", "no"):
        return False
    return "PORT" in os.environ


async def _ensure_browser():
    if _BROWSER["browser"] is not None and _BROWSER["browser"].is_connected():
        return _BROWSER["browser"]
    import sys
    if getattr(sys, "frozen", False):
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright"))
    if _BROWSER["pw"] is None:
        _BROWSER["pw"] = await async_playwright().start()
    # Flags de memorie redusa pentru hosturi cu RAM putin (Render free = 512MB);
    # /dev/shm e minuscul in containere -> fara el Chromium crapa aleator.
    #
    # 18.08.2026: Render a omorat instanta exact in timpul unei reprogramari
    # ("exceeded its memory limit"), fiindca pornirea lui Chromium plus boot-ul
    # paginii Blazor de la my-appointments cer impreuna ~670 MB masurati.
    # Masurat pe aceeasi pagina: flagurile vechi 670 MB, setul de mai jos fara
    # --single-process 659 MB, cu el 556 MB - deci procesul unic e singurul care
    # aduce o economie reala (~17%). Nu e o garantie ca incape mereu in 512 MB,
    # dar reface marja pe care au mancat-o versiunile mai noi de Chromium si
    # aplicatia Blazor tot mai grea.
    headless = is_headless()
    args = ["--no-sandbox"]
    if headless:
        args += ["--disable-dev-shm-usage", "--disable-gpu",
                 "--disable-extensions", "--no-zygote",
                 "--disable-features=site-per-process,IsolateOrigins,TranslateUI,"
                 "BackForwardCache",
                 "--renderer-process-limit=1", "--disable-background-networking",
                 "--disable-breakpad", "--disable-sync", "--metrics-recording-only",
                 "--mute-audio", "--disable-software-rasterizer",
                 "--js-flags=--max-old-space-size=128"]
    try:
        _BROWSER["browser"] = await _BROWSER["pw"].chromium.launch(
            headless=headless, args=args + (["--single-process"] if headless else []))
    except Exception as e:
        # --single-process e cel mai eficient, dar si cel mai fragil: daca
        # Chromium refuza sa porneasca asa, mai bine un browser gras care merge
        # decat niciunul - reprogramarea e oricum rara.
        if not headless:
            raise
        print(f"  [!] Chromium nu a pornit cu --single-process ({e}) - reincerc fara.")
        _BROWSER["browser"] = await _BROWSER["pw"].chromium.launch(
            headless=headless, args=args)
    return _BROWSER["browser"]


async def close_browser():
    """Chromium e pornit rar (rebooking) - il inchidem imediat dupa, ca sa nu
    tina ~200MB ocupati degeaba (Render free = 512MB)."""
    if _BROWSER["browser"] is not None:
        try:
            await _BROWSER["browser"].close()
        except Exception:
            pass
        _BROWSER["browser"] = None


async def shutdown_browser():
    await close_browser()
    if _BROWSER["pw"] is not None:
        try:
            await _BROWSER["pw"].stop()
        except Exception:
            pass
        _BROWSER["pw"] = None


async def _block_heavy(route):
    # Imagini/fonturi/media nu afecteaza calendarul (DOM pur) - le taiem
    # ca sa incapa Chromium in 512MB (Render free da OOM kill altfel).
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


async def new_page():
    """Context + pagina noua. 1280x900 si pe headless: la 1024x768 site-ul gov
    trece pe layout compact - tabelul din my-appointments devine duplicatul
    ASCUNS si butonul text MODIFICA dispare => auto-update pica pe Render."""
    headless = is_headless()
    kwargs = {"viewport": {"width": 1280, "height": 900}, "locale": "ro-RO"}
    if headless:
        kwargs["user_agent"] = _BROWSER_UA
    browser = await _ensure_browser()
    ctx = await browser.new_context(**kwargs)
    if headless:
        await ctx.route("**/*", _block_heavy)
    return ctx, await ctx.new_page()


async def select_date_in_picker(page, target_day, target_month, target_year):
    await page.wait_for_timeout(600)
    year_btn = page.locator(".fod-picker-paper button.fod-button-year").first
    await year_btn.wait_for(timeout=5000)
    await year_btn.click()
    await page.wait_for_timeout(600)

    found_year = False
    for selector in [
        f".fod-picker-paper li:has-text('{target_year}')",
        f".fod-picker-paper .fod-picker-year:has-text('{target_year}')",
        f".fod-picker-paper button:has-text('{target_year}')",
    ]:
        el = page.locator(selector).first
        if await el.is_visible(timeout=1000):
            await el.click()
            found_year = True
            break

    if not found_year:
        for scroll_sel in [".fod-picker-paper .fod-picker-years", ".fod-picker-paper ul", ".fod-picker-paper .fod-paper"]:
            container = page.locator(scroll_sel).first
            if await container.is_visible(timeout=1000):
                await container.evaluate("el => { el.scrollTop = 0; }")
                await page.wait_for_timeout(400)
                break
        for selector in [
            f".fod-picker-paper li:has-text('{target_year}')",
            f".fod-picker-paper .fod-picker-year:has-text('{target_year}')",
        ]:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                await el.click()
                found_year = True
                break
        if not found_year:
            all_els = await page.locator(".fod-picker-paper *").all()
            for el in all_els:
                try:
                    txt = (await el.inner_text(timeout=300)).strip()
                    if txt == str(target_year):
                        await el.click()
                        found_year = True
                        break
                except Exception:
                    continue

    await page.wait_for_timeout(500)
    month_short = RO_MONTHS_SHORT[target_month - 1]
    month_full  = RO_MONTHS_FULL[target_month - 1]
    found_month = False

    for selector in [
        f".fod-picker-paper button:has-text('{month_short}')",
        f".fod-picker-paper button:has-text('{month_full}')",
        f".fod-picker-paper .fod-picker-month:has-text('{month_short}')",
        f".fod-picker-paper .fod-picker-month:has-text('{month_full}')",
    ]:
        el = page.locator(selector).first
        if await el.is_visible(timeout=1500):
            await el.click()
            found_month = True
            break

    if not found_month:
        all_month_btns = await page.locator(".fod-picker-paper .fod-picker-month").all()
        if len(all_month_btns) >= target_month:
            await all_month_btns[target_month - 1].click()

    await page.wait_for_timeout(500)

    day_btns = await page.locator(
        ".fod-picker-paper .fod-picker-calendar button.fod-picker-calendar-day:not([disabled])"
    ).all()
    for btn in day_btns:
        cls    = await btn.get_attribute("class") or ""
        aria   = (await btn.get_attribute("aria-label") or "").lower()
        p_text = (await btn.locator("p").inner_text()).strip()
        if "fod-hidden" in cls:
            continue
        if p_text == str(target_day) and month_full in aria:
            await btn.click()
            return True
    for btn in day_btns:
        cls    = await btn.get_attribute("class") or ""
        p_text = (await btn.locator("p").inner_text()).strip()
        if "fod-hidden" in cls:
            continue
        if p_text == str(target_day):
            await btn.click()
            return True
    return False


async def navigate_to_month(cal, month_name, page):
    for _ in range(24):
        header = cal.locator(".fod-picker-calendar-header-transition p")
        if not await header.is_visible(timeout=3000):
            break
        header_text = (await header.inner_text()).lower()
        if month_name in header_text and "2026" in header_text:
            return True
        cur_idx   = next((i for i, m in enumerate(RO_MONTHS_FULL) if m in header_text), -1)
        month_idx = RO_MONTHS_FULL.index(month_name) + 1
        if cur_idx + 1 < month_idx:
            await cal.locator("button[aria-label*='next month']").click()
        else:
            await cal.locator("button[aria-label*='previous month']").click()
        await page.wait_for_timeout(350)
    return False


async def collect_days(cal, month_name):
    all_btns  = await cal.locator(".fod-picker-calendar button.fod-picker-calendar-day").all()
    available = []
    for btn in all_btns:
        is_disabled = await btn.get_attribute("disabled")
        classes     = await btn.get_attribute("class") or ""
        if is_disabled is not None or "fod-hidden" in classes:
            continue
        aria = await btn.get_attribute("aria-label") or ""
        if month_name in aria.lower() and "2026" in aria:
            day_text = (await btn.locator("p").inner_text()).strip()
            available.append(f"{day_text} {month_name} 2026")
    return available


def build_warmup_months(target_months):
    """Include one extra month after the highest configured target month for calendar warmup."""
    normalized = []
    for month in target_months:
        m = str(month or "").strip().lower()
        if m in RO_MONTHS_FULL and m not in normalized:
            normalized.append(m)

    if not normalized:
        return []

    highest_idx = max(RO_MONTHS_FULL.index(m) for m in normalized)
    if highest_idx < len(RO_MONTHS_FULL) - 1:
        extra_month = RO_MONTHS_FULL[highest_idx + 1]
        if extra_month not in normalized:
            normalized.append(extra_month)

    return normalized


async def extract_with_warmup(page, retries=1):
    cal          = page.locator(".fod-picker-static .fod-picker-calendar-content").first
    target_months = [str(m).lower() for m in FORM_DATA["target_months"]]
    warmup_months = build_warmup_months(target_months)
    best_results = {m: [] for m in target_months}

    for attempt in range(1, retries + 1):
        for m in warmup_months:
            await navigate_to_month(cal, m, page)
            await page.wait_for_timeout(500)
        for m in reversed(warmup_months):
            await navigate_to_month(cal, m, page)
            await page.wait_for_timeout(500)
        for m in target_months:
            await navigate_to_month(cal, m, page)
            await page.wait_for_timeout(700)
            days = await collect_days(cal, m)
            if len(days) > len(best_results[m]):
                best_results[m] = days
        if all(len(best_results[m]) > 0 for m in target_months):
            break

    return best_results


async def force_select_location(page, location: dict):
    """Selecteaza o locatie folosind metoda nativa Playwright (driveste corect componenta Angular)."""
    try:
        await page.select_option('select[name="ExaminationLocation"]', value=location["value"])
        return
    except Exception:
        pass
    try:
        await page.select_option('select[name="ExaminationLocation"]', label=location["label"])
    except Exception:
        pass


async def _calendar_signature(page):
    """Returneaza o semnatura a calendarului curent (None daca lipseste)."""
    try:
        return await page.evaluate("""() => {
            const picker = document.querySelector('.fod-picker-static');
            if (!picker) return null;
            const html = picker.innerHTML;
            return html.length + '|' + html.slice(0, 300) + '|' + html.slice(-300);
        }""")
    except Exception:
        return None


async def _calendar_has_any_days(page, extra_months: int = 2) -> bool:
    """Verifica luna curenta + extra_months inainte, apoi revine la luna initiala.

    Scaneaza current, current+1, current+2 (implicit). Daca vreo luna are zile
    clickabile => calendarul e incarcat. Revine mereu la pozitia de start.
    """
    try:
        cal = page.locator(".fod-picker-static .fod-picker-calendar-content").first
        if not await cal.is_visible(timeout=3000):
            return False
    except Exception:
        return False

    months_advanced = 0
    found = False

    for i in range(extra_months + 1):  # current + extra_months
        try:
            day_count = await cal.locator(
                ".fod-picker-calendar button.fod-picker-calendar-day:not([disabled]):not(.fod-hidden)"
            ).count()
            if day_count > 0:
                found = True
                break
        except Exception:
            pass
        if i < extra_months:
            try:
                next_btn = cal.locator("button[aria-label*='next month']")
                if not await next_btn.is_visible(timeout=1000):
                    break
                await next_btn.click()
                await page.wait_for_timeout(300)
                months_advanced += 1
            except Exception:
                break

    # Revino la luna initiala
    for _ in range(months_advanced):
        try:
            prev_btn = cal.locator("button[aria-label*='previous month']")
            if not await prev_btn.is_visible(timeout=1000):
                break
            await prev_btn.click()
            await page.wait_for_timeout(300)
        except Exception:
            break

    return found


async def try_select_and_detect_reload(page, location: dict, verbose: bool = True) -> bool:
    """Selecteaza o locatie si verifica daca calendarul s-a incarcat cu adevarat.

    Strategie: (1) observa schimbarea semnaturii HTML (fast-path ~5s), apoi
    (2) confirma cu scan semantic - daca vreo luna din 6 inainte are zile clickabile,
    data e incarcata de pe server. Daca nicio luna nu are zile, consideram ca nu
    s-a reincarcat (stale) si lasam apelantul sa comute locatii.
    """
    before_sig = await _calendar_signature(page)

    await force_select_location(page, location)

    sig_changed = False
    # Poll 5 seconds pentru schimbare rapida de semnatura
    for _ in range(25):  # 25 * 200ms = 5s
        await page.wait_for_timeout(200)
        try:
            after_sig = await _calendar_signature(page)
        except Exception:
            # JS context resetat = navigare completa
            if verbose:
                print("    [OK] Pagina s-a reincarcat (navigare completa)!")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_selector(".fod-picker-static", timeout=10000)
            except Exception:
                await page.wait_for_timeout(2000)
            return True

        if before_sig is None and after_sig is not None:
            sig_changed = True
            break
        if before_sig is not None and after_sig is not None and after_sig != before_sig:
            sig_changed = True
            break

    # Settle
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        await page.wait_for_timeout(500)

    # Confirmare semantica: luna curenta + 2 inainte, apoi revenire
    has_days = await _calendar_has_any_days(page, extra_months=2)

    if has_days:
        if verbose:
            if sig_changed:
                print("    [OK] Calendar incarcat (semnatura schimbata + zile gasite)")
            else:
                print("    [OK] Calendar incarcat (zile gasite la scanare)")
        return True

    if verbose:
        if sig_changed:
            print("    [!] Semnatura schimbata dar nicio zi in 6 luni - probabil stale")
        else:
            print("    [!] Calendar neschimbat si nicio zi gasita")
    return False


async def select_location_with_reload_check(
    page, target: dict, all_locations: list, max_attempts: int = 8
) -> bool:
    """Comuta intre locatii pana cand locatia dorita se incarca cu re-render real."""
    alternate = next((loc for loc in all_locations if loc["value"] != target["value"]), None)

    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and alternate:
            print(f"    [~] Comut la alta locatie pentru a forta re-render ({alternate['label'][:30]}...)")
            await try_select_and_detect_reload(page, alternate, verbose=False)

        flashed = await try_select_and_detect_reload(page, target)
        if flashed:
            return True

        print(f"    [!] Pagina nu s-a reincarcat (incercarea {attempt}/{max_attempts})")

    return False


def _normalize_loc(s: str) -> str:
    """Lowercase + scoate diacriticele + pastreaza doar alfanumerice.

    Permite potrivirea robusta a etichetelor de locatie chiar daca diacriticele
    (ș/ş, â/î) sau punctuatia/spatiile difera intre dropdown si eticheta de pe site.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _parse_day(day_str: str, month: str):
    """Parse a calendar day label ('15 iunie 2026') + month name → datetime, or None."""
    mi = RO_MONTHS_FULL.index(month.lower()) + 1 if month.lower() in RO_MONTHS_FULL else None
    if not mi:
        return None
    parts = (day_str or "").split()
    if len(parts) < 3:
        return None
    try:
        return datetime(int(parts[2]), mi, int(parts[0]))  # datetime(year, month, day)
    except Exception:
        return None


def _earlier_than(days, month, current_dt):
    """Pastreaza doar zilele mai devreme decat current_dt (programarea curenta).
    Daca current_dt e None (auto-update oprit sau data curenta lipseste/invalida),
    pastreaza toate zilele. Zilele care nu pot fi parsate sunt pastrate, ca sa nu
    ascundem din greseala un slot posibil mai devreme."""
    if not current_dt:
        return list(days)
    kept = []
    for day_str in days:
        dt = _parse_day(day_str, month)
        if dt is None or dt < current_dt:
            kept.append(day_str)
    return kept


def find_earliest_available(results: dict, target_loc_substring: str):
    """Cea mai devreme zi libera: (eticheta locatiei, datetime) sau None.

    `target_loc_substring` gol = ORICARE dintre locatiile primite (care sunt
    deja doar cele bifate de utilizator). Inainte, gol insemna "nu returna
    nimic", adica auto-update pornit + camp necompletat = nu se intampla nimic,
    in tacere. Si invers, un camp completat il leaga de o singura locatie: pe
    18.08.2026 erau libere 22.08 si 02.09 la Radautanu/Calea Iesilor, in timp ce
    tinta era Salcamilor - deci nu s-ar fi luat niciodata, desi utilizatorul
    voia orice zi mai devreme.
    """
    sub = _normalize_loc(target_loc_substring) if target_loc_substring else ""
    earliest = None
    for loc, months in results.items():
        if sub and sub not in _normalize_loc(loc):
            continue
        for month, days in months.items():
            mi = RO_MONTHS_FULL.index(month.lower()) + 1 if month.lower() in RO_MONTHS_FULL else None
            if not mi or not days:
                continue
            for day_str in days:
                parts = day_str.split()
                if len(parts) < 3:
                    continue
                try:
                    d = int(parts[0])
                    y = int(parts[2])
                    dt = datetime(y, mi, d)
                    if earliest is None or dt < earliest[1]:
                        earliest = (loc, dt)
                except Exception:
                    continue
    return earliest


def _persist_current_date(config: dict, new_date: str, code: str = "",
                          request_number: str = ""):
    """Scrie noua data a programarii inapoi in credentials.json, altfel la
    restart comparatia se face cu o data veche si auto-update poate alege
    o zi mai proasta decat programarea reala.

    Codul programarii si numarul cererii se schimba si ele la fiecare
    reprogramare, iar perechea veche moare (cautarea intoarce "[]"), deci le
    salvam in acelasi loc cand le avem - altfel a doua mutare automata nu mai
    are cu ce sa gaseasca programarea.
    """
    path = config.get("_credentials_file") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["current_appointment_date"] = new_date
        if code:
            data["appointment_code"] = code
        if request_number:
            data["request_number"] = request_number
        # _rev = garda anti-tab-vechi din web UI: bump ca un tab incarcat
        # inainte de rebook sa nu poata suprascrie data noua cu cea veche
        data["_rev"] = int(data.get("_rev", 0) or 0) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  [AU] Data programarii salvata in {os.path.basename(path)}: {new_date}")
    except Exception as e:
        print(f"  [AU] Nu am putut salva noua data in credentials.json: {e}")


def parse_current_appointment_date(date_str: str):
    """Parse 'dd.mm.yyyy' → datetime, or None."""
    if not date_str:
        return None
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", date_str)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except Exception:
        return None


async def _calendar_has_clickable_day(page) -> bool:
    """Check if the OPEN picker dialog has any clickable day."""
    try:
        cnt = await page.locator(
            ".fod-picker-paper button.fod-picker-calendar-day:not([disabled]):not(.fod-hidden)"
        ).count()
        return cnt > 0
    except Exception:
        return False


async def _pick_date_in_popup(page, target_dt: datetime) -> bool:
    """Navigate the OPEN popup picker to target_dt's month and click the day.

    The popup opens on the CURRENT month (often fully greyed out), so the
    target month must be reached with the next/previous-month arrows —
    the year/month-grid navigation from select_date_in_picker gets stuck
    on this popup. Day buttons carry aria-labels like
    'miercuri, 08 iulie 2026' (day zero-padded).
    """
    paper = page.locator(".fod-picker-paper")
    month_full = RO_MONTHS_FULL[target_dt.month - 1]

    in_target_month = False
    for _ in range(18):
        try:
            hdr = (await paper.locator(".fod-picker-calendar-header-transition p")
                   .first.inner_text(timeout=3000)).strip().lower()
        except Exception:
            return False
        if month_full in hdr and str(target_dt.year) in hdr:
            in_target_month = True
            break
        cur_month = next((i for i, m in enumerate(RO_MONTHS_FULL) if m in hdr), -1)
        ym = re.search(r"\d{4}", hdr)
        if cur_month < 0 or not ym:
            return False
        if (int(ym.group(0)), cur_month) < (target_dt.year, target_dt.month - 1):
            arrow = paper.locator("button[aria-label*='next month']").first
        else:
            arrow = paper.locator("button[aria-label*='previous month']").first
        try:
            await arrow.click()
        except Exception:
            return False
        await page.wait_for_timeout(400)
    if not in_target_month:
        return False

    label = f"{target_dt.day:02d} {month_full} {target_dt.year}"
    day_btn = paper.locator(
        f"button.fod-picker-calendar-day:not([disabled]):not(.fod-hidden)[aria-label*='{label}']"
    ).first
    try:
        if await day_btn.is_visible(timeout=2000):
            await day_btn.click()
            return True
    except Exception:
        pass
    return False


async def auto_update_appointment(context, config: dict, target_dt: datetime,
                                  notify=None, out: dict = None) -> bool:
    """Open my-appointments, search, modify, pick the earliest date and time.

    Flow per user spec:
      1. goto /APO/my-appointments
      2. fill IDNP, appointment_code, request_number, click Cautare
      3. on error → TG alert, reload, up to 3 attempts total
      4. on success → click Modifica → click DA
      5. open date picker, navigate to target month, pick the day;
         on failure reload page up to 10 times
      6. pick earliest Ora; click Programeaza-te — after the button the site
         POSTs validate-appointment then qmatic/update (the real commit);
         no further manual steps exist, so wait for qmatic/update as proof
    """
    # 'notify' = unde pleaca mesajele de progres. In monitorul partajat fiecare
    # utilizator are propriul chat, deci apelantul injecteaza o functie legata
    # de contul lui; fara ea folosim botul global (mod standalone/GUI).
    send = notify or send_telegram

    # IDNP-ul pentru reprogramare poate diferi de cel folosit la scraping.
    # Daca 'auto_update_idnp' e gol, folosim IDNP-ul de scraping.
    idnp = (config.get("auto_update_idnp") or "").strip() or (config.get("idnp") or "").strip()
    code = (config.get("appointment_code") or "").strip()
    req  = (config.get("request_number") or "").strip()

    if not (idnp and code and req):
        # ⛔ Ramura asta iesea in TACERE: dupa "Auto-update pornit" nu mai venea
        # niciun mesaj, exact ca la un proces omorat de OOM - imposibil de
        # deosebit de pe Telegram (18.08.2026). Orice iesire spune de ce.
        lipsa = ", ".join(n for n, v in (("IDNP", idnp), ("cod programare", code),
                                         ("numar cerere", req)) if not v)
        print(f"  [AU] Date auto-update incomplete ({lipsa}) - skip")
        await send(f"❌ <b>Auto-update esuat</b>: lipseste {lipsa} din setari.\n"
                   f"⚠️ Codul programarii si numarul cererii se SCHIMBA la fiecare "
                   f"reprogramare - completeaza-le in interfata web, altfel nu pot "
                   f"muta programarea.")
        return False
    if (config.get("auto_update_idnp") or "").strip():
        print(f"  [AU] Folosesc IDNP separat pentru programare: {idnp}")

    page = await context.new_page()
    try:
        # ── Step 1-3: search with up to 3 attempts ─────────────────
        search_ok = False
        for attempt in range(1, 4):
            print(f"  [AU] Cautare programare (incercarea {attempt}/3)...")
            await page.goto(APPOINTMENTS_URL, wait_until="networkidle", timeout=60000)
            # Acelasi boot Blazor WASM lent ca la run_single_check: networkidle
            # trece cat timp runtime-ul inca porneste - asteptam UI-ul real.
            try:
                await page.wait_for_selector("input:visible", timeout=180000)
            except PlaywrightTimeout:
                print("  [AU] UI my-appointments neincarcat (boot lent/blocat)")
                continue
            await page.wait_for_timeout(800)
            inputs = page.locator('input[type="text"]:visible, input:not([type]):visible')
            n = await inputs.count()
            if n < 3:
                # Fallback - any visible input
                inputs = page.locator('input:visible')
                n = await inputs.count()
            if n < 3:
                print(f"  [AU] Nu am gasit campurile de input (n={n})")
                continue
            await inputs.nth(0).fill(idnp)
            await inputs.nth(1).fill(code)
            await inputs.nth(2).fill(req)
            await page.wait_for_timeout(300)

            # Click Cautare
            try:
                await page.locator("button", has_text=re.compile(r"C[ĂA]UTARE", re.IGNORECASE)).first.click()
            except Exception as e:
                print(f"  [AU] Nu am putut apasa Cautare: {e}")
                continue

            await page.wait_for_timeout(2500)

            # Check for error snackbar
            try:
                err_loc = page.locator("text=/Verifica.i corectitudinea/i").first
                if await err_loc.is_visible(timeout=1500):
                    print(f"  [AU] Eroare cautare (incercarea {attempt}/3)")
                    await send(
                        f"⚠️ <b>Auto-update: eroare la cautare</b> (incercarea {attempt}/3)\n"
                        f"Verificați corectitudinea datelor introduse."
                    )
                    continue
            except Exception:
                pass

            # Check that table appeared
            try:
                await page.wait_for_selector("table tbody tr", timeout=5000)
                search_ok = True
                break
            except Exception:
                print(f"  [AU] Tabel rezultate neafisat (incercarea {attempt}/3)")
                continue

        if not search_ok:
            await send("❌ <b>Auto-update esuat</b>: cautarea nu a reusit dupa 3 incercari.")
            return False

        # ── Step 4: Modifica → DA ──────────────────────────────────
        try:
            await page.locator("button", has_text=re.compile(r"MODIFIC[ĂA]", re.IGNORECASE)).first.click()
        except Exception as e:
            print(f"  [AU] Nu am putut apasa Modifica: {e}")
            await send("❌ Auto-update: butonul Modifica nu a fost gasit.")
            return False
        await page.wait_for_timeout(800)
        try:
            await page.locator("button", has_text=re.compile(r"^\s*DA\s*$", re.IGNORECASE)).first.click()
        except Exception as e:
            # A doua iesire tacuta (vezi nota de mai sus) - acum anunta.
            print(f"  [AU] Nu am putut apasa DA: {e}")
            await send("❌ Auto-update: confirmarea (butonul DA) nu a putut fi apasata.")
            return False

        try:
            await page.wait_for_url("**/cerere/**", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        # ── Step 5: open calendar, retry reload up to 10x if empty ─
        date_picked = False
        for attempt in range(1, 11):
            try:
                await page.click('button[aria-label="Open Date Picker"]', timeout=10000)
            except Exception as e:
                print(f"  [AU] Nu am putut deschide picker (incercarea {attempt}/10): {e}")
                await page.reload(wait_until="networkidle")
                await page.wait_for_timeout(1500)
                continue
            await page.wait_for_timeout(1200)

            if await _pick_date_in_popup(page, target_dt):
                date_picked = True
                break
            print(f"  [AU] Data {target_dt.strftime('%d.%m.%Y')} nu a putut fi selectata (incercarea {attempt}/10), reload...")

            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(2000)

        if not date_picked:
            await send(
                f"❌ <b>Auto-update esuat</b>: data {target_dt.strftime('%d.%m.%Y')} "
                f"nu mai e disponibila (10 incercari)."
            )
            return False

        await page.wait_for_timeout(2000)

        # ── Step 6: pick earliest Ora ──────────────────────────────
        picked_hour = None
        try:
            ora_select = page.locator('select[name*="Time"], select[name*="Hour"], select[name*="Ora"]').first
            if not await ora_select.is_visible(timeout=2000):
                ora_select = page.locator('select:visible').last
            options = await ora_select.locator("option").all()
            for opt in options:
                val = await opt.get_attribute("value") or ""
                txt = (await opt.inner_text()).strip()
                if not val or not txt:
                    continue
                if "selecta" in txt.lower():
                    continue
                await ora_select.select_option(value=val)
                picked_hour = txt
                break
            await page.wait_for_timeout(500)
        except Exception as e:
            print(f"  [AU] Avertisment selectare Ora: {e}")

        # Click Programeaza-te si asteapta commit-ul real (qmatic/update).
        # Dupa acest buton nu mai exista pasi manuali pe site.
        try:
            async with page.expect_response(
                lambda r: "qmatic/update" in r.url, timeout=30000
            ) as resp_info:
                await page.locator("button", has_text=re.compile(r"PROGRAMEAZ", re.IGNORECASE)).first.click()
            update_resp = await resp_info.value
        except PlaywrightTimeout:
            print("  [AU] qmatic/update nu a fost trimis - probabil validarea a esuat")
            await send("❌ <b>Auto-update esuat</b>: site-ul nu a confirmat reprogramarea (validare esuata).")
            return False
        except Exception as e:
            print(f"  [AU] Nu am putut apasa Programeaza-te: {e}")
            await send("❌ Auto-update: butonul Programeaza-te nu a fost gasit.")
            return False
        if not update_resp.ok:
            print(f"  [AU] qmatic/update a esuat: HTTP {update_resp.status}")
            await send(f"❌ <b>Auto-update esuat</b>: serverul a raspuns HTTP {update_resp.status} la reprogramare.")
            return False
        await page.wait_for_timeout(2000)

        # ⚠️ O reprogramare emite un COD si un NUMAR DE CERERE NOI, iar cele
        # vechi devin moarte: cautarea din my-appointments intoarce "[]" si
        # urmatorul auto-update nu mai are cu ce sa caute programarea. Pana
        # acum se salva doar data, deci perechea trebuia rescrisa de mana dupa
        # fiecare mutare. Le citim de pe pagina de finalizare (acelasi format
        # ca la prima programare) si le dam mai departe apelantului ca sa le
        # persiste.
        if out is not None:
            out["date"] = target_dt.strftime("%d.%m.%Y")
            if picked_hour:
                out["hour"] = picked_hour
            try:
                body = await page.inner_text("body")
                m = re.search(r"Codul program[ăa]rii\s*([A-Z0-9]{10,})", body)
                if m:
                    out["code"] = m.group(1)
                m = re.search(r"Num[ăa]rul cererii\s*([0-9]{6,})", body)
                if m:
                    out["request_number"] = m.group(1)
            except Exception as e:
                print(f"  [AU] Nu am putut citi codul/numarul nou: {e}")
            if not out.get("code") or not out.get("request_number"):
                print("  [AU] ATENTIE: codul/numarul nou nu au fost gasite pe pagina.")

        msg = (
            f"✅ <b>Programare actualizata automat!</b>\n"
            f"Noua data: <b>{target_dt.strftime('%d.%m.%Y')}</b>"
        )
        if picked_hour:
            msg += f"\nOra: <b>{picked_hour}</b>"
        if out and out.get("code"):
            msg += f"\nCod programare: <code>{out['code']}</code>"
        if out and out.get("request_number"):
            msg += f"\nNr. cererii: <code>{out['request_number']}</code>"
        if out and (out.get("code") or out.get("request_number")):
            msg += "\n<i>(salvate automat in setari)</i>"
        else:
            msg += ("\n⚠️ Nu am putut citi codul/numarul nou de pe pagina - "
                    "ia-le din emailul ASP si pune-le in setari, altfel "
                    "urmatoarea reprogramare nu va gasi programarea.")
        await send(msg)
        print(f"  [AU] SUCCESS: programare mutata pe {target_dt.strftime('%d.%m.%Y')} {picked_hour or ''}")
        return True
    except Exception as e:
        print(f"  [AU] Eroare neasteptata: {e}")
        await send(f"❌ Auto-update eroare neasteptata: {e}")
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  REPROGRAMARE FARA BROWSER (calea rapida)
# ═════════════════════════════════════════════════════════════════════════════
# De ce exista: pe 18.08.2026 Render a omorat instanta ("exceeded its memory
# limit") exact in timpul unei reprogramari, fiindca Chromium plus pagina Blazor
# cer ~670 MB masurati pe o instanta de 512 MB. Fluxul de mai jos face acelasi
# lucru din 6 apeluri HTTP, deci nu mai depinde deloc de RAM.
#
# Harta rutelor, verificata live pe 18.08.2026 (sondare read-only + capturarea
# requesturilor reale ale paginii, cu scrierile blocate):
#   1. POST apo-request/get-appointment {IDNP, RequestNumber, SgariNumber}
#        ⚠️ RequestNumber = codul APO…, SgariNumber = numarul cererii (INVERS
#        fata de cum suna). Raspunde cu programarea: id (GUID), data, ora,
#        canBeModified.
#   2. GET  fod/request/APO01/<guid>  -> cererea INTREAGA (~2.7 KB). De aici vin
#        campurile pe care nu le poti inventa: appointmentPublicId, type.id,
#        cost, receptionMode.
#   3. POST qmatic/times {PublicServiceId, PublicLocationId, Date, RequestId,
#        Idnp, SeriaAndNumber, IssueDate} -> orele libere.
#        ⛔ Fara RequestId raspunde 404, iar forma veche GET .../times/... a
#        disparut (intoarce index.html-ul SPA-ului).
#   4. schimbam in obiect data + ora (+ locatia, daca ziua e la alta locatie)
#   5. POST apo-request/validate-appointment <obiect> -> {isValid, validations}
#   6. POST qmatic/update <obiect>  = COMMIT-UL. Dupa el nu mai exista pasi.
#   7. GET  fod/request/APO01/<guid> din nou -> codul si numarul NOI (GUID-ul
#        ramane acelasi, deci recitirea merge chiar daca perechea s-a schimbat).
_APPT_TYPE_CODE = "APO01"


def _rb_issue_date(config: dict) -> str:
    """Data emiterii buletinului din config, in ambele forme in care apare:
    setarile din interfata web au id_date_day/month/year, iar FORM_DATA are
    id_date={'day':..,'month':..,'year':..}."""
    d = config.get("id_date")
    if isinstance(d, dict):
        return iso_issue_date(d.get("day"), d.get("month"), d.get("year"))
    return iso_issue_date(config.get("id_date_day"), config.get("id_date_month"),
                          config.get("id_date_year"))


def _rb_escape(text) -> str:
    """Mesajele pleaca pe Telegram cu parse_mode=HTML - un '<' din raspunsul
    ASP ar face botul sa refuze tot mesajul."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


async def _rb_get(session, path):
    async with session.get(f"{_api_base()}/{path}") as resp:
        text = await resp.text()
        if resp.status != 200 or text.lstrip().startswith("<!DOCTYPE"):
            raise RuntimeError(f"GET {path} -> {resp.status} {text[:120]!r}")
        return json.loads(text)


async def _rb_post(session, path, payload):
    async with session.post(f"{_api_base()}/{path}", json=payload) as resp:
        text = await resp.text()
        if resp.status != 200 or text.lstrip().startswith("<!DOCTYPE"):
            raise RuntimeError(f"POST {path} -> {resp.status} {text[:200]!r}")
        return json.loads(text) if text.strip() else None


def _rb_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT00:00:00")


async def api_rebook_appointment(config: dict, target_dt: datetime,
                                 target_location: str = "", notify=None,
                                 out: dict = None, dry_run: bool = False) -> bool:
    """Muta programarea pe target_dt fara sa porneasca vreun browser.

    Returneaza True doar dupa ce qmatic/update a raspuns 200 SI recitirea
    cererii confirma noua data - un "am trimis requestul" nu e o dovada.

    dry_run=True face toti pasii de CITIRE (cauta programarea, ia cererea,
    cere orele, construieste corpul) si se opreste inainte de validare si de
    commit. Nimic nu se scrie; pune corpul construit in `out["payload"]`.
    """
    send = notify or send_telegram
    idnp = (config.get("auto_update_idnp") or "").strip() or (config.get("idnp") or "").strip()
    code = (config.get("appointment_code") or "").strip()      # -> RequestNumber
    number = (config.get("request_number") or "").strip()      # -> SgariNumber
    seria = (config.get("id_series") or "").strip()
    issue = _rb_issue_date(config)
    if not (idnp and code and number):
        lipsa = ", ".join(n for n, v in (("IDNP", idnp), ("cod programare", code),
                                         ("numar cerere", number)) if not v)
        await send(f"❌ <b>Auto-update esuat</b>: lipseste {lipsa} din setari.")
        return False

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout, headers=_api_headers()) as s:
        # ── 1. programarea curenta ────────────────────────────────────────
        found = await _rb_post(s, "apo-request/get-appointment",
                               {"IDNP": idnp, "RequestNumber": code,
                                "SgariNumber": number})
        if not found:
            await send("❌ <b>Auto-update esuat</b>: nu am gasit programarea cu "
                       "codul si numarul din setari. Se schimba la fiecare "
                       "reprogramare - actualizeaza-le in interfata web.")
            return False
        appt = found[0]
        guid = appt.get("id")
        if not appt.get("canBeModified"):
            # Regula ASP: modificarea e permisa doar cu peste 24h inainte.
            await send("❌ <b>Auto-update esuat</b>: ASP nu mai permite "
                       "modificarea acestei programari (mai putin de 24h?).")
            return False

        # ── 2. cererea intreaga ───────────────────────────────────────────
        obj = await _rb_get(s, f"fod/request/{_APPT_TYPE_CODE}/{guid}")
        sr = obj.get("serviceRequest") or {}
        svc_id = sr.get("publicServiceId")

        # ⛔ Singura diferenta dintre ce SERVESTE serverul si ce TRIMITE pagina:
        # `requestorStatuteModel` vine null din GET, dar validate/update il cer
        # ("Solicit în calitate de" -> 400 daca lipseste). Pagina il completeaza
        # inainte de submit exact cu valorile de mai jos - inclusiv "blablabla",
        # care chiar asta e in requestul real capturat de la site (campul e
        # obligatoriu in DTO dar nefolosit pentru o cerere in nume propriu).
        # Copiat identic: aici nu inventam, reproducem.
        req_o = obj.get("requestor") or {}
        if not req_o.get("requestorStatuteModel"):
            req_o["requestorStatuteModel"] = {
                "id": "00000000-0000-0000-0000-000000000000",
                "value": "blablabla", "onBehalfOn": 1}
            obj["requestor"] = req_o

        # ── 3. locatia tinta (poate diferi de cea a programarii curente) ──
        location = sr.get("examinationLocation") or {}
        if target_location:
            want = _normalize_loc(target_location)
            for loc in await _rb_get(s, f"qmatic/locations/{svc_id}"):
                name = loc.get("name") or ""
                norm = _normalize_loc(name)
                if want in norm or norm in want:
                    location = loc
                    break
        if not location.get("publicId"):
            await send("❌ <b>Auto-update esuat</b>: nu am identificat locatia.")
            return False
        # Forma exacta pe care o trimite pagina: qmatic/locations da si
        # addressCity/translations, site-ul le trimite null. ASP accepta si
        # varianta bogata (validat live), dar tinem corpul identic cu al lui.
        location = {"publicId": location["publicId"], "name": location.get("name"),
                    "addressCity": None, "translations": None,
                    "id": location["publicId"]}

        # ── 4. orele libere in ziua tinta ─────────────────────────────────
        hours = await _rb_post(s, "qmatic/times",
                               {"PublicServiceId": svc_id,
                                "PublicLocationId": location["publicId"],
                                "Date": target_dt.strftime("%Y-%m-%d"),
                                "RequestId": guid, "Idnp": idnp,
                                "SeriaAndNumber": seria, "IssueDate": issue})
        if not hours:
            await send(f"❌ <b>Auto-update esuat</b>: ziua "
                       f"{target_dt.strftime('%d.%m.%Y')} nu mai are ore libere "
                       f"(a prins-o altcineva).")
            return False
        hour = hours[0]
        hour_txt = hour.get("time") or hour.get("name") or ""

        # ── 5. schimbam DOAR data, ora si locatia; restul cererii ramane ──
        sr["examinationDate"] = _rb_iso(target_dt)
        sr["examinationTime"] = {"time": hour_txt, "id": hour.get("id") or hour_txt,
                                 "name": hour.get("name") or hour_txt}
        sr["examinationLocation"] = location
        sr["isMakingAppointment"] = True
        obj["serviceRequest"] = sr

        if dry_run:
            if out is not None:
                out.update({"payload": obj, "hours": hours,
                            "would_book": f"{target_dt.strftime('%d.%m.%Y')} {hour_txt} "
                                          f"la {location.get('name', '')}"})
            print(f"  [AU-API] DRY-RUN: m-as programa pe "
                  f"{target_dt.strftime('%d.%m.%Y')} {hour_txt} la "
                  f"{location.get('name', '')} - NU trimit nimic.")
            return False

        vres = await _rb_post(s, "apo-request/validate-appointment", obj)
        if isinstance(vres, dict) and not vres.get("isValid", True):
            msgs = "; ".join(str(v.get("message") or "")
                             for v in (vres.get("validations") or []))[:200]
            await send(f"❌ <b>Auto-update esuat</b>: ASP a respins reprogramarea "
                       f"({_rb_escape(msgs) or 'validare esuata'}).")
            return False

        # ── 6. COMMIT ─────────────────────────────────────────────────────
        await _rb_post(s, "qmatic/update", obj)

        # ── 7. dovada + perechea noua cod/numar ───────────────────────────
        after = await _rb_get(s, f"fod/request/{_APPT_TYPE_CODE}/{guid}")
        asr = after.get("serviceRequest") or {}
        got = (asr.get("examinationDate") or "")[:10]
        if got != target_dt.strftime("%Y-%m-%d"):
            await send(f"⚠️ <b>Auto-update incert</b>: am trimis reprogramarea pe "
                       f"{target_dt.strftime('%d.%m.%Y')}, dar cererea arata "
                       f"{got or '?'}. Verifica manual.")
            return False

    new_code = (after.get("requestNumber") or "").strip()
    new_number = (after.get("serviceProviderNumber") or "").strip()
    new_hour = (asr.get("examinationTime") or {}).get("time") or hour_txt
    if out is not None:
        out.update({"date": target_dt.strftime("%d.%m.%Y"), "hour": new_hour,
                    "code": new_code, "request_number": new_number})
    msg = (f"✅ <b>Programare actualizata automat!</b>\n"
           f"Noua data: <b>{target_dt.strftime('%d.%m.%Y')}</b>\n"
           f"Ora: <b>{new_hour}</b>\n"
           f"Locatie: {location.get('name', '')}")
    if new_code:
        msg += f"\nCod programare: <code>{new_code}</code>"
    if new_number:
        msg += f"\nNr. cererii: <code>{new_number}</code>"
    msg += "\n<i>(salvate automat in setari)</i>"
    await send(msg)
    print(f"  [AU-API] SUCCESS: {target_dt.strftime('%d.%m.%Y')} {new_hour} "
          f"la {location.get('name', '')}")
    return True


# ═════════════════════════════════════════════════════════════════════════════
#  PRIMA PROGRAMARE (cerere noua, link primit de la ASP)
# ═════════════════════════════════════════════════════════════════════════════
# Diferenta fata de auto-update: acolo ai deja o programare si o muti prin
# my-appointments (MODIFICA -> DA -> commit pe qmatic/update). Aici ai o cerere
# proaspata, cu plata deja facuta, si un link unic .../cerere/APO01/<guid>.
# Pasii sunt doar: alege data -> alege ora -> PROGRAMEAZA-TE, iar commit-ul
# real e pe qmatic/confirm (NU update).
#
# ⚠️ BUG-UL SITE-ULUI (descoperit 2026-07-29, blocheaza complet programarea):
# aplicatia Blazor cere orele de la
#     GET api/qmatic/times/<svc>/<loc>/<data>/<requestId>
# ruta care NU exista pe server -> raspunde 200 cu index.html. Clientul face
# JsonSerializer pe "<!DOCTYPE html>" -> JsonException aruncata dintr-un task
# de fundal -> moare toata aplicatia WASM (bara galbena "An unhandled error has
# occurred", spinner blocat la 60%) si NICIUN buton nu mai raspunde.
# Ruta care chiar exista pe server e aceeasi FARA requestId la final.
# Patch-ul de mai jos prinde raspunsul non-JSON si reinterogheaza varianta
# scurta. Se injecteaza ca init script => supravietuieste oricarui reload
# (spre deosebire de un simplu evaluate, care se pierde la navigare).
CERERE_RE = re.compile(
    r"https://eservicii\.gov\.md/asp/dimtcca/cerere/([A-Za-z0-9]+)/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

TIMES_REPAIR_JS = r"""
(() => {
  if (window.__aspTimesPatch) return;
  window.__aspTimesPatch = true;
  window.__aspPatchLog = [];
  const orig = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const r = await orig(input, init);
    if (!/\/api\/qmatic\/times\//.test(url)) return r;
    if (/json/i.test(r.headers.get("content-type") || "")) return r;
    const alt = url.replace(
      /\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/?$/i, "");
    if (alt === url) return r;
    try {
      const r2 = await orig(alt);
      const body = await r2.text();
      if (r2.ok && body.trim().startsWith("[")) {
        window.__aspPatchLog.push("reparat");
        return new Response(body, {
          status: 200, headers: {"content-type": "application/json"}});
      }
      window.__aspPatchLog.push("alt " + r2.status);
    } catch (e) { window.__aspPatchLog.push("alt eroare"); }
    // Backendul e jos. Returnam raspunsul original stricat: aplicatia va crapa
    // si reincercam la ciclul urmator. NU inventam ore - o ora fabricata ar
    // trimite o rezervare pentru un slot care poate nu exista.
    return r;
  };
})();
"""


def parse_cerere_url(url: str):
    """Valideaza un link de cerere ASP. Returneaza dict sau None."""
    m = CERERE_RE.search((url or "").strip())
    if not m:
        return None
    return {"url": m.group(0), "kind": m.group(1), "request_id": m.group(2)}


async def fetch_cerere_dates(service_hash: str, location_id: str, idnp: str = "",
                             seria: str = "", issue_date: str = ""):
    """Zilele libere pentru cererea data - 1 POST public, fara browser.

    Acelasi endpoint pe care il foloseste do_check_api, dar tintit pe serviciul
    si locatia cu care a fost creata cererea (o cerere e legata de o singura
    locatie). Returneaza lista de datetime sortata crescator.

    2026-08-17: `qmatic/dates` cere identitatea completa in corp (serie buletin
    + data emiterii) - vezi _read_calendar. Convertim un refuz intr-un
    RuntimeError clar ca apelantii care prind Exception sa vada cauza reala.
    """
    idnp = (idnp or FORM_DATA.get("idnp") or "").strip()
    seria = (seria or FORM_DATA.get("id_series") or "").strip()
    issue_date = (issue_date or _form_issue_date()).strip()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout,
                                     headers=_api_headers()) as session:
        try:
            payload = await _read_calendar(session, service_hash,
                                           location_id, idnp, seria, issue_date)
        except _IdentityRejected as e:
            raise RuntimeError(str(e))
    out = []
    for item in payload or []:
        raw = (item.get("date") or "").strip()
        try:
            out.append(datetime.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            continue
    return sorted(out)


async def probe_cerere(context, cerere_url: str):
    """Deschide linkul o data si afla ce serviciu/locatie foloseste cererea.

    Rulat la salvarea linkului in interfata: valideaza ca linkul e viu si ne da
    hash-urile cu care putem apoi interoga calendarul FARA browser.
    Returneaza {"service_hash", "location_id", "request_id", "dates"}.
    """
    parsed = parse_cerere_url(cerere_url)
    if not parsed:
        raise RuntimeError("Link invalid - astept .../cerere/APO01/<cod-unic>")

    await context.add_init_script(TIMES_REPAIR_JS)
    page = await context.new_page()
    found = {}

    def on_response(resp):
        m = re.search(r"/api/qmatic/dates/([0-9a-fA-F]{16,})/([0-9a-fA-F]{16,})",
                      resp.url)
        if m and not found:
            found["service_hash"] = m.group(1)
            found["location_id"] = m.group(2)

    page.on("response", on_response)
    try:
        # domcontentloaded, nu networkidle: vezi nota din first_time_booking.
        await page.goto(parsed["url"], wait_until="domcontentloaded",
                        timeout=60000)
        # Acelasi boot lent de Blazor WASM ca in restul aplicatiei.
        try:
            await page.wait_for_selector('button[aria-label="Open Date Picker"]',
                                         timeout=180000)
        except PlaywrightTimeout:
            raise RuntimeError(
                "Pagina cererii nu s-a incarcat (link expirat sau site cazut?)")
        await page.wait_for_timeout(1500)
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not found:
        raise RuntimeError(
            "Nu am putut identifica serviciul/locatia cererii - "
            "verifica daca linkul mai e valid.")

    found["request_id"] = parsed["request_id"]
    try:
        found["dates"] = await fetch_cerere_dates(found["service_hash"],
                                                  found["location_id"])
    except Exception:
        found["dates"] = []
    return found


async def _pick_first_hour(page):
    """Alege prima ora reala din dropdownul Ora. Returneaza textul sau None."""
    try:
        ora = page.locator('select[name*="Time"], select[name*="Hour"], '
                           'select[name*="Ora"]').first
        if not await ora.is_visible(timeout=2000):
            ora = page.locator("select:visible").last
        for opt in await ora.locator("option").all():
            val = await opt.get_attribute("value") or ""
            txt = (await opt.inner_text()).strip()
            if not val or not txt or "selecta" in txt.lower():
                continue
            await ora.select_option(value=val)
            await page.wait_for_timeout(500)
            return txt
    except Exception as e:
        print(f"  [FB] Avertisment selectare Ora: {e}")
    return None


async def first_time_booking(context, cerere_url: str, target_dt: datetime,
                             notify=None):
    """Programeaza pentru prima data folosind linkul de cerere.

    Returneaza (ok: bool, info: dict). info poate contine 'code' (codul
    programarii extras din pagina de confirmare), 'hour' si 'date'.
    """
    send = notify or send_telegram
    parsed = parse_cerere_url(cerere_url)
    if not parsed:
        print("  [FB] Link de cerere invalid - skip")
        return False, {}

    ziua = target_dt.strftime("%d.%m.%Y")
    await context.add_init_script(TIMES_REPAIR_JS)
    page = await context.new_page()
    try:
        # ── Data: pana la 10 incercari cu reload (calendarul se poate incarca gol)
        date_picked = False
        for attempt in range(1, 11):
            if attempt == 1:
                # NU networkidle: pe pagina cererii conexiunile raman deschise
                # si evenimentul poate sa nu vina niciodata (observat 2026-07-29
                # - deschiderea a atarnat >120s desi pagina se incarcase).
                # Semnalul real de "gata" e butonul calendarului, pe care oricum
                # il asteptam mai jos.
                await page.goto(parsed["url"], wait_until="domcontentloaded",
                                timeout=60000)
            try:
                await page.wait_for_selector(
                    'button[aria-label="Open Date Picker"]', timeout=180000)
            except PlaywrightTimeout:
                print(f"  [FB] Pagina cererii neincarcata (incercarea {attempt}/10)")
                await page.reload(wait_until="domcontentloaded")
                continue
            await page.wait_for_timeout(1200)
            try:
                await page.click('button[aria-label="Open Date Picker"]',
                                 timeout=10000)
            except Exception as e:
                print(f"  [FB] Nu am putut deschide calendarul "
                      f"(incercarea {attempt}/10): {e}")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                continue
            await page.wait_for_timeout(1200)

            if await _pick_date_in_popup(page, target_dt):
                date_picked = True
                break
            print(f"  [FB] Data {ziua} nu a putut fi selectata "
                  f"(incercarea {attempt}/10), reload...")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

        if not date_picked:
            await send(f"❌ <b>Prima programare esuata</b>: data {ziua} "
                       f"nu a putut fi selectata (10 incercari).")
            return False, {}

        await page.wait_for_timeout(2500)

        # Daca patch-ul n-a reusit sa repare ruta, aplicatia e moarta: nu are
        # rost sa apasam butonul. Reincercam la ciclul urmator.
        patch_log = await page.evaluate("window.__aspPatchLog || []")
        if patch_log and all("reparat" not in x for x in patch_log):
            print(f"  [FB] Ruta 'times' nu a putut fi reparata ({patch_log}) - "
                  f"backendul QMatic pare jos. Reincerc la ciclul urmator.")
            return False, {"retry": True}

        hour = await _pick_first_hour(page)
        if not hour:
            print("  [FB] Nicio ora disponibila in dropdown - reincerc mai tarziu.")
            return False, {"retry": True}

        # ── Commit: pentru o cerere NOUA site-ul trimite validate-appointment
        #    urmat de qmatic/CONFIRM (la reprogramare ar fi qmatic/update).
        try:
            async with page.expect_response(
                    lambda r: "qmatic/confirm" in r.url, timeout=45000) as info:
                await page.locator(
                    "button",
                    has_text=re.compile(r"PROGRAMEAZ", re.IGNORECASE)
                ).first.click()
            confirm = await info.value
        except PlaywrightTimeout:
            print("  [FB] qmatic/confirm nu a fost trimis - validarea a esuat")
            await send("❌ <b>Prima programare esuata</b>: site-ul nu a "
                       "confirmat rezervarea (validare esuata).")
            return False, {"retry": True}
        except Exception as e:
            print(f"  [FB] Nu am putut apasa Programeaza-te: {e}")
            return False, {"retry": True}

        if not confirm.ok:
            print(f"  [FB] qmatic/confirm a esuat: HTTP {confirm.status}")
            await send(f"❌ <b>Prima programare esuata</b>: serverul a raspuns "
                       f"HTTP {confirm.status}.")
            return False, {"retry": True}

        await page.wait_for_timeout(2500)

        # Codul programarii apare pe pagina de finalizare - il extragem ca sa
        # ajunga direct in Telegram (userul are nevoie de el la ghiseu).
        info_out = {"date": ziua, "hour": hour}
        try:
            body = await page.inner_text("body")
            m = re.search(r"Codul program[ăa]rii\s*([A-Z0-9]{10,})", body)
            if m:
                info_out["code"] = m.group(1)
            m = re.search(r"Num[ăa]rul cererii\s*([0-9]{6,})", body)
            if m:
                info_out["request_number"] = m.group(1)
        except Exception:
            pass

        msg = (f"🎉 <b>PROGRAMAT!</b>\n"
               f"Data: <b>{ziua}</b>")
        if hour:
            msg += f"\nOra: <b>{hour}</b>"
        if info_out.get("code"):
            msg += f"\nCod programare: <code>{info_out['code']}</code>"
        if info_out.get("request_number"):
            msg += f"\nNr. cererii: <code>{info_out['request_number']}</code>"
        msg += "\n\n⚠️ Verifica si pe email confirmarea de la ASP."
        await send(msg)
        print(f"  [FB] SUCCESS: programat {ziua} {hour or ''} "
              f"cod={info_out.get('code', '?')}")
        return True, info_out

    except Exception as e:
        print(f"  [FB] Eroare neasteptata: {e}")
        await send(f"❌ Prima programare - eroare neasteptata: {e}")
        return False, {"retry": True}
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def do_check(page):
    """Ruleaza o singura verificare si returneaza rezultatele."""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  Verificare la {now}")
    print(f"{'='*60}")

    diagnostics = {
        "locations_found": 0,
        "calendar_unavailable_count": 0,
        "had_check_error": False,
    }

    try:
        await page.goto(URL, wait_until="networkidle", timeout=60000)

        # Pagina FOD e Blazor WebAssembly: dupa networkidle runtime-ul .NET
        # inca booteaza in browser ("Se incarca configuratiile pentru
        # aplicatia FOD"). Pe CPU slab (Render free ~0.1 vCPU) bootul poate
        # dura minute, nu secunde - asteptam explicit campul, generos.
        await page.wait_for_selector('input[name="RequestorIdnp"]', timeout=180000)

        await page.fill('input[name="RequestorIdnp"]',     FORM_DATA["idnp"])
        await page.fill('input[name="RequestorLastName"]',  FORM_DATA["last_name"])
        await page.fill('input[name="RequestorFirstName"]', FORM_DATA["first_name"])
        await page.fill('input[name="RequestorPhone"]',     FORM_DATA["phone"])
        await page.fill('input[name="RequestorEmail"]',     FORM_DATA["email"])
        checkboxes = page.locator('input[type="checkbox"]')
        cb_count   = await checkboxes.count()
        await checkboxes.nth(cb_count - 1).check(force=True)
        await page.wait_for_timeout(500)

        await page.locator("button", has_text="PASUL").click()
        await page.wait_for_selector('input[name="SeriaAndNumber"]', timeout=30000)

        await page.fill('input[name="SeriaAndNumber"]', FORM_DATA["id_series"])
        await page.locator('input[name="SeriaAndNumber"]').press("Tab")
        await page.wait_for_timeout(500)

        await page.click('button[aria-label="Open Date Picker"]')
        await page.wait_for_timeout(800)
        id_date = FORM_DATA.get("id_date") or {}
        await select_date_in_picker(page,
                                    int(id_date.get("day", 1)),
                                    int(id_date.get("month", 1)),
                                    int(id_date.get("year", 2020)))

        print("  Astept dupa data (3s)...")
        await page.wait_for_timeout(3000)

        await page.wait_for_selector('input[name="MedicalCertificateNumber"]', timeout=20000)
        await page.fill('input[name="MedicalCertificateNumber"]', FORM_DATA["medical_cert"])
        await page.locator('input[name="MedicalCertificateNumber"]').press("Tab")

        print("  Astept dupa adeverinta (3s)...")
        await page.wait_for_timeout(3000)

        await page.wait_for_selector('select[name="RequestType"]', timeout=10000)
        await page.select_option('select[name="RequestType"]', FORM_DATA["service_type"])
        await page.wait_for_timeout(1000)
        await page.wait_for_selector('select[name="ReasonPracticalEnum"]', timeout=10000)
        await page.select_option('select[name="ReasonPracticalEnum"]', FORM_DATA["category"])
        await page.wait_for_timeout(1000)
        await page.wait_for_selector('select[name="ReasonEnum"]', timeout=10000)
        await page.select_option('select[name="ReasonEnum"]', FORM_DATA["reason"])
        await page.wait_for_timeout(2000)

        # Citeste locatiile
        await page.wait_for_selector('select[name="ExaminationLocation"]', timeout=10000)
        all_options = await page.locator('select[name="ExaminationLocation"] option').all()
        # Utilizatorul alege ce locatii sa fie scrapuite (scrape_locations).
        # Daca lista e goala (config vechi/standalone), pastram comportamentul
        # vechi: toate locatiile DECA Chisinau, mai putin Salcamilor.
        selected_norm = [_normalize_loc(s) for s in (FORM_DATA.get("scrape_locations") or []) if s]
        chisinau_locations = []
        for opt in all_options:
            txt = (await opt.inner_text()).strip()
            val = await opt.get_attribute("value") or ""
            if not txt or "Selecta" in txt:
                continue
            if "deca chi" not in txt.lower():
                continue
            norm = _normalize_loc(txt)
            if selected_norm:
                if not any(s in norm or norm in s for s in selected_norm):
                    continue
            else:
                # Fallback (nicio selectie): exclude Salcamilor, ca inainte.
                if "salc" in txt.lower():
                    continue
            chisinau_locations.append({"label": txt, "value": val})

        diagnostics["locations_found"] = len(chisinau_locations)
        print(f"  Locatii gasite: {len(chisinau_locations)}")
        for loc in chisinau_locations:
            print(f"    - {loc['label']} (value={loc['value']})")

        all_results = {loc["label"]: {m: [] for m in FORM_DATA["target_months"]} for loc in chisinau_locations}

        for loc in chisinau_locations:
            loc_name = loc["label"]
            print(f"\n  >> {loc_name}")
            reloaded = await select_location_with_reload_check(
                page, loc, chisinau_locations, max_attempts=8
            )
            if not reloaded:
                diagnostics["calendar_unavailable_count"] += 1
                print("    [!] Calendar indisponibil (reload esuat dupa 8 incercari)")
                continue
            try:
                await page.wait_for_selector('select[name="ExaminationLocation"]', timeout=10000)
                await page.wait_for_selector(".fod-picker-static", timeout=15000)
                days = await extract_with_warmup(page, retries=2)
                for month, available in days.items():
                    if len(available) > len(all_results[loc_name][month]):
                        all_results[loc_name][month] = available
                for month, available in all_results[loc_name].items():
                    status = f"{len(available)} zile" if available else "nicio zi"
                    print(f"    {month.capitalize()}: {status}")
            except PlaywrightTimeout:
                diagnostics["calendar_unavailable_count"] += 1
                print("    [!] Calendar indisponibil")

        return all_results, diagnostics

    except Exception as e:
        diagnostics["had_check_error"] = True
        print(f"  EROARE in verificare: {e}")
        # Ce a servit site-ul de fapt (pagina de blocare WAF? mentenanta?)
        try:
            title = await page.title()
            body = (await page.inner_text("body"))[:250].replace("\n", " | ")
            print(f"  [DIAG] url={page.url}")
            print(f"  [DIAG] titlu={title}")
            print(f"  [DIAG] continut: {body}")
        except Exception:
            pass
        return {}, diagnostics


API_BASE = "https://eservicii.gov.md/asp/dimtcca/api"
_API_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _api_headers():
    """Antetele cu care API-ul ASP ne raspunde.

    2026-08-03: site-ul a pus o regula de filtrare care intoarce
    "402 Payment Required" (nginx, pagina HTML) la ORICE cerere fara antetul
    Sec-Fetch-Site - adica la orice client care nu e un browser real.
    Verificat prin bisectie: doar Sec-Fetch-Site conteaza si ORICE valoare
    trece (same-origin / none / cross-site); Sec-Fetch-Mode si Sec-Fetch-Dest
    nu schimba nimic. Le trimitem oricum pe toate trei, ca un browser.
    """
    headers = {"User-Agent": _API_UA,
               "Accept": "application/json, text/plain, */*",
               "Referer": URL,
               "Sec-Fetch-Site": "same-origin",
               "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Dest": "empty"}
    # Cheia releului (vezi _api_base): fara ea Worker-ul ar fi un proxy deschis.
    secret = os.environ.get("ASP_API_PROXY_SECRET")
    if secret:
        headers["X-ASP-Proxy-Key"] = secret
    return headers


def _api_base() -> str:
    """De unde citim cele 3 GET-uri publice ale calendarului.

    04.08.2026: WAF-ul ASP a inceput sa raspunda "402 Payment Required" la
    cererile venite de pe IP-ul Render, desi aceleasi cereri, cu aceleasi
    antete, merg de acasa si chiar de pe alte IP-uri de datacenter. Deci
    blocarea e pe REPUTATIA IP-ului (acelasi IP scana la fiecare 2 minute),
    nu pe antete - niciun antet nu o poate ocoli. Solutia e sa mutam iesirea
    pe alt IP: un Cloudflare Worker care releu-eaza exact aceste 3 rute
    (vezi proxy/worker.js).

    Gol => mergem direct la ASP: local, in GUI si in .exe nu e nevoie de releu.
    Caile cu browser (rebooking, first_time_booking) NU trec pe aici - ele au
    deja amprenta unui Chromium real.
    """
    return (os.environ.get("ASP_API_PROXY") or API_BASE).rstrip("/")


def _redact(text) -> str:
    """Textul unei erori, fara date personale.

    Erorile de retea includ URL-ul complet, iar URL-ul calendarului contine
    ?idnp=<13 cifre> - asa ca textul ajunge pe Telegram doar mascat.
    """
    return re.sub(r"\d{13}", "<idnp>", str(text))


class _IdentityRejected(Exception):
    """qmatic/dates a raspuns 403: identitatea din corp (serie buletin + data
    emiterii) lipseste sau nu valideaza pentru acest IDNP. E o eroare de
    configurare a contului de scanare, NU o pana a site-ului si nici un blocaj
    de IP - deci nu declanseaza backoff-ul."""


class _RateLimited(Exception):
    """qmatic/dates a raspuns 429: am epuizat cota de citiri a calendarului.

    ⚠️ 18.08.2026 dimineata am crezut ca e RITMUL nostru (prima locatie trecea,
    urmatoarele doua, trase spate-in-spate, luau 429) si am raspuns cu pauze
    intre locatii. GRESIT - pauzele nu ajuta deloc.

    Masurat seara, cu sonde din 3 puncte (acasa, Render, Worker):
      * 429 vine si de ACASA, la PRIMA cerere a zilei de pe IP-ul asta;
      * un ALT IDNP (checksum valid) de pe ACELASI IP primeste 403, nu 429;
      * antetul Retry-After e identic pe toate cele 3 locatii si scade odata cu
        ceasul (12927 -> 12861 -> 12758), spre 00:00 UTC (03:00 ora Chisinaului).
    Deci limita e o COTA PE IDNP, resetata zilnic la 00:00 UTC - nu o limita pe
    IP, nu o pana a site-ului si nu ritmul cererilor. Odata epuizata, orice
    cerere in plus e irosita: nu prelungeste fereastra, dar nici nu intoarce
    nimic. Singurul raspuns corect e sa numeri cate citiri incap intr-o zi si
    sa le imparti pe toata ziua (vezi bugetul din web.py _shared_loop).

    Cota e doar pe calendar: apo-request/get-appointment raspunde 200 in acelasi
    timp in care qmatic/dates da 429, deci reprogramarea nu e blocata de ea.
    """

    def __init__(self, message, retry_after: float = 0.0):
        super().__init__(message)
        # Secunde pana la resetarea ferestrei, din antetul Retry-After.
        self.retry_after = retry_after


# Pauza intre citirile celor 3 calendare. NU are legatura cu 429 (aia e o cota
# pe IDNP, vezi _RateLimited) - e doar bunul simt de a nu trage trei cereri in
# aceeasi milisecunda.
_SCAN_GAP_SECONDS = 1.5
# Pauza de baza intre runde; creste cu numarul rundei (6s, 12s, 18s).
_RATE_LIMIT_PAUSE = 6.0
# Cate runde incercam pana declaram o locatie necitita. O locatie care n-a
# raspuns nu inseamna "fara locuri", ci o citire pierduta - si asa se rateaza
# o zi aparuta pentru cateva minute. Worst case ~45s, deci incape lejer intr-un
# ciclu de 2 minute.
_CALENDAR_ROUNDS = 4


def iso_issue_date(day, month, year) -> str:
    """Data emiterii buletinului in formatul cerut de qmatic/dates:
    'YYYY-MM-DDT00:00:00'. Gol daca lipseste ceva."""
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T00:00:00"
    except (TypeError, ValueError):
        return ""


def _form_issue_date() -> str:
    d = FORM_DATA.get("id_date") or {}
    return iso_issue_date(d.get("day"), d.get("month"), d.get("year"))


async def _read_calendar(session, service_hash: str, location_id: str,
                         idnp: str, seria: str, issue_date: str):
    """Zilele libere la o locatie. Returneaza lista JSON a calendarului.

    ISTORIC:
      * pana in 2026-08-03: GET /qmatic/dates/<svc>/<loc>            (public)
      * 2026-08-03: acelasi GET dar cu ?idnp=<13 cifre> obligatoriu  (public)
      * 2026-08-17: ruta GET a DISPARUT (orice GET -> index.html al SPA-ului)
        si calendarul e acum **POST /api/qmatic/dates**. Corpul cere acum
        IDENTITATEA COMPLETA a solicitantului:
            {publicServiceId, publicLocationId, idnp, seriaAndNumber, issueDate}
        Fara serie+data emiterii (sau gresite) -> 403 Forbidden; cu ele corecte
        -> 200 cu zilele. Poarta e VALIDAREA IDENTITATII, nu plata: valoarea lui
        publicServiceId nu conteaza (hash-ul vechi din get-service merge la fel),
        iar raspunsul e acelasi pentru orice identitate valida, deci un singur
        scan partajat ramane valid. issueDate = 'YYYY-MM-DDT00:00:00'.
        Vezi [[asp-qmatic-dates-gated-behind-wizard-2026-08-17]].
    """
    idnp = (idnp or "").strip()
    seria = (seria or "").strip()
    issue_date = (issue_date or "").strip()
    missing = [n for n, v in (("IDNP", idnp), ("serie buletin", seria),
                              ("data emiterii", issue_date)) if not v]
    if missing:
        raise _IdentityRejected("lipsesc din setarile contului de scanare: "
                                + ", ".join(missing))
    payload = {"publicServiceId": service_hash,
               "publicLocationId": location_id,
               "idnp": idnp,
               "seriaAndNumber": seria,
               "issueDate": issue_date}
    async with session.post(f"{_api_base()}/qmatic/dates",
                            json=payload) as resp:
        if resp.status == 403:
            # Corpul e complet dar ASP il refuza: seria/data emiterii nu se
            # potrivesc cu IDNP-ul. Eroare de configurare, nu de retea.
            raise _IdentityRejected(
                "buletinul (serie) sau data emiterii nu corespund IDNP-ului "
                "(verifica setarile contului folosit la scanare)")
        if resp.status == 429:
            # Retry-After = secunde pana la 00:00 UTC, adica pana se reseteaza
            # cota zilnica a IDNP-ului. Il ducem mai departe ca sa dormim exact
            # cat trebuie, in loc sa ghicim.
            try:
                retry_after = float(resp.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            raise _RateLimited(
                "cota zilnica de citiri ale calendarului pentru acest IDNP e "
                "epuizata (429)", retry_after)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if resp.status != 200 or "text/html" in ctype:
            body = (await resp.text())[:120]
            raise RuntimeError(f"qmatic/dates a raspuns {resp.status}: {body!r}")
        return await resp.json()


def filter_locations(labels, selected, legacy_fallback: bool = True):
    """Pastreaza doar locatiile alese de utilizator (potrivire fara diacritice).

    Lista goala inseamna lucruri diferite in cele doua moduri, de aceea flagul:
      * standalone/GUI (legacy_fallback=True): config vechi fara camp -> se
        pastreaza comportamentul de dinainte, toate DECA Chisinau fara Salcamilor;
      * monitor partajat (legacy_fallback=False): utilizatorul chiar nu a bifat
        nimic -> nu i se anunta NICIO locatie (o locatie nebifata nu ajunge la el
        niciodata, chiar daca e scanata pentru altcineva).
    """
    selected_norm = [_normalize_loc(s) for s in (selected or []) if s]
    if not selected_norm and not legacy_fallback:
        return []
    kept = []
    for name in labels:
        norm = _normalize_loc(name)
        if selected_norm:
            if not any(s in norm or norm in s for s in selected_norm):
                continue
        elif "salc" in name.lower():
            continue
        kept.append(name)
    return kept


def days_by_month(dates, months):
    """[datetime, ...] -> {'iulie': ['08 iulie 2026', ...]} doar pentru lunile cerute."""
    months = [str(m).lower() for m in months]
    out = {m: [] for m in months}
    for dt in sorted(dates):
        month_name = RO_MONTHS_FULL[dt.month - 1]
        if month_name in out:
            out[month_name].append(f"{dt.day:02d} {month_name} {dt.year}")
    return out


async def fetch_all_locations_dates(service_type: str = "PracticalExam",
                                    category: str = "BMechanical",
                                    idnp: str = "", seria: str = "",
                                    issue_date: str = "",
                                    only_locations=None):
    """Toate zilele libere, la TOATE locatiile DECA Chisinau - fara browser.

    Exact API-ul pe care il apeleaza aplicatia Blazor cand alegi locatia:
      1. GET  /apo-request/get-service/<tip>/False/<categorie>  -> hash serviciu
      2. GET  /qmatic/locations/<hash>                          -> lista locatii
      3. POST /qmatic/dates {publicServiceId, publicLocationId, idnp,
              seriaAndNumber, issueDate}                         -> zile libere
    Din 2026-08-17 pasul 3 e POST si cere IDENTITATEA completa (serie buletin +
    data emiterii), altfel 403 - vezi _read_calendar. Raspunsul NU depinde de
    identitate atata timp cat e valida (aceleasi zile pentru toti), deci un
    singur scan partajat ramane valid: folosim identitatea primului cont activ
    care o are completa. Lunile NU se filtreaza aici (e treaba fiecarui
    utilizator, in aval).

    only_locations = etichetele pe care le urmareste macar un cont activ. Din
    18.08.2026 conteaza: fiecare citire consuma din cota zilnica a IDNP-ului
    (vezi _RateLimited), deci o locatie pe care n-o vrea nimeni ar taia
    degeaba de trei ori din numarul de scanari pe zi. None/gol = toate.

    Returneaza ({eticheta locatie: [datetime, ...]}, diagnostics).
    """
    now = datetime.now().strftime("%H:%M:%S")
    print()
    print("=" * 60)
    print(f"  Scanare la {now} (direct pe API, fara browser)")
    print(f"{'='*60}")

    diagnostics = {
        "locations_found": 0,
        "calendar_unavailable_count": 0,
        "had_check_error": False,
        # Textul primei erori reale. Fara el alerta de pe Telegram spune doar
        # "Eroare=DA", iar cauzele (402 de la WAF, 400 idnp, IDNP lipsa din
        # setari, retea) arata toate la fel - vezi pana din 04.08.2026, cand
        # diagnosticul a cerut acces la logurile Render.
        "error_text": "",
        # 2026-08-17: True cand qmatic/dates raspunde 403 fiindca identitatea
        # (serie buletin + data emiterii) lipseste sau nu se potriveste cu
        # IDNP-ul. E o eroare de CONFIGURARE a contului de scanare, nu o pana a
        # site-ului si nici un blocaj de IP - se raporteaza o data, fara
        # backoff. Vezi [[asp-qmatic-dates-gated-behind-wizard-2026-08-17]].
        "identity_bad": False,
        # 2026-08-18: True cand ASP a raspuns 429. Nu e o pana a site-ului,
        # nici un blocaj de IP, nici ritmul cererilor: e COTA ZILNICA a
        # IDNP-ului, resetata la 00:00 UTC (vezi _RateLimited). Monitorul
        # doarme pana la resetare in loc sa strige "nu pot scana".
        "rate_limited": False,
        # Secunde pana la resetarea cotei (antetul Retry-After al lui ASP).
        "retry_after": 0.0,
        # Cate calendare am apucat sa citim cu succes in scanul asta. Din ele
        # isi invata web.py bugetul zilnic - vezi _shared_loop.
        "calendar_reads": 0,
    }
    idnp = (idnp or FORM_DATA.get("idnp") or "").strip()
    seria = (seria or FORM_DATA.get("id_series") or "").strip()
    issue_date = (issue_date or _form_issue_date()).strip()
    results = {}

    # Identitate incompleta = eroare de configurare, nu o pana a site-ului:
    # calendarul cere idnp + serie buletin + data emiterii (din 2026-08-17).
    # O marcam o singura data, nu ca 3 calendare indisponibile la rand.
    missing = [n for n, v in (("IDNP", idnp), ("serie buletin", seria),
                              ("data emiterii", issue_date)) if not v]
    if missing:
        diagnostics["had_check_error"] = True
        diagnostics["identity_bad"] = True
        diagnostics["error_text"] = (
            "contul de scanare nu are completat: " + ", ".join(missing)
            + " (calendarul ASP le cere pe toate din 17.08.2026)")
        print(f"  EROARE: {diagnostics['error_text']}")
        return {}, diagnostics

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=_api_headers()) as session:
            svc_url = (f"{_api_base()}/apo-request/get-service/"
                       f"{service_type}/False/{category}")
            async with session.get(svc_url) as resp:
                service_id = (await resp.text()).strip().strip('"')
                if resp.status != 200 or not re.fullmatch(r"[0-9a-fA-F]{16,}", service_id):
                    raise RuntimeError(
                        f"get-service a raspuns {resp.status}: {service_id[:120]!r}")

            async with session.get(f"{_api_base()}/qmatic/locations/{service_id}") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"qmatic/locations a raspuns {resp.status}")
                locations = await resp.json()

            wanted = []
            for loc in locations:
                name = (loc.get("name") or "").strip()
                pub_id = loc.get("publicId") or loc.get("id") or ""
                if not name or not pub_id or "deca chi" not in name.lower():
                    continue
                wanted.append({"label": name, "value": pub_id})

            diagnostics["locations_found"] = len(wanted)
            print(f"  Locatii gasite: {len(wanted)}")

            # Citim doar locatiile urmarite de cineva: fiecare citire costa din
            # cota zilnica a IDNP-ului (18.08.2026), deci trei locatii inseamna
            # de trei ori mai putine scanari pe zi decat una singura.
            kept = filter_locations([w["label"] for w in wanted],
                                    only_locations, legacy_fallback=False) \
                if only_locations else None
            if kept:
                wanted = [w for w in wanted if w["label"] in kept]
                print(f"  Locatii urmarite de conturi: {len(wanted)} "
                      f"({', '.join(w['label'] for w in wanted)})")

            # O locatie care n-a raspuns NU e o locatie fara locuri: e o citire
            # pierduta, si exact asa se rateaza o zi aparuta pentru cateva
            # minute. Deci insistam pe cele ramase, in runde, pana le avem pe
            # toate sau pana se termina rundele - abia ce ramane nerezolvat
            # dupa toate rundele se raporteaza ca "calendar indisponibil".
            # (Tiparul e vechi si la ASP: si pe vremea scanului prin browser
            # calendarul se incarca uneori gol si trebuia reincercat.)
            for loc in wanted:
                results[loc["label"]] = []
            pending = list(wanted)
            for round_no in range(1, _CALENDAR_ROUNDS + 1):
                if round_no > 1:
                    # Pauza intre runde creste: 429 se stinge daca tacem putin.
                    pause = _RATE_LIMIT_PAUSE * (round_no - 1)
                    print(f"    [i] {len(pending)} locatii necitite - "
                          f"runda {round_no}/{_CALENDAR_ROUNDS} peste {pause:.0f}s")
                    await asyncio.sleep(pause)
                still, stop_all = [], False
                for pos, loc in enumerate(pending):
                    # Fara pauza, locatiile 2 si 3 iau 429 (vezi _RateLimited).
                    if pos or round_no > 1:
                        await asyncio.sleep(_SCAN_GAP_SECONDS)
                    try:
                        dates = await _read_calendar(session, service_id,
                                                     loc["value"], idnp, seria,
                                                     issue_date)
                    except _IdentityRejected as e:
                        # 2026-08-17: 403 fiindca serie buletin / data emiterii
                        # nu valideaza pentru IDNP. Eroare de CONFIGURARE a
                        # contului de scanare, nu o pana a site-ului si nici un
                        # blocaj de IP - o marcam distinct ca sa nu spamam
                        # "calendar indisponibil" si sa nu intram in backoff-ul
                        # de "protejam IP-ul". Toate locatiile ar da acelasi
                        # 403, deci nu are rost sa mai incercam.
                        diagnostics["identity_bad"] = True
                        diagnostics["had_check_error"] = True
                        if not diagnostics["error_text"]:
                            diagnostics["error_text"] = str(e)
                        print(f"    [i] {loc['label']}: {e}")
                        stop_all = True
                        break
                    except _RateLimited as e:
                        # Cota e pe IDNP, nu pe locatie: daca una a luat 429,
                        # toate celelalte iau 429, iar reincercarile din rundele
                        # urmatoare sunt timp pierdut. Iesim si lasam monitorul
                        # sa doarma pana la resetare.
                        diagnostics["rate_limited"] = True
                        diagnostics["retry_after"] = e.retry_after
                        if not diagnostics["error_text"]:
                            diagnostics["error_text"] = str(e)
                        print(f"    [i] {loc['label']}: {e} "
                              f"(reset in {e.retry_after / 3600:.1f}h)")
                        stop_all = True
                        break
                    except Exception as e:
                        loc["last_error"] = e
                        still.append(loc)
                        print(f"    [~] {loc['label']}: {e} - reincerc")
                        continue

                    diagnostics["calendar_reads"] += 1
                    for entry in dates or []:
                        try:
                            results[loc["label"]].append(
                                datetime.fromisoformat(entry["date"]))
                        except Exception:
                            continue
                    found = results[loc["label"]]
                    if found:
                        pretty = ", ".join(d.strftime("%d.%m") for d in sorted(found))
                        print(f"    [+] {loc['label']}: {len(found)} zile ({pretty})")
                    else:
                        print(f"    [-] {loc['label']}: nicio zi")
                pending = still
                if stop_all or not pending:
                    break

            # Ce n-a raspuns nici dupa toate rundele. La 429 nu numaram nimic:
            # calendarul nu e "indisponibil", doar cota zilnica s-a terminat -
            # altfel monitorul ar raporta o pana care nu exista (exact alertele
            # "Calendar indisponibil=3" din 18.08.2026).
            for loc in (pending if not diagnostics["rate_limited"] else []):
                diagnostics["calendar_unavailable_count"] += 1
                if not diagnostics["error_text"]:
                    diagnostics["error_text"] = \
                        f"calendar: {_redact(loc.get('last_error'))}"
                print(f"    [!] {loc['label']}: calendar indisponibil dupa "
                      f"{_CALENDAR_ROUNDS} runde ({loc.get('last_error')})")

        return results, diagnostics

    except Exception as e:
        diagnostics["had_check_error"] = True
        diagnostics["error_text"] = _redact(e)
        print(f"  EROARE in scanare (API): {e}")
        return {}, diagnostics


async def do_check_api():
    """Verificare pentru UN utilizator (mod standalone/GUI): scaneaza tot, apoi
    aplica filtrele lui (locatii + luni). Monitorul partajat din web.py nu
    trece pe aici - el cheama fetch_all_locations_dates o singura data si
    filtreaza separat pentru fiecare cont.

    Returneaza (results, diagnostics) in acelasi format ca do_check().
    """
    target_months = [m.lower() for m in FORM_DATA["target_months"]]
    raw, diagnostics = await fetch_all_locations_dates(
        FORM_DATA["service_type"], FORM_DATA["category"])

    kept = filter_locations(list(raw), FORM_DATA.get("scrape_locations"))
    all_results = {}
    for label in kept:
        all_results[label] = days_by_month(raw[label], target_months)
        print()
        print(f"  >> {label}")
        for month in target_months:
            found = all_results[label][month]
            print(f"    {month.capitalize()}: "
                  f"{str(len(found)) + ' zile' if found else 'nicio zi'}")
        off = [d.strftime("%d.%m.%Y") for d in sorted(raw[label])
               if RO_MONTHS_FULL[d.month - 1] not in target_months]
        if off:
            print(f"    (in afara lunilor urmarite: {', '.join(off)})")

    # Locatiile scanate exista, dar niciuna nu e bifata de utilizator: nu e o
    # pana de scanare, deci nu raportam 0 locatii (ar declansa alerta 'nu pot
    # scana site-ul'). Semnalam doar in consola.
    if diagnostics["locations_found"] and not kept:
        print("  [!] Nicio locatie bifata dintre cele scanate - "
              "bifeaza cel putin una in setari.")

    return all_results, diagnostics


def _apply_config(config: dict):
    """Config dict -> variabile globale. Apelata la pornire si la fiecare ciclu
    (hot-reload: schimbarile salvate din UI se aplica fara restart de monitor)."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, INTERVAL_MINUTE, FORM_DATA

    TELEGRAM_BOT_TOKEN = config.get("telegram_token", "")
    TELEGRAM_CHAT_ID = config.get("telegram_chat_id", "")
    INTERVAL_MINUTE = config.get("interval_minutes", 5)

    FORM_DATA = {
        "idnp": config.get("idnp", ""),
        "last_name": config.get("last_name", ""),
        "first_name": config.get("first_name", ""),
        "phone": config.get("phone", ""),
        "email": config.get("email", ""),
        "id_series": config.get("id_series", ""),
        "id_date": {
            "day": config.get("id_date_day", 1),
            "month": config.get("id_date_month", 1),
            "year": config.get("id_date_year", 2020),
        },
        "medical_cert": config.get("medical_cert", ""),
        "service_type": "PracticalExam",
        "category": "BMechanical",
        "reason": "ObtainingRightToDrive",
        "target_months": config.get("target_months", ["aprilie"]),
        "scrape_locations": config.get("scrape_locations", []),
        "auto_update_enabled": bool(config.get("auto_update_enabled", False)),
        "auto_update_idnp": config.get("auto_update_idnp", ""),
        "appointment_code": config.get("appointment_code", ""),
        "request_number": config.get("request_number", ""),
        "target_location": config.get("target_location", ""),
        "current_appointment_date": config.get("current_appointment_date", ""),
        # 'earlier' = doar date mai devreme decat programarea curenta (clasic);
        # 'any'     = muta programarea in lunile alese chiar daca e mai tarziu,
        #             apoi imbunatateste doar spre mai devreme.
        "auto_update_mode": (config.get("auto_update_mode") or "earlier").strip().lower(),
    }


async def run_with_config(config: dict, stop_event=None):
    """Ruleaza monitorul cu configuratie din dict."""
    _apply_config(config)

    # Avertizare daca tinta auto-update nu e printre locatiile scrapuite —
    # altfel auto-update nu va avea niciodata date pentru locatia respectiva.
    if FORM_DATA["auto_update_enabled"]:
        _tl = _normalize_loc(FORM_DATA.get("target_location", ""))
        _sls = [_normalize_loc(s) for s in FORM_DATA.get("scrape_locations", []) if s]
        if _tl and _sls and not any(_tl in s or s in _tl for s in _sls):
            print("  [AU] ATENTIE: locatia tinta auto-update nu e in lista de locatii scrapuite "
                  "- bifeaz-o ca sa fie verificata.")

    # Chromium e necesar DOAR pentru auto-update (rebooking) si pentru
    # fallback-ul ASP_FORCE_BROWSER_CHECK=1. Verificarea normala merge direct
    # pe API (do_check_api) - fara browser, fara boot de Blazor WASM (care pe
    # Render free depasea si 180s de CPU si omora verificarile in lant).
    # Lansarea/inchiderea lui stau la nivel de modul (new_page/close_browser),
    # ca monitorul partajat din web.py sa foloseasca exact acelasi setup.

    force_browser_check = (os.environ.get("ASP_FORCE_BROWSER_CHECK", "")
                           .strip().lower() in ("1", "true", "yes"))
    target_months = [m.lower() for m in FORM_DATA["target_months"]]
    pretty_months = ", ".join(m.capitalize() for m in target_months)

    print("=" * 60)
    print("  ASP Exam Checker - Monitor automat")
    print(f"  Verifica la fiecare {INTERVAL_MINUTE} minute")
    print(f"  Notificari Telegram: {'DA' if TELEGRAM_BOT_TOKEN else 'NECONFIGURATE'}")
    print("=" * 60)

    # Mesaj de pornire pe Telegram
    await send_telegram(
        f"✅ <b>ASP Monitor pornit!</b>\n"
        f"Verific la fiecare {INTERVAL_MINUTE} minute.\n"
        f"Te notific imediat daca gasesc locuri in: {pretty_months} (2026). 🔍"
    )

    check_count = 0  # Flag - oprim notificarile dupa primul mesaj
    selector_issue_active = False
    consecutive_failures = 0  # backoff: nu bombarda site-ul cand ne blocheaza

    creds_path = config.get("_credentials_file")

    while True:
        # Check if stop_event was triggered (from GUI)
        if stop_event and stop_event.is_set():
            print("\n  Monitor oprit.")
            break

        # Hot-reload: reciteste setarile salvate din UI (luni, interval, mod
        # auto-update...) ca sa se aplice de la acest ciclu, fara restart.
        if creds_path and os.path.exists(creds_path):
            try:
                with open(creds_path, encoding="utf-8") as f:
                    fresh = json.load(f)
                fresh["_credentials_file"] = creds_path
                _apply_config(fresh)
                config = fresh
            except Exception as e:
                print(f"  [!] Reload config esuat: {e}")
        target_months = [m.lower() for m in FORM_DATA["target_months"]]
        pretty_months = ", ".join(m.capitalize() for m in target_months)

        check_count += 1
        print(f"\n>>> Verificare #{check_count} <<<")

        if force_browser_check:
            # Fallback: vechea verificare prin Chromium (daca API-ul dispare).
            context, page = await new_page()
            try:
                results, diagnostics = await do_check(page)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                await close_browser()
        else:
            results, diagnostics = await do_check_api()

        # ── Analizeaza rezultatele ────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  SUMAR #{check_count} - {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        print(f"{'='*60}")

        disponibil_pe_luna = {m: [] for m in target_months}
        toate_disponibile  = []

        # Cand auto-update e activ, notificarile TG arata doar zilele mai devreme
        # decat programarea curenta (restul nu ne intereseaza). Consola ramane completa.
        au_current_dt = None
        if FORM_DATA.get("auto_update_enabled"):
            au_current_dt = parse_current_appointment_date(
                FORM_DATA.get("current_appointment_date", ""))

        au_mode = FORM_DATA.get("auto_update_mode", "earlier")

        def _in_watched(dt):
            return (dt is not None
                    and RO_MONTHS_FULL[dt.month - 1] in target_months)

        # In modul 'any', cand programarea curenta e IN AFARA lunilor alese,
        # tinta e mutarea in lunile alese -> arata toate zilele gasite pe TG,
        # nu doar cele mai devreme decat programarea curenta.
        tg_cutoff = au_current_dt
        if au_mode == "any" and not _in_watched(au_current_dt):
            tg_cutoff = None

        for loc, months in results.items():
            for month, days in months.items():
                if days:
                    tg_days = _earlier_than(days, month, tg_cutoff)
                    if tg_days:
                        toate_disponibile.append((loc, month, tg_days))
                        if month in disponibil_pe_luna:
                            disponibil_pe_luna[month].append((loc, tg_days))
                    print(f"  [+] {loc} - {month.capitalize()}: {len(days)} zile")
                    for d in days:
                        print(f"      - {d}")
                else:
                    print(f"  [-] {loc} - {month.capitalize()}: nicio zi")

        selector_issue = (
            diagnostics["locations_found"] == 0
            or diagnostics["calendar_unavailable_count"] >= 2
            or diagnostics["had_check_error"]
        )

        if selector_issue:
            consecutive_failures += 1
            diagnostic_text = (
                "Daca vezi 'Locatii gasite: 0', 'Calendar indisponibil' repetat "
                "sau 'EROARE in verificare', atunci problema este la logica/selectori scraper."
            )
            print(f"  [DIAG] {diagnostic_text}")
            print(
                "  [DIAG] "
                f"Locatii={diagnostics['locations_found']}, "
                f"Calendar indisponibil={diagnostics['calendar_unavailable_count']}, "
                f"Eroare={'DA' if diagnostics['had_check_error'] else 'NU'}"
            )

            # Alerta la primul esec + re-alerta la fiecare 10 esecuri la rand
            # (cu backoff 15 min asta inseamna ~o data la 2.5h cat timp e mort)
            # ca o pana de cateva zile sa nu insemne un singur mesaj usor de ratat.
            if not selector_issue_active or consecutive_failures % 10 == 0:
                await send_telegram(
                    "🛑 <b>MONITORUL NU POATE SCANA SITE-UL ASP!</b>\n"
                    f"Verificari esuate la rand: <b>{consecutive_failures}</b>.\n"
                    "⚠️ NU primesti notificari de locuri cat timp dureaza asta - "
                    "NU inseamna ca nu sunt locuri libere!\n"
                    f"Detalii: Locatii={diagnostics['locations_found']}, "
                    f"Calendar indisponibil={diagnostics['calendar_unavailable_count']}, "
                    f"Eroare={'DA' if diagnostics['had_check_error'] else 'NU'}\n"
                    f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                )
                selector_issue_active = True
        else:
            if selector_issue_active:
                await send_telegram(
                    "✅ <b>Scanarea functioneaza din nou!</b>\n"
                    f"Dupa {consecutive_failures} verificari esuate, monitorul "
                    "a reusit iar sa citeasca calendarul. Notificarile de locuri merg normal.\n"
                    f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                )
            selector_issue_active = False
            consecutive_failures = 0

        # ── Notificari Telegram ───────────────────────────────────
        if check_count == 1:
            # Prima verificare: trimite tot indiferent de rezultat (test bot)
            msg_lines = ["🔍 <b>ASP Monitor - Prima verificare</b>", ""]
            if toate_disponibile:
                for loc, month, days in toate_disponibile:
                    msg_lines.append(f"📍 <b>{loc}</b> - {month.capitalize()}")
                    for d in days:
                        msg_lines.append(f"   • {d}")
                    msg_lines.append("")
            elif selector_issue:
                # NU spune "nicio zi disponibila" cand de fapt scanarea a esuat -
                # exact asa a parut ca "totul e ok" in timp ce monitorul era mort.
                msg_lines.append("🛑 Prima verificare a ESUAT (problema tehnica, nu lipsa de locuri).")
                msg_lines.append("Vezi alerta separata - reincerc automat.")
            else:
                msg_lines.append("Nicio zi disponibila momentan.")
                msg_lines.append(f"Te notific cand apare ceva in: {pretty_months}. 👀")
            msg_lines.append(f"\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            await send_telegram("\n".join(msg_lines))

        elif any(disponibil_pe_luna.values()):
            # Urmatoarele verificari: notifica pentru ORICARE luna din target_months
            msg_lines = ["🚨 <b>LOCURI DISPONIBILE IN LUNILE URMARITE!</b>", ""]
            for month in target_months:
                entries = disponibil_pe_luna[month]
                if not entries:
                    continue
                msg_lines.append(f"📅 <b>{month.capitalize()}</b>")
                for loc, days in entries:
                    msg_lines.append(f"📍 <b>{loc}</b>")
                    for d in days:
                        msg_lines.append(f"   • {d}")
                    msg_lines.append("")
            msg_lines.append(f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            msg_lines.append("🔗 https://eservicii.gov.md/asp/dimtcca/cerere/apo01")
            await send_telegram("\n".join(msg_lines))

        else:
            print(f"  Nicio zi in lunile urmarite ({pretty_months}). Urmatoarea verificare in {INTERVAL_MINUTE} minute...")

        # ── Auto-update: rebook if earlier date found at target location ──
        if FORM_DATA.get("auto_update_enabled") and results:
            current_dt = parse_current_appointment_date(FORM_DATA.get("current_appointment_date", ""))
            earliest = find_earliest_available(results, FORM_DATA.get("target_location", ""))
            should_rebook = False
            if current_dt and earliest:
                if au_mode == "any" and not _in_watched(current_dt):
                    # programarea curenta e in afara lunilor alese -> orice zi
                    # din lunile alese e o tinta valida (chiar mai tarzie)
                    should_rebook = earliest[1] != current_dt
                else:
                    should_rebook = earliest[1] < current_dt

            if not current_dt:
                print("  [AU] 'Data programare curenta' lipseste sau invalida - skip auto-update.")
            elif not earliest:
                pass  # nothing better
            elif not should_rebook:
                print(f"  [AU] Cea mai devreme data ({earliest[1].strftime('%d.%m.%Y')}) nu e mai buna decat cea curenta ({current_dt.strftime('%d.%m.%Y')}) [mod={au_mode}].")
            else:
                loc_label, new_dt = earliest
                print(f"  [AU] Data mai buna gasita: {new_dt.strftime('%d.%m.%Y')} la {loc_label}. Pornesc auto-update...")
                await send_telegram(
                    f"🔄 <b>Auto-update pornit</b>\n"
                    f"Data noua: <b>{new_dt.strftime('%d.%m.%Y')}</b>\n"
                    f"Locatie: {loc_label}\n"
                    f"Inlocuieste programarea din {current_dt.strftime('%d.%m.%Y')}."
                )
                ok = False
                au_ctx = None
                au_out = {}
                try:
                    au_ctx, _au_page = await new_page()
                    ok = await auto_update_appointment(au_ctx, FORM_DATA, new_dt,
                                                       out=au_out)
                except Exception as e:
                    print(f"  [AU] Browserul pentru auto-update nu a pornit: {e}")
                    await send_telegram(
                        f"❌ Auto-update: browserul nu a putut porni ({e}). "
                        f"Reincerc la urmatoarea verificare.")
                finally:
                    if au_ctx is not None:
                        try:
                            await au_ctx.close()
                        except Exception:
                            pass
                    # Rebooking-ul e rar - inchidem Chromium ca sa nu tina
                    # ~200MB ocupati pana data viitoare (Render free = 512MB).
                    await close_browser()
                if ok:
                    # Update cached current date so we don't re-trigger on the same slot next cycle
                    new_str = new_dt.strftime("%d.%m.%Y")
                    FORM_DATA["current_appointment_date"] = new_str
                    if au_out.get("code"):
                        FORM_DATA["appointment_code"] = au_out["code"]
                    if au_out.get("request_number"):
                        FORM_DATA["request_number"] = au_out["request_number"]
                    _persist_current_date(config, new_str, au_out.get("code", ""),
                                          au_out.get("request_number", ""))

        # ── Asteapta inainte de urmatoarea verificare ─────────────
        # Backoff: 3+ esecuri consecutive = probabil blocati de WAF/site cazut.
        # Verificarile dese doar prelungesc blocarea -> incetinim la 15 min
        # pana la primul succes.
        wait_minutes = INTERVAL_MINUTE
        if consecutive_failures >= 3:
            wait_minutes = max(INTERVAL_MINUTE, 15)
            print(f"\n  [BACKOFF] {consecutive_failures} verificari esuate la rand - "
                  f"astept {wait_minutes} minute (protejam IP-ul de blocare).")

        print(f"\n  Urmatoarea verificare in {wait_minutes} minute...")
        print("  (Opreste din interfata / Ctrl+C pentru a opri)")

        for remaining in range(wait_minutes * 60, 0, -60):
            if stop_event and stop_event.is_set():
                break
            for _ in range(60):
                if stop_event and stop_event.is_set():
                    break
                await asyncio.sleep(1)
            if not (stop_event and stop_event.is_set()):
                print(f"  {remaining//60 - 1} minute pana la urmatoarea verificare...")

    # Iesire prin stop_event -> curata Chromium daca a fost pornit (auto-update)
    await shutdown_browser()


if __name__ == "__main__":
    # Load credentials.json from same directory
    creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    if os.path.exists(creds_file):
        with open(creds_file, encoding='utf-8') as f:
            config = json.load(f)
        asyncio.run(run_with_config(config))
    else:
        print("EROARE: credentials.json nu a fost gasit.")
        print(f"Cauta in: {creds_file}")