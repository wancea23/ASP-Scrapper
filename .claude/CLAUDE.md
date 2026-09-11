# ASP Exam Checker — Project Guide

Automated checker for practical driving exam slots at DECA Chișinău. GUI (customtkinter) + Playwright + Telegram notifications. Builds to a Windows `.exe` with PyInstaller.

---

## Structure

```
ASP Scrapper/
├── app.py                      # customtkinter GUI — entry point
├── launcher.py                 # legacy .exe launcher (scrapper.py shim)
├── build.bat                   # PyInstaller build script
├── dist/
│   ├── scrapper.py            # Playwright automation logic
│   └── ASP Exam Checker.exe   # built executable
└── .claude/
    └── CLAUDE.md               # this file
```

- **`app.py`** — GUI. Loads `dist/scrapper.py` via `importlib`. Reads/writes `credentials.json` with `encoding='utf-8'` + `ensure_ascii=False` so Romanian diacritics (Ș, Ț, Ă, Î, Â) survive. All placeholders are generic (`"Prenume"`, `"07xxxxxxxx"`, etc.) — no personal data.
- **`dist/scrapper.py`** — async Playwright. Fills the ASP form at `https://eservicii.gov.md/asp/dimtcca/cerere/apo01`, then polls two DECA Chișinău locations in a 15-minute loop. When `sys.frozen` is True it sets `PLAYWRIGHT_BROWSERS_PATH` to `~\AppData\Local\ms-playwright` so the packaged .exe finds Chromium.
- **`build.bat`** — `pip install pyinstaller customtkinter playwright aiohttp --quiet` → `python -m playwright install chromium` → `PyInstaller --onefile --windowed --collect-all=playwright app.py`. Keeps `dist/scrapper.py` beside the .exe.

---

## Calendar-Reload Detection (current design)

The ASP site uses an Angular custom `<select>` (`.fod-*` classes). Switching location does not always trigger a calendar re-fetch. Real reloads show a ~0.1s black flash; stale state leaves the calendar frozen (e.g. June renders empty even when slots exist).

Three helpers in `dist/scrapper.py`:

1. **`force_select_location(page, location)`** — native `page.select_option(value=...)` with `label=...` fallback. (Replaced an older, unreliable `page.evaluate` + manual `dispatchEvent` approach.)
2. **`_calendar_signature(page)`** — returns `len + '|' + first_300 + '|' + last_300` of `.fod-picker-static` innerHTML, or `None` if no calendar.
3. **`try_select_and_detect_reload(page, location, verbose=True)`** — captures `before_sig`, selects, polls every 200ms for up to 10s. Returns `True` on any of: JS context reset (full navigation), calendar appeared where there was none, signature changed. Otherwise `False`.
4. **`select_location_with_reload_check`** — up to **8 attempts**. Between attempts it calls `try_select_and_detect_reload(..., verbose=False)` on the **alternate** location, so the next attempt on the target starts from a genuinely different calendar state (makes signature change detection definitive).

If 8 attempts fail: prints `Calendar indisponibil (reload esuat dupa 8 incercari)`.

**Manual smoke test:** after a cycle, open the visible browser → navigate calendar to a month the user knows has availability (e.g. June) → if days are clickable, reload worked.

---

## Common Issues

| Issue | Fix |
|---|---|
| `.exe` crashes with missing Chromium path | Make sure `sys.frozen` branch sets `PLAYWRIGHT_BROWSERS_PATH`. User needs `python -m playwright install chromium` installed on the machine. |
| Diacritics render as `?` | All `open()` calls on JSON must use `encoding='utf-8'`; all `json.dump` must use `ensure_ascii=False`. |
| "Pagina s-a reincarcat!" but calendar still stale | The old MutationObserver approach gave false positives. Ensure signature-based detection is in use (see above). |
| Location dropdown doesn't change | Use native `page.select_option` — not manual dispatched events. |

---

## Workflows

```bash
# Run from source (user's default dev loop)
python app.py

# Build .exe
build.bat
```

---

## Rules for Future Edits

- **Do not hardcode months.** June works today; next month it won't. Detection must be month-agnostic.
- **Do not revert to MutationObserver-based reload detection** — it produced false positives that shipped as `[OK] Pagina s-a reincarcat!` on tooltip/class flips.
- **No personal data in code or placeholders** — IDNP, name, phone, email, ID series must come from `credentials.json`, never from defaults.
- **Read before editing** `dist/scrapper.py` and `app.py` — both are large and easy to corrupt with blind edits.
- **Git:** commit only when asked. Clear messages (`Fix: …`, `Add: …`).

---

## Recent State (2026-04)

- Signature-based detection + 8-retry alternate-switch loop is in place at `dist/scrapper.py` (`_calendar_signature`, `try_select_and_detect_reload`, `select_location_with_reload_check`).
- User runs from source (`python app.py`); `.exe` in `dist/` is older and needs rebuild via `build.bat` once behavior is confirmed.
- Pending confirmation: whether the signature approach reliably catches real reloads. Fallback if not: detect the ~0.1s black flash element directly (likely a CDK overlay or `.fod-loading` indicator).
