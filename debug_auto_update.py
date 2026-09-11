"""
Debug pas-cu-pas pentru auto_update_appointment (dist/scrapper.py).

Ruleaza EXACT aceiasi selectori ca auto_update_appointment, dar:
  - logheaza diagnostice la fiecare pas (ce gaseste / ce nu gaseste)
  - face screenshot la fiecare pas in debug_out/
  - inainte de click pe PROGRAMEAZA-TE blocheaza TOATE requesturile
    POST/PUT/PATCH/DELETE catre eservicii.gov.md, deci programarea
    reala NU poate fi modificata. Logheaza requestul blocat (URL + body)
    ca sa vedem ce s-ar fi trimis si ce pasi urmeaza dupa buton.

NU trimite nimic pe Telegram.
"""

import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "debug_out")
os.makedirs(OUT, exist_ok=True)

# import dist/scrapper.py ca modul, ca sa testam functiile lui reale
spec = importlib.util.spec_from_file_location("scrapper", os.path.join(HERE, "dist", "scrapper.py"))
scrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrapper)

from playwright.async_api import async_playwright  # noqa: E402

import re  # noqa: E402

APPOINTMENTS_URL = "https://eservicii.gov.md/asp/dimtcca/APO/my-appointments"

RO_MONTHS_FULL = scrapper.RO_MONTHS_FULL

shot_idx = 0
async def shot(page, name):
    global shot_idx
    shot_idx += 1
    path = os.path.join(OUT, f"{shot_idx:02d}_{name}.png")
    try:
        await page.screenshot(path=path, full_page=False)
        print(f"      [shot] {os.path.basename(path)}")
    except Exception as e:
        print(f"      [shot] FAILED {name}: {e}")


async def main():
    with open(os.path.join(HERE, "dist", "credentials.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    idnp = cfg["idnp"].strip()
    code = cfg["appointment_code"].strip()
    req  = cfg["request_number"].strip()
    print(f"[*] Credentiale: idnp={idnp[:4]}..., code={code[:6]}..., req={req[:4]}...")

    blocked_requests = []
    api_log = []
    # mode: "off" = totul permis; "allow_validate" = doar validate-appointment trece;
    # "block_all" = orice POST/PUT blocat
    arm_block = {"mode": "off"}

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(viewport={"width": 1500, "height": 950})

    async def route_handler(route):
        r = route.request
        is_write = r.method in ("POST", "PUT", "PATCH", "DELETE") and "eservicii.gov.md" in r.url
        if is_write and arm_block["mode"] != "off":
            if arm_block["mode"] == "allow_validate" and "validate-appointment" in r.url:
                print(f"  [ALLOW] {r.method} {r.url}")
                await route.continue_()
                return
            blocked_requests.append((r.method, r.url, r.post_data))
            print(f"  [BLOCK] {r.method} {r.url}")
            if r.post_data:
                print(f"          body: {r.post_data[:500]}")
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_handler)

    def on_request(r):
        if r.resource_type in ("xhr", "fetch") and "eservicii.gov.md" in r.url:
            api_log.append(f"{r.method} {r.url}" + (f" body={r.post_data[:300]}" if r.post_data else ""))
    context.on("request", on_request)

    async def on_response(resp):
        if "api/apo-request" in resp.url:
            try:
                body = (await resp.text())[:600]
            except Exception:
                body = "<necitit>"
            print(f"  [RESP] {resp.status} {resp.url}")
            print(f"         {body}")
    context.on("response", lambda resp: asyncio.ensure_future(on_response(resp)))

    page = await context.new_page()

    # ── Pas 1-3: cautare (selectorii exacti din auto_update_appointment) ──
    print("\n[1] goto my-appointments...")
    await page.goto(APPOINTMENTS_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(800)
    await shot(page, "my_appointments")

    inputs = page.locator('input[type="text"]:visible, input:not([type]):visible')
    n = await inputs.count()
    print(f"[2] inputuri vizibile gasite: {n}")
    for i in range(n):
        el = inputs.nth(i)
        ph = await el.get_attribute("placeholder") or ""
        nm = await el.get_attribute("name") or ""
        idd = await el.get_attribute("id") or ""
        print(f"      input[{i}]: name='{nm}' id='{idd}' placeholder='{ph}'")
    if n < 3:
        inputs = page.locator("input:visible")
        n = await inputs.count()
        print(f"      fallback input:visible -> {n}")

    await inputs.nth(0).fill(idnp)
    await inputs.nth(1).fill(code)
    await inputs.nth(2).fill(req)
    await page.wait_for_timeout(300)

    print("[3] click CAUTARE...")
    btn = page.locator("button", has_text=re.compile(r"C[ĂA]UTARE", re.IGNORECASE)).first
    print(f"      buton cautare vizibil: {await btn.is_visible()}")
    await btn.click()
    await page.wait_for_timeout(2500)
    await shot(page, "after_cautare")

    try:
        err = page.locator("text=/Verifica.i corectitudinea/i").first
        if await err.is_visible(timeout=1500):
            print("      !! SNACKBAR EROARE: 'Verificati corectitudinea datelor'")
    except Exception:
        pass

    try:
        await page.wait_for_selector("table tbody tr", timeout=5000)
        rows = await page.locator("table tbody tr").count()
        print(f"      tabel OK, {rows} rand(uri)")
    except Exception:
        print("      !! TABELUL NU A APARUT — aici pica pasul de cautare")
        await shot(page, "search_failed")
        await browser.close(); await pw.stop(); return
    try:
        print(f"      rand 1: {(await page.locator('table tbody tr').first.inner_text(timeout=3000))[:200]}")
    except Exception as e:
        print(f"      (nu am putut citi textul randului: {type(e).__name__})")

    # ── Pas 4: Modifica → DA ──
    print("[4] click MODIFICA...")
    mod = page.locator("button", has_text=re.compile(r"MODIFIC[ĂA]", re.IGNORECASE)).first
    print(f"      buton Modifica vizibil: {await mod.is_visible()}")
    await mod.click()
    await page.wait_for_timeout(800)
    await shot(page, "after_modifica")

    da = page.locator("button", has_text=re.compile(r"^\s*DA\s*$", re.IGNORECASE)).first
    da_visible = False
    try:
        da_visible = await da.is_visible(timeout=2000)
    except Exception:
        pass
    print(f"      buton DA vizibil: {da_visible}")
    if not da_visible:
        # diagnoza: ce butoane exista in dialog?
        all_btns = await page.locator("button:visible").all()
        print("      butoane vizibile pe pagina:")
        for b in all_btns[:20]:
            try:
                print(f"        - '{(await b.inner_text()).strip()}'")
            except Exception:
                pass
        await browser.close(); await pw.stop(); return
    await da.click()

    try:
        await page.wait_for_url("**/cerere/**", timeout=30000)
        print(f"      URL dupa DA: {page.url}")
    except Exception:
        print(f"      !! Nu a navigat la /cerere/. URL actual: {page.url}")
    await page.wait_for_timeout(2000)
    await shot(page, "edit_page")

    # ── Pas 5: deschide calendarul ──
    print("[5] deschid date picker...")
    picker_btn = page.locator('button[aria-label="Open Date Picker"]')
    cnt = await picker_btn.count()
    print(f"      'Open Date Picker' gasit: {cnt}")
    if cnt == 0:
        all_btns = await page.locator("button:visible").all()
        print("      butoane vizibile (aria-label / text):")
        for b in all_btns[:25]:
            try:
                al = await b.get_attribute("aria-label") or ""
                tx = (await b.inner_text()).strip()[:40]
                print(f"        - aria='{al}' text='{tx}'")
            except Exception:
                pass
        await browser.close(); await pw.stop(); return

    await picker_btn.first.click()
    await page.wait_for_timeout(1200)
    await shot(page, "picker_open")

    # Testez FIXUL: _pick_date_in_popup din scrapper.py, cu tinta = EXACT
    # slotul curent (08 iulie), deci worst case = programare identica.
    cur_dt = scrapper.parse_current_appointment_date(cfg.get("current_appointment_date", ""))
    print(f"      testez _pick_date_in_popup({cur_dt.strftime('%d.%m.%Y')})...")
    ok = await scrapper._pick_date_in_popup(page, cur_dt)
    print(f"      _pick_date_in_popup -> {ok}")
    if not ok:
        print("      !! FIXUL NU MERGE")
        await shot(page, "fix_failed")
        await browser.close(); await pw.stop(); return
    await page.wait_for_timeout(2000)
    await shot(page, "date_picked")
    # s-a inchis pickerul? ce scrie in campul Data?
    try:
        data_val = await page.locator("input").first.input_value()
    except Exception:
        data_val = "?"
    picker_still_open = await page.locator(".fod-picker-paper").first.is_visible() if await page.locator(".fod-picker-paper").count() else False
    print(f"      picker inca deschis: {picker_still_open}")
    inputs_on_page = await page.locator("input:visible").all()
    for el in inputs_on_page[:5]:
        nm = await el.get_attribute("name") or ""
        try:
            vv = await el.input_value()
        except Exception:
            vv = "?"
        print(f"      input name='{nm}' value='{vv}'")

    # ── Pas 6: Ora (selectorii exacti din auto_update_appointment) ──
    print("[6] selectez Ora...")
    all_selects = await page.locator("select").all()
    print(f"      <select> pe pagina: {len(all_selects)}")
    for s in all_selects:
        nm = await s.get_attribute("name") or ""
        vis = await s.is_visible()
        opts = await s.locator("option").all_inner_texts()
        print(f"        - name='{nm}' visible={vis} options={opts}")

    # SIGURANTA: alegem EXACT ora programarii curente (10:00), nu cea mai devreme
    picked_hour = None
    try:
        ora_select = page.locator('select[name*="Time"], select[name*="Hour"], select[name*="Ora"]').first
        if not await ora_select.is_visible(timeout=2000):
            print("      selectorul name*=Time/Hour/Ora NU e vizibil -> fallback select:visible last")
            ora_select = page.locator("select:visible").last
        options = await ora_select.locator("option").all()
        for opt in options:
            val = await opt.get_attribute("value") or ""
            txt = (await opt.inner_text()).strip()
            if not val or not txt or "selecta" in txt.lower():
                continue
            if txt.strip() != "10:00":
                continue
            await ora_select.select_option(value=val)
            picked_hour = txt
            break
        if picked_hour is None:
            print("      !! ora 10:00 (cea curenta) nu e in lista — ma opresc (nu risc alta ora)")
            await browser.close(); await pw.stop(); return
    except Exception as e:
        print(f"      !! eroare selectare Ora: {e}")
        await browser.close(); await pw.stop(); return
    print(f"      ora aleasa: {picked_hour}")
    await page.wait_for_timeout(500)
    await shot(page, "hour_picked")

    # ── Pas 7: blochez ORICE write — doar verificam ca butonul declanseaza requestul ──
    print("\n[7] Mod 'block_all': orice POST e blocat, nimic nu se trimite.")
    arm_block["mode"] = "block_all"
    api_log.clear()

    prog = page.locator("button", has_text=re.compile(r"PROGRAMEAZ", re.IGNORECASE)).first
    print(f"      buton Programeaza-te vizibil: {await prog.is_visible()}")
    await prog.click()
    await page.wait_for_timeout(4000)
    await shot(page, "after_programeaza_click")

    print(f"\n[8] DUPA CLICK pe Programeaza-te:")
    print(f"      URL: {page.url}")
    # dialoguri / textul vizibil relevant
    for sel in ["[role=dialog]", ".fod-dialog", ".fod-modal", ".modal"]:
        loc = page.locator(sel)
        if await loc.count() > 0 and await loc.first.is_visible():
            print(f"      DIALOG ({sel}): {(await loc.first.inner_text())[:400]}")
    vis_btns = await page.locator("button:visible").all()
    print("      butoane vizibile acum:")
    for b in vis_btns[:20]:
        try:
            print(f"        - '{(await b.inner_text()).strip()[:50]}'")
        except Exception:
            pass
    snack = page.locator(".fod-snackbar, [class*=snackbar], [class*=toast]")
    if await snack.count() > 0:
        try:
            print(f"      snackbar: {(await snack.first.inner_text())[:200]}")
        except Exception:
            pass

    # Daca a aparut un pas nou (Finalizare / dialog cu buton de confirmare),
    # il apasam cu TOTUL blocat ca sa vedem ce request ar fi trimis.
    print("\n[9] Caut un buton de pas urmator (Finalizeaza/Confirma/DA)...")
    arm_block["mode"] = "block_all"
    next_btn = None
    for pat in [r"FINALIZ", r"CONFIRM", r"^\s*DA\s*$", r"TRIMITE", r"SALV"]:
        loc = page.locator("button", has_text=re.compile(pat, re.IGNORECASE)).first
        try:
            if await loc.is_visible(timeout=1000):
                next_btn = loc
                print(f"      gasit buton '{(await loc.inner_text()).strip()}' — il apas cu TOT blocat")
                break
        except Exception:
            continue
    if next_btn:
        await next_btn.click()
        await page.wait_for_timeout(4000)
        await shot(page, "after_next_step_click")
        print(f"      URL: {page.url}")
        vis2 = await page.locator("button:visible").all()
        print("      butoane vizibile acum:")
        for b in vis2[:20]:
            try:
                print(f"        - '{(await b.inner_text()).strip()[:50]}'")
            except Exception:
                pass
    else:
        print("      niciun buton de pas urmator gasit")

    print(f"\n      requesturi BLOCATE ({len(blocked_requests)}):")
    for m, u, b in blocked_requests:
        print(f"        {m} {u}")
        if b:
            print(f"          body: {b[:800]}")
    print(f"\n      requesturi xhr/fetch dupa click ({len(api_log)}):")
    for line in api_log[-15:]:
        print(f"        {line}")

    print("\n[*] Las browserul deschis 90s pentru inspectie manuala (NIMIC nu se mai trimite)...")
    await page.wait_for_timeout(90000)
    await browser.close()
    await pw.stop()
    print("[*] Gata. Screenshots in debug_out/")


if __name__ == "__main__":
    asyncio.run(main())
