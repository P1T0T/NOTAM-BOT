#!/usr/bin/env python3
"""
NOTAM Polska -> Discord (webhook)

Co robi:
  1. Co X minut pyta FAA o aktywne NOTAMy dla wybranych polskich lotnisk.
  2. Sprawdza w małej bazie (plik notam_seen.db), które już widział.
  3. Nowe wysyła na Discorda. Stare ignoruje.

Uruchamianie:
  python notam_bot.py --test       # wysyła wiadomość testową (sprawdza webhook)
  python notam_bot.py --demo       # wysyła 3 FAŁSZYWE NOTAMy (sprawdza wygląd na Discordzie)
  python notam_bot.py --once       # jedno sprawdzenie i koniec (pod Harmonogram zadań / cron)
  python notam_bot.py              # działa non-stop, sprawdza co CO_ILE_MINUT minut
"""

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
import time

import requests

# =====================================================================
#                     USTAWIENIA  (edytuj tylko tu)
# =====================================================================

# Link do webhooka z Discorda (wklej między cudzysłowy, nikomu go nie pokazuj!)
WEBHOOK_URL = os.environ.get("NOTAM_WEBHOOK_URL") or "https://discord.com/api/webhooks/1554166924945133710/9MpVaZjHInKde1PUMFm2tSVmYPB4kGQhX3u7UQ7E4cZLK2U3Bc7i4Q5jnYgkMlvILgFs"
# (Na GitHubie link jest w sekrecie NOTAM_WEBHOOK_URL - wtedy nic tu nie wklejasz.)

# Lotniska (kody ICAO). Dodawaj / usuwaj śmiało.
# (Chcesz NOTAMy dla całej przestrzeni powietrznej? Dopisz "EPWW" - FIR Warszawa.)
LOTNISKA = [
    "EPGD",  # Gdańsk
    "EPSC",  # Szczecin
    "EPSY",  # Olsztyn-Mazury
    "EPMO",  # Warszawa Modlin
    "EPWA",  # Warszawa Chopin
    "EPLL",  # Łódź
    "EPRA",  # Radom
    "EPLB",  # Lublin
    "EPRZ",  # Rzeszów
    "EPKK",  # Kraków
    "EPKT",  # Katowice
    "EPWR",  # Wrocław
    "EPPO",  # Poznań
    "EPZG",  # Zielona Góra
    "EPBY",  # Bydgoszcz
]

# Co ile minut sprawdzać (nie schodź poniżej 10, żeby nie męczyć serwera FAA)
CO_ILE_MINUT = 30

# Plik z pamięcią "co już wysłałem"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "notam_seen.db")
LOG_FILE = os.path.join(BASE_DIR, "notam_bot.log")

# =====================================================================
#                      dalej nie musisz nic ruszać
# =====================================================================

FAA_HOME = "https://notams.aim.faa.gov/notamSearch/"
FAA_SEARCH = FAA_HOME + "search"
CHUNK = 5  # ile lotnisk w jednym zapytaniu do FAA

log = logging.getLogger("notam")


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------
# POBIERANIE Z FAA
# To jest JEDYNE miejsce, które gada z FAA. Jeśli kiedyś przejdziesz na
# oficjalne NMS-API, podmieniasz tylko tę funkcję (musi zwracać listę słowników).
# ---------------------------------------------------------------------
def fetch_faa(airports):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": FAA_HOME,
            "Origin": "https://notams.aim.faa.gov",
        }
    )
    try:
        session.get(FAA_HOME, timeout=30)  # dostajemy ciasteczka sesji
    except requests.RequestException as e:
        raise FetchError(f"Nie mogę wejść na stronę FAA: {e}")

    results = []
    for i in range(0, len(airports), CHUNK):
        chunk = airports[i : i + CHUNK]
        offset = 0
        while True:
            data = {
                "searchType": "0",
                "designatorsForLocation": ",".join(chunk),
                "designatorForAccountable": "",
                "formatType": "ICAO",
                "notamsOnly": "false",
                "radius": "10",
                "sortColumns": "5 false",
                "sortDirection": "true",
                "offset": str(offset),
                "flightPathBuffer": "4",
                "flightPathIncludeNavaids": "true",
                "flightPathIncludeArtcc": "false",
                "flightPathIncludeTfr": "true",
                "flightPathIncludeRegulatory": "false",
                "flightPathResultsType": "All NOTAMs",
                "radiusSearchOnDesignator": "false",
                "latitudeDirection": "N",
                "longitudeDirection": "W",
            }
            try:
                r = session.post(FAA_SEARCH, data=data, timeout=60)
                r.raise_for_status()
                j = r.json()
            except ValueError:
                raise FetchError(
                    "FAA nie odpowiedziało w formacie JSON (możliwa blokada, captcha "
                    "albo zmiana strony)."
                )
            except requests.RequestException as e:
                raise FetchError(f"Błąd połączenia z FAA: {e}")

            items = j.get("notamList") or []
            results.extend(items)
            total = j.get("totalNotamCount") or 0
            offset += len(items)
            if not items or offset >= total or offset > 3000:
                break
            time.sleep(1)
        time.sleep(1)
    return results


# ---------------------------------------------------------------------
# PORZĄDKOWANIE DANYCH
# ---------------------------------------------------------------------
def normalize(item):
    """Wyciąga z odpowiedzi FAA to, co potrzebne. Odporne na brakujące pola."""
    icao = item.get("icaoId") or item.get("facilityDesignator") or item.get("location") or "????"
    number = item.get("notamNumber") or ""
    text = (
        item.get("icaoMessage")
        or item.get("traditionalMessage")
        or item.get("notamText")
        or ""
    ).strip()
    if number:
        uid = f"{icao}|{number}"
    else:
        uid = f"{icao}|{hashlib.sha1(text.encode()).hexdigest()[:16]}"
    return {
        "id": uid,
        "icao": icao,
        "number": number or "(bez numeru)",
        "text": text or "(brak treści)",
        "start": item.get("startDate") or "",
        "end": item.get("endDate") or "",
    }


def color_for(text):
    t = text.upper()
    if "NOTAMC" in t.split("\n")[0] or " NOTAMC " in t:
        return 0x95A5A6  # szary - anulowanie
    if "CLSD" in t or "CLOSED" in t or "PROHIBITED" in t:
        return 0xE74C3C  # czerwony - zamknięcia / zakazy
    return 0xF1C40F  # żółty - reszta


def make_embed(n):
    text = n["text"].replace("```", "'''")
    if len(text) > 1800:
        text = text[:1800] + "\n... (skrócone, pełna treść na stronie FAA)"
    embed = {
        "title": f"{n['icao']} • {n['number']}",
        "description": f"```\n{text}\n```",
        "color": color_for(n["text"]),
    }
    fields = []
    if n["start"]:
        fields.append({"name": "Od", "value": str(n["start"]), "inline": True})
    if n["end"]:
        fields.append({"name": "Do", "value": str(n["end"]), "inline": True})
    if fields:
        embed["fields"] = fields
    return embed


# ---------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------
def post_to_discord(payload):
    for _ in range(6):
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
        if r.status_code == 429:  # za szybko - Discord każe chwilę poczekać
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord ciągle odpowiada 429 (limit) - spróbuję przy następnym przebiegu.")


def send_text(msg):
    post_to_discord({"username": "NOTAM PL", "content": msg[:1900]})


def embed_size(e):
    return len(e.get("title", "")) + len(e.get("description", "")) + 100


def send_notams(notams, on_sent):
    """Wysyła paczkami (limity Discorda: max 10 embedów i 6000 znaków na wiadomość).
    Po każdej udanej paczce woła on_sent(lista) - żeby zapisać, że poszło."""
    batch, size = [], 0
    for n in notams:
        e = make_embed(n)
        s = embed_size(e)
        if batch and (len(batch) >= 5 or size + s > 5500):
            _flush(batch, on_sent)
            batch, size = [], 0
        batch.append((n, e))
        size += s
    if batch:
        _flush(batch, on_sent)


def _flush(batch, on_sent):
    post_to_discord({"username": "NOTAM PL", "embeds": [e for _, e in batch]})
    on_sent([n for n, _ in batch])
    time.sleep(1)


# ---------------------------------------------------------------------
# PAMIĘĆ (SQLite)
# ---------------------------------------------------------------------
def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def get_meta(conn, key, default="0"):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    # zapisujemy tylko gdy wartość się zmienia (żeby plik bazy nie zmieniał się bez powodu)
    if get_meta(conn, key, None) == value:
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def db_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


def is_seen(conn, uid):
    return conn.execute("SELECT 1 FROM seen WHERE id=?", (uid,)).fetchone() is not None


def mark_seen(conn, notams):
    conn.executemany(
        "INSERT OR IGNORE INTO seen (id, first_seen) VALUES (?, datetime('now'))",
        [(n["id"],) for n in notams],
    )
    conn.commit()


# ---------------------------------------------------------------------
# GŁÓWNA LOGIKA
# ---------------------------------------------------------------------
def run_once(conn, raw_items, post_existing=False):
    items = {}
    for x in raw_items:
        n = normalize(x)
        items[n["id"]] = n  # słownik = automatyczne usuwanie duplikatów
    items = list(items.values())

    first_run = db_empty(conn)
    new = [n for n in items if not is_seen(conn, n["id"])]
    new.sort(key=lambda n: (n["icao"], n["number"]))

    if first_run and not post_existing:
        mark_seen(conn, new)
        send_text(
            f"✅ Bot NOTAM wystartował. Zapisałem {len(new)} aktywnych NOTAMów jako "
            f"już znane - od teraz dostaniesz tylko NOWE."
        )
        log.info("Pierwsze uruchomienie: zapisano %d NOTAMów bez wysyłania.", len(new))
        return

    if not new:
        log.info("Brak nowych NOTAMów (aktywnych: %d).", len(items))
        return

    log.info("Nowych NOTAMów: %d - wysyłam.", len(new))
    send_notams(new, on_sent=lambda sent: mark_seen(conn, sent))


def demo_items():
    return [
        {
            "icaoId": "EPWA",
            "notamNumber": "DEMO001/26",
            "startDate": "01/01/2026 0600",
            "endDate": "01/01/2026 1800",
            "icaoMessage": "[DEMO - NIE PRAWDZIWY]\nDEMO001/26 NOTAMN\nQ) EPWW/QMRLC/IV/NBO/A/000/999\n"
            "A) EPWA B) 2601010600 C) 2601011800\nE) RWY 15/33 CLSD",
        },
        {
            "icaoId": "EPKK",
            "notamNumber": "DEMO002/26",
            "startDate": "01/01/2026 0600",
            "endDate": "01/02/2026 1800",
            "icaoMessage": "[DEMO - NIE PRAWDZIWY]\nDEMO002/26 NOTAMN\nQ) EPWW/QOBCE/IV/M/A/000/999\n"
            "A) EPKK B) 2601010600 C) 2601021800\nE) CRANE ERECTED 500M W OF THR RWY 07",
        },
        {
            "icaoId": "EPGD",
            "notamNumber": "DEMO003/26",
            "icaoMessage": "[DEMO - NIE PRAWDZIWY]\nDEMO003/26 NOTAMC DEMO001/26\nQ) EPWW/QFALC/IV/NBO/A/000/999\n"
            "A) EPGD\nE) CANCELLED",
        },
    ]


def main():
    ap = argparse.ArgumentParser(description="NOTAM Polska -> Discord")
    ap.add_argument("--test", action="store_true", help="wyślij wiadomość testową i zakończ")
    ap.add_argument("--demo", action="store_true", help="wyślij 3 fałszywe NOTAMy i zakończ")
    ap.add_argument("--once", action="store_true", help="jedno sprawdzenie i zakończ")
    ap.add_argument(
        "--post-existing",
        action="store_true",
        help="przy pierwszym uruchomieniu wyślij też wszystkie aktualnie aktywne NOTAMy",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )

    if WEBHOOK_URL.startswith("WKLEJ") or not WEBHOOK_URL.startswith("https://"):
        sys.exit("Najpierw wklej link do webhooka w linii WEBHOOK_URL na górze pliku.")

    if args.test:
        send_text("🛫 Test OK - webhook działa.")
        print("Wysłano wiadomość testową. Sprawdź kanał na Discordzie.")
        return

    if args.demo:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE seen (id TEXT PRIMARY KEY, first_seen TEXT)")
        run_once(conn, demo_items(), post_existing=True)
        print("Wysłano 3 demo-NOTAMy. Sprawdź kanał na Discordzie.")
        return

    conn = open_db(DB_FILE)
    while True:
        ok = True
        try:
            raw = fetch_faa(LOTNISKA)
            run_once(conn, raw, post_existing=args.post_existing)
            args.post_existing = False
            if get_meta(conn, "alerted") == "1":
                send_text("✅ Bot NOTAM: problem minął, pobieranie znów działa.")
            set_meta(conn, "fails", "0")
            set_meta(conn, "alerted", "0")
        except Exception as e:  # noqa: BLE001
            ok = False
            fails = int(get_meta(conn, "fails")) + 1
            set_meta(conn, "fails", str(fails))
            log.error("Błąd (%d z rzędu): %s", fails, e)
            if fails >= 3 and get_meta(conn, "alerted") != "1":
                try:
                    send_text(
                        f"⚠️ Bot NOTAM: {fails} nieudane przebiegi z rzędu (pobieranie z FAA "
                        f"lub wysyłka).\nOstatni błąd: {e}"
                    )
                    set_meta(conn, "alerted", "1")
                except Exception:  # noqa: BLE001
                    pass
        if args.once:
            sys.exit(0 if ok else 1)
        time.sleep(max(10, CO_ILE_MINUT) * 60)


if __name__ == "__main__":
    main()
