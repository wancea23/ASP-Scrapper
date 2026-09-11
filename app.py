"""
ASP Exam Checker - GUI Application
Minimalist blueish interface with embedded console and credentials form.
"""

import customtkinter as ctk
import json
import os
import sys
import threading
import asyncio
import queue
from pathlib import Path
from datetime import datetime

# These imports are needed so PyInstaller bundles them for scrapper.py
import aiohttp  # noqa: F401
import playwright  # noqa: F401
import playwright.async_api  # noqa: F401

# Import scrapper module - dynamically to handle both frozen and non-frozen
import importlib.util

# Get base path (where app.py or .exe is located)
if getattr(sys, 'frozen', False):
    # Running as .exe - scrapper.py is in same folder as .exe
    base_path = os.path.dirname(sys.executable)
else:
    # Running from source - scrapper.py is in dist/ subfolder
    base_path = os.path.dirname(__file__)

# Try both locations: dist/scrapper.py (dev) and scrapper.py (built)
scrapper_paths = [
    os.path.join(base_path, "dist", "scrapper.py"),
    os.path.join(base_path, "scrapper.py"),
]

scrapper = None
for scrapper_path in scrapper_paths:
    if os.path.exists(scrapper_path):
        spec = importlib.util.spec_from_file_location("scrapper", scrapper_path)
        if spec and spec.loader:
            scrapper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scrapper)
            break

if scrapper is None:
    raise RuntimeError(f"Could not find scrapper.py in {scrapper_paths}")

# Configure theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Determine credentials file location (next to .exe or script)
if getattr(sys, "frozen", False):
    CREDENTIALS_FILE = os.path.join(os.path.dirname(sys.executable), "credentials.json")
else:
    CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

# Default credentials structure - with your actual data pre-filled
DEFAULT_CREDENTIALS = {
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
    "telegram_token": "",
    "telegram_chat_id": "",
    "interval_minutes": 5,
    "target_months": ["aprilie"],
    "scrape_locations": [
        "DECA Chișinău (str. Acad. S. Rădăuțanu, 1)",
        "DECA Chișinău (str. Calea Ieșilor, 14)",
    ],
    "auto_update_enabled": False,
    "auto_update_idnp": "",
    "appointment_code": "",
    "request_number": "",
    "target_location": "",
    "current_appointment_date": "",
}

RO_MONTHS = [
    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie"
]

# Known DECA Chișinău exam locations (must match the label substring used by scrapper).
# Used both for the "scrape locations" multi-select and the auto-update target dropdown.
KNOWN_LOCATIONS = [
    "DECA Chișinău (str. Acad. S. Rădăuțanu, 1)",
    "DECA Chișinău (str. Calea Ieșilor, 14)",
    "DECA Chișinău (str. Salcâmilor, 28)",
]

# Default set of locations to scrape when nothing is saved yet (Salcâmilor off by default).
DEFAULT_SCRAPE_LOCATIONS = [
    "DECA Chișinău (str. Acad. S. Rădăuțanu, 1)",
    "DECA Chișinău (str. Calea Ieșilor, 14)",
]


class QueueWriter:
    """Custom writer to redirect stdout to a queue."""
    def __init__(self, queue_obj):
        self.queue = queue_obj

    def write(self, text):
        if text.strip():
            self.queue.put(text)

    def flush(self):
        pass


class ASPCheckerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ASP Exam Checker")
        self.geometry("1000x700")

        # Try to set icon
        icon_path = os.path.join(os.path.dirname(__file__), "asp_logo.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass  # Icon not critical

        # State
        self.is_running = False
        self.monitor_thread = None
        self.stop_event = None
        self.output_queue = queue.Queue()
        self.original_stdout = sys.stdout

        # Main container with two columns
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        # Left panel - Settings
        self.create_settings_panel()

        # Right panel - Console
        self.create_console_panel()

        # Bottom panel - Controls
        self.create_control_panel()

        # Load saved credentials
        self.load_credentials()

        # Start polling console output
        self.poll_output()

    def create_settings_panel(self):
        """Left panel with credentials form."""
        settings_frame = ctk.CTkFrame(self)
        settings_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        # Title
        title = ctk.CTkLabel(settings_frame, text="SETARI", font=("Arial", 14, "bold"))
        title.pack(pady=(0, 10))

        # Scrollable area for settings
        scrollable = ctk.CTkScrollableFrame(settings_frame, fg_color="transparent")
        scrollable.pack(fill="both", expand=True)

        # IDNP
        ctk.CTkLabel(scrollable, text="IDNP:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.idnp_entry = ctk.CTkEntry(scrollable, placeholder_text="Ex: 1234567890123")
        self.idnp_entry.pack(fill="x", padx=10, pady=(0, 5))

        # Prenume / Nume (side by side)
        name_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        name_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(name_frame, text="Prenume:", font=("Arial", 10)).pack(side="left", padx=(0, 5))
        self.first_name_entry = ctk.CTkEntry(name_frame, placeholder_text="Prenume")
        self.first_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ctk.CTkLabel(name_frame, text="Nume:", font=("Arial", 10)).pack(side="left", padx=(0, 5))
        self.last_name_entry = ctk.CTkEntry(name_frame, placeholder_text="Nume")
        self.last_name_entry.pack(side="left", fill="x", expand=True)

        # Telefon / Email (side by side)
        contact_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        contact_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(contact_frame, text="Telefon:", font=("Arial", 10)).pack(side="left", padx=(0, 5))
        self.phone_entry = ctk.CTkEntry(contact_frame, placeholder_text="07xxxxxxxx")
        self.phone_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ctk.CTkLabel(contact_frame, text="Email:", font=("Arial", 10)).pack(side="left", padx=(0, 5))
        self.email_entry = ctk.CTkEntry(contact_frame, placeholder_text="email@example.com")
        self.email_entry.pack(side="left", fill="x", expand=True)

        # Seria buletin
        ctk.CTkLabel(scrollable, text="Seria/Nr. buletin:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.id_series_entry = ctk.CTkEntry(scrollable, placeholder_text="Ex: A12345678")
        self.id_series_entry.pack(fill="x", padx=10, pady=(0, 5))

        # Data buletin (day/month/year)
        ctk.CTkLabel(scrollable, text="Data buletin:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        date_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        date_frame.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(date_frame, text="Zi:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.id_date_day_entry = ctk.CTkEntry(date_frame, width=50, placeholder_text="ZZ")
        self.id_date_day_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(date_frame, text="Luna:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.id_date_month_entry = ctk.CTkEntry(date_frame, width=50, placeholder_text="LL")
        self.id_date_month_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(date_frame, text="An:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.id_date_year_entry = ctk.CTkEntry(date_frame, width=60, placeholder_text="AAAA")
        self.id_date_year_entry.pack(side="left")

        # Certificat medical
        ctk.CTkLabel(scrollable, text="Certificat medical:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.medical_cert_entry = ctk.CTkEntry(scrollable, placeholder_text="Ex: 12345/6789")
        self.medical_cert_entry.pack(fill="x", padx=10, pady=(0, 10))

        # Divider
        ctk.CTkLabel(scrollable, text="", font=("Arial", 5)).pack()

        # Telegram Token
        ctk.CTkLabel(scrollable, text="Telegram Bot Token:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.tg_token_entry = ctk.CTkEntry(scrollable, placeholder_text="123456:ABCdef...")
        self.tg_token_entry.pack(fill="x", padx=10, pady=(0, 5))

        # Telegram Chat ID
        ctk.CTkLabel(scrollable, text="Telegram Chat ID:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.tg_chat_id_entry = ctk.CTkEntry(scrollable, placeholder_text="1234567890")
        self.tg_chat_id_entry.pack(fill="x", padx=10, pady=(0, 5))

        # Interval minutes
        ctk.CTkLabel(scrollable, text="Interval verificare (minute):", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.interval_entry = ctk.CTkEntry(scrollable, placeholder_text="5")
        self.interval_entry.pack(fill="x", padx=10, pady=(0, 5))

        # Target months (multiple selection)
        ctk.CTkLabel(scrollable, text="Luni verificare:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 5))

        # Month checkboxes - use two columns
        months_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        months_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.month_vars = {}
        col1_frame = ctk.CTkFrame(months_frame, fg_color="transparent")
        col1_frame.pack(side="left", fill="both", expand=True)

        col2_frame = ctk.CTkFrame(months_frame, fg_color="transparent")
        col2_frame.pack(side="left", fill="both", expand=True)

        for i, month in enumerate(RO_MONTHS):
            var = ctk.BooleanVar(value=(month == "aprilie"))
            self.month_vars[month] = var

            cb = ctk.CTkCheckBox(col1_frame if i < 6 else col2_frame, text=month.capitalize(), variable=var)
            cb.pack(anchor="w", padx=5, pady=2)

        # Locatii de verificat (scrape) - selectie multipla
        ctk.CTkLabel(scrollable, text="Locatii de verificat:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 5))
        loc_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        loc_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.scrape_loc_vars = {}
        for loc in KNOWN_LOCATIONS:
            var = ctk.BooleanVar(value=(loc in DEFAULT_SCRAPE_LOCATIONS))
            self.scrape_loc_vars[loc] = var
            cb = ctk.CTkCheckBox(loc_frame, text=loc, variable=var, font=("Arial", 9))
            cb.pack(anchor="w", padx=5, pady=2)

        # ── Auto-update programare ──
        ctk.CTkLabel(scrollable, text="", font=("Arial", 5)).pack()
        ctk.CTkLabel(scrollable, text="── AUTO-UPDATE PROGRAMARE ──", font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        self.auto_update_var = ctk.BooleanVar(value=False)
        self.auto_update_cb = ctk.CTkCheckBox(
            scrollable, text="Activeaza auto-update programare", variable=self.auto_update_var
        )
        self.auto_update_cb.pack(anchor="w", padx=10, pady=(0, 5))

        ctk.CTkLabel(scrollable, text="IDNP auto-update (gol = la fel ca scraping):", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.auto_update_idnp_entry = ctk.CTkEntry(scrollable, placeholder_text="Lasa gol pentru a folosi IDNP-ul de mai sus")
        self.auto_update_idnp_entry.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(scrollable, text="Codul programarii (APO...):", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.appointment_code_entry = ctk.CTkEntry(scrollable, placeholder_text="APO0126...")
        self.appointment_code_entry.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(scrollable, text="Numarul cererii:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.request_number_entry = ctk.CTkEntry(scrollable, placeholder_text="3003...")
        self.request_number_entry.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(scrollable, text="Locatie tinta (auto-update):", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        self.target_location_menu = ctk.CTkOptionMenu(scrollable, values=KNOWN_LOCATIONS)
        self.target_location_menu.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(scrollable, text="Data programare curenta:", font=("Arial", 10)).pack(anchor="w", padx=10, pady=(10, 2))
        appt_date_frame = ctk.CTkFrame(scrollable, fg_color="transparent")
        appt_date_frame.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(appt_date_frame, text="Zi:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.appt_day_entry = ctk.CTkEntry(appt_date_frame, width=50, placeholder_text="ZZ")
        self.appt_day_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(appt_date_frame, text="Luna:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.appt_month_entry = ctk.CTkEntry(appt_date_frame, width=50, placeholder_text="LL")
        self.appt_month_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(appt_date_frame, text="An:", font=("Arial", 9)).pack(side="left", padx=(0, 5))
        self.appt_year_entry = ctk.CTkEntry(appt_date_frame, width=60, placeholder_text="AAAA")
        self.appt_year_entry.pack(side="left")

        # Save button
        save_btn = ctk.CTkButton(scrollable, text="[SALVEAZA]", command=self.save_credentials, fg_color="#4CAF50")
        save_btn.pack(pady=10)

    def create_console_panel(self):
        """Right panel with console output."""
        console_frame = ctk.CTkFrame(self)
        console_frame.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")

        # Title
        title = ctk.CTkLabel(console_frame, text="CONSOLA", font=("Arial", 14, "bold"))
        title.pack(pady=(0, 10))

        # Console text area
        self.console_text = ctk.CTkTextbox(
            console_frame,
            fg_color="#1a1a1a",
            text_color="#00ff00",
            font=("Courier New", 9)
        )
        self.console_text.pack(fill="both", expand=True)
        self.console_text.configure(state="disabled")

    def create_control_panel(self):
        """Bottom panel with Start/Stop button and status."""
        control_frame = ctk.CTkFrame(self)
        control_frame.grid(row=1, column=0, columnspan=2, padx=10, pady=10, sticky="ew")

        # Start/Stop button
        self.start_btn = ctk.CTkButton(
            control_frame,
            text="[>] PORNESTE MONITORUL",
            command=self.toggle_monitor,
            font=("Arial", 12, "bold"),
            fg_color="#2196F3",
            height=40
        )
        self.start_btn.pack(side="left", padx=(0, 20), fill="x", expand=True)

        # Status label
        status_frame = ctk.CTkFrame(control_frame, fg_color="transparent")
        status_frame.pack(side="right", padx=20)

        self.status_dot = ctk.CTkLabel(status_frame, text="[*]", text_color="#FF5252", font=("Arial", 12))
        self.status_dot.pack(side="left", padx=(0, 5))

        self.status_label = ctk.CTkLabel(status_frame, text="Oprit", font=("Arial", 11))
        self.status_label.pack(side="left")

    def load_credentials(self):
        """Load credentials from JSON file or use defaults."""
        # Always start with defaults
        creds = DEFAULT_CREDENTIALS.copy()

        # Try to load from file if it exists
        if os.path.exists(CREDENTIALS_FILE):
            try:
                with open(CREDENTIALS_FILE, encoding='utf-8') as f:
                    file_creds = json.load(f)
                    creds.update(file_creds)  # Override defaults with file data
            except Exception as e:
                print(f"[!] Eroare la citirea credentials: {e}")
        else:
            # File doesn't exist, create it with defaults
            try:
                with open(CREDENTIALS_FILE, "w", encoding='utf-8') as f:
                    json.dump(DEFAULT_CREDENTIALS, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[!] Nu s-a putut crea credentials.json: {e}")

        # Fill form
        self.idnp_entry.delete(0, "end")
        self.idnp_entry.insert(0, creds.get("idnp", ""))

        self.first_name_entry.delete(0, "end")
        self.first_name_entry.insert(0, creds.get("first_name", ""))

        self.last_name_entry.delete(0, "end")
        self.last_name_entry.insert(0, creds.get("last_name", ""))

        self.phone_entry.delete(0, "end")
        self.phone_entry.insert(0, creds.get("phone", ""))

        self.email_entry.delete(0, "end")
        self.email_entry.insert(0, creds.get("email", ""))

        self.id_series_entry.delete(0, "end")
        self.id_series_entry.insert(0, creds.get("id_series", ""))

        self.id_date_day_entry.delete(0, "end")
        self.id_date_day_entry.insert(0, str(creds.get("id_date_day", 1)))

        self.id_date_month_entry.delete(0, "end")
        self.id_date_month_entry.insert(0, str(creds.get("id_date_month", 1)))

        self.id_date_year_entry.delete(0, "end")
        self.id_date_year_entry.insert(0, str(creds.get("id_date_year", 2020)))

        self.medical_cert_entry.delete(0, "end")
        self.medical_cert_entry.insert(0, creds.get("medical_cert", ""))

        self.tg_token_entry.delete(0, "end")
        self.tg_token_entry.insert(0, creds.get("telegram_token", ""))

        self.tg_chat_id_entry.delete(0, "end")
        self.tg_chat_id_entry.insert(0, creds.get("telegram_chat_id", ""))

        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, str(creds.get("interval_minutes", 5)))

        # Load selected months
        selected_months = creds.get("target_months", ["aprilie"])
        for month, var in self.month_vars.items():
            var.set(month in selected_months)

        # Load selected scrape locations
        selected_locs = creds.get("scrape_locations", DEFAULT_SCRAPE_LOCATIONS)
        for loc, var in self.scrape_loc_vars.items():
            var.set(loc in selected_locs)

        # Auto-update fields
        self.auto_update_var.set(bool(creds.get("auto_update_enabled", False)))

        self.auto_update_idnp_entry.delete(0, "end")
        self.auto_update_idnp_entry.insert(0, creds.get("auto_update_idnp", ""))

        self.appointment_code_entry.delete(0, "end")
        self.appointment_code_entry.insert(0, creds.get("appointment_code", ""))

        self.request_number_entry.delete(0, "end")
        self.request_number_entry.insert(0, creds.get("request_number", ""))

        saved_loc = creds.get("target_location", "")
        if saved_loc in KNOWN_LOCATIONS:
            self.target_location_menu.set(saved_loc)
        else:
            self.target_location_menu.set(KNOWN_LOCATIONS[0])

        # current_appointment_date is stored as "dd.mm.yyyy" — split into 3 entries
        appt_date = creds.get("current_appointment_date", "")
        day, month, year = "", "", ""
        if appt_date:
            parts = appt_date.split(".")
            if len(parts) == 3:
                day, month, year = parts[0], parts[1], parts[2]
        self.appt_day_entry.delete(0, "end")
        self.appt_day_entry.insert(0, day)
        self.appt_month_entry.delete(0, "end")
        self.appt_month_entry.insert(0, month)
        self.appt_year_entry.delete(0, "end")
        self.appt_year_entry.insert(0, year)

    def _compose_appt_date(self) -> str:
        """Compose dd.mm.yyyy from the 3 entries, or '' if any is blank/invalid."""
        d = self.appt_day_entry.get().strip()
        m = self.appt_month_entry.get().strip()
        y = self.appt_year_entry.get().strip()
        if not (d and m and y):
            return ""
        try:
            return f"{int(d):02d}.{int(m):02d}.{int(y):04d}"
        except ValueError:
            return ""

    def save_credentials(self):
        """Save credentials to JSON file."""
        try:
            # Get selected months
            selected_months = [month for month, var in self.month_vars.items() if var.get()]
            if not selected_months:
                selected_months = ["aprilie"]  # Default if none selected

            # Get selected scrape locations
            selected_locs = [loc for loc, var in self.scrape_loc_vars.items() if var.get()]

            creds = {
                "idnp": self.idnp_entry.get(),
                "first_name": self.first_name_entry.get(),
                "last_name": self.last_name_entry.get(),
                "phone": self.phone_entry.get(),
                "email": self.email_entry.get(),
                "id_series": self.id_series_entry.get(),
                "id_date_day": int(self.id_date_day_entry.get() or 1),
                "id_date_month": int(self.id_date_month_entry.get() or 1),
                "id_date_year": int(self.id_date_year_entry.get() or 2020),
                "medical_cert": self.medical_cert_entry.get(),
                "telegram_token": self.tg_token_entry.get(),
                "telegram_chat_id": self.tg_chat_id_entry.get(),
                "interval_minutes": int(self.interval_entry.get() or 5),
                "target_months": selected_months,
                "scrape_locations": selected_locs,
                "auto_update_enabled": bool(self.auto_update_var.get()),
                "auto_update_idnp": self.auto_update_idnp_entry.get().strip(),
                "appointment_code": self.appointment_code_entry.get().strip(),
                "request_number": self.request_number_entry.get().strip(),
                "target_location": self.target_location_menu.get(),
                "current_appointment_date": self._compose_appt_date(),
            }

            with open(CREDENTIALS_FILE, "w", encoding='utf-8') as f:
                json.dump(creds, f, indent=2, ensure_ascii=False)

            self.log("[OK] Credentiale salvate! Luni selectate: " + ", ".join(m.capitalize() for m in selected_months))
        except Exception as e:
            self.log(f"[!] Eroare salvare: {e}")

    def toggle_monitor(self):
        """Start or stop monitoring."""
        if self.is_running:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self):
        """Start the monitoring thread."""
        self.save_credentials()

        # Redirect stdout to queue
        sys.stdout = QueueWriter(self.output_queue)

        # Load config
        try:
            with open(CREDENTIALS_FILE, encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            self.log(f"[!] Eroare citire config: {e}")
            sys.stdout = self.original_stdout
            return
        # Scrapper-ul scrie inapoi current_appointment_date dupa un auto-update reusit
        config["_credentials_file"] = CREDENTIALS_FILE

        self.is_running = True
        self.stop_event = asyncio.Event()

        # Start monitor thread
        self.monitor_thread = threading.Thread(
            target=self._run_monitor,
            args=(config,),
            daemon=True
        )
        self.monitor_thread.start()

        # Update UI
        self.start_btn.configure(text="[X] OPRESTE MONITORUL", fg_color="#F44336")
        self.status_dot.configure(text_color="#4CAF50")
        self.status_label.configure(text="Pornit")

        self.log("\n[>] Monitor pornit...")

    def stop_monitor(self):
        """Stop the monitoring thread."""
        if self.stop_event:
            self.stop_event.set()

        self.is_running = False

        # Restore stdout
        sys.stdout = self.original_stdout

        # Update UI
        self.start_btn.configure(text="[>] PORNESTE MONITORUL", fg_color="#2196F3")
        self.status_dot.configure(text_color="#FF5252")
        self.status_label.configure(text="Oprit")

        self.log("[X] Monitor oprit.")

    def _run_monitor(self, config):
        """Run the scrapper in asyncio (in background thread)."""
        try:
            asyncio.run(scrapper.run_with_config(config, self.stop_event))
        except Exception as e:
            self.log(f"\n[!] EROARE: {e}")
        finally:
            # Clean up
            sys.stdout = self.original_stdout
            if self.is_running:
                self.stop_monitor()

    def log(self, message: str):
        """Log message to console (safe from any thread)."""
        self.output_queue.put(message)

    def poll_output(self):
        """Poll the queue and update console."""
        try:
            while True:
                text = self.output_queue.get_nowait()
                self.console_text.configure(state="normal")
                self.console_text.insert("end", text)
                self.console_text.see("end")
                self.console_text.configure(state="disabled")
        except queue.Empty:
            pass

        # Poll again in 100ms
        self.after(100, self.poll_output)


if __name__ == "__main__":
    app = ASPCheckerApp()
    app.mainloop()
