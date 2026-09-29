#!/usr/bin/env python3
"""
NOTAM Polska -> Discord (webhook)

Co robi:
  1. Co X minut pyta FAA o aktywne NOTAMy dla wybranych polskich lotnisk.
  2. Odfiltrowuje to, co nieistotne dla vATC (patrz sekcja FILTR niżej).
  3. Sprawdza w małej bazie (plik notam_seen.db), które już widział.
  4. Nowe i istotne wysyła na Discorda. Gdy NOTAM wygasa, oznacza starą
     wiadomość na Discordzie jako "WYGASŁ" (szary, przekreślony tytuł).

Uruchamianie:
  python notam_bot.py --test       # wysyła wiadomość testową (sprawdza webhook)
  python notam_bot.py --demo       # wysyła kilka PRZYKŁADOWYCH NOTAMów (test filtra + wyglądu)
  python notam_bot.py --once       # jedno sprawdzenie i koniec (pod Harmonogram zadań / GitHub Actions)
  python notam_bot.py              # działa non-stop, sprawdza co CO_ILE_MINUT minut
"""

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

import requests

# =====================================================================
#                     USTAWIENIA  (edytuj tylko tu)
# =====================================================================

# Link do webhooka z Discorda (wklej między cudzysłowy, nikomu go nie pokazuj!)
WEBHOOK_URL = os.environ.get("NOTAM_WEBHOOK_URL") or "WKLEJ_TUTAJ_LINK_DO_WEBHOOKA"
# (Na GitHubie link jest w sekrecie NOTAM_WEBHOOK_URL - wtedy nic tu nie wklejasz.)

# Lotniska (kody ICAO). Dodawaj / usuwaj śmiało.
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
CO_ILE_MINUT = 15

# ---------------------------------------------------------------------
# FILTR - co się wysyła na Discorda
# ---------------------------------------------------------------------
# NOTAM trafia na Discorda wg pierwszej pasującej reguły (sprawdzane w tej kolejności):
#
#   0. To odwołanie (NOTAMC) NOTAMu, który WCZEŚNIEJ wysłaliśmy -> ZAWSZE wysyłamy
#      (żeby na kanale nie zostawała nieaktualna informacja o zamknięciu).
#   1. To zamknięcie CAŁEGO LOTNISKA (AD/ARO) -> NIGDY nie wysyłamy
#      (na VATSIM tego nie egzekwujemy, więc to szum).
#   2. To zamknięcie drogi startowej / kołowania / apronu / stanowiska
#      (RWY/TWY/APRON/STAND CLSD, albo "niesprawne") -> ZAWSZE wysyłamy,
#      niezależnie od tego, jak krótko trwa.
#   3. To NOTAM o przeszkodzie (żuraw, budowa itp.) -> NIGDY nie wysyłamy.
#   4. Wszystko inne -> wysyłamy TYLKO jeśli trwa co najmniej MIN_GODZIN_INNE
#      godzin (albo jest bezterminowe / PERM). Krócej = uznajemy za rutynowe
#      prace (jak przykład z ILS w Radomiu na 6h) i pomijamy.
#
# Wszystko poniżej możesz dostroić.

MIN_GODZIN_INNE = 24  # próg czasu trwania dla punktu 4. Zmień, jeśli chcesz inaczej.

# Kody Q) dla ruchu naziemnego (2. i 3. litera po "Q" w linii Q) NOTAMu ICAO):
#   MR = droga startowa, MX = droga kołowania, MN = apron, MK = miejsce postojowe, MP = stanowisko
KODY_ZAMKNIECIA_PODMIOT = {"MR", "MX", "MN", "MK", "MP"}
# Kody warunku: LC = zamknięte, AS = niesprawne (w praktyce = nieużywalne)
KODY_ZAMKNIECIA_WARUNEK = {"LC", "AS"}
# Kod Q) dla całego lotniska
KOD_LOTNISKO_PODMIOT = "FA"
# Kod Q) dla przeszkód (żurawie itp.)
KOD_PRZESZKODA_PODMIOT = "OB"

# Słowa kluczowe jako zapasowy filtr tekstowy, gdyby linia Q) nie dała się rozczytać
SLOWA_ZAMKNIECIE = ("RWY", "TWY", "APRON", "APN", "STAND", "STND")
SLOWA_LOTNISKO = ("AERODROME CLSD", "AD CLSD", "ARO CLSD")
SLOWA_PRZESZKODA = ("CRANE", "OBST ", "OBSTACLE")

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


# ---------------------------------------------------------------------
# DATY - potrzebne do liczenia czasu trwania i do "wygasł"
# ---------------------------------------------------------------------
DATE_FORMATS = (
    "%m/%d/%Y %H%M",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y%m%d%H%M",
)


def parse_dt(s):
    """Próbuje rozpoznać datę z FAA. Zwraca None, gdy się nie da albo gdy NOTAM
    jest bezterminowy (PERM / rok >= 2099 - tak FAA zwykle koduje "na zawsze")."""
    if not s:
        return None
    s = str(s).strip()
    if not s or "PERM" in s.upper():
        return None
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year >= 2099:
                return None
            return dt
        except ValueError:
            continue
    return None


def duration_hours(start_raw, end_raw):
    """Zwraca liczbę godzin trwania NOTAMu, albo None gdy nie da się ustalić
    (brak/format daty, albo NOTAM bezterminowy - traktujemy to jak "długi")."""
    start_dt, end_dt = parse_dt(start_raw), parse_dt(end_raw)
    if not start_dt or not end_dt:
        return None
    return (end_dt - start_dt).total_seconds() / 3600


# ---------------------------------------------------------------------
# FILTR
# ---------------------------------------------------------------------
Q_RE = re.compile(r"Q\)\s*[A-Z]{4}/Q([A-Z]{4})")


def q_code(text):
    """Zwraca (podmiot, warunek) z linii Q) NOTAMu ICAO, np. ('MR','LC'). None, gdy brak."""
    m = Q_RE.search(text.upper())
    if not m:
        return None, None
    code = m.group(1)
    return code[:2], code[2:]


def is_cancellation(text):
    first_line = text.strip().splitlines()[0].upper() if text.strip() else ""
    return "NOTAMC" in first_line


def cancelled_number(text):
    """Z 'DEMO003/26 NOTAMC DEMO001/26' wyciąga 'DEMO001/26' - numer odwoływanego NOTAMu."""
    m = re.search(r"NOTAMC\s+([A-Z0-9/]+)", text.upper())
    return m.group(1) if m else None


def is_area_closure(text):
    subject, condition = q_code(text)
    if subject in KODY_ZAMKNIECIA_PODMIOT and condition in KODY_ZAMKNIECIA_WARUNEK:
        return True
    t = text.upper()
    return any(w in t for w in SLOWA_ZAMKNIECIE) and "CLSD" in t


def is_airport_closure(text):
    subject, _ = q_code(text)
    if subject == KOD_LOTNISKO_PODMIOT:
        return True
    t = text.upper()
    return any(w in t for w in SLOWA_LOTNISKO)


def is_obstacle(text):
    subject, _ = q_code(text)
    if subject == KOD_PRZESZKODA_PODMIOT:
        return True
    t = text.upper()
    return any(w in t for w in SLOWA_PRZESZKODA)


def is_relevant(n, was_original_sent):
    """Decyduje, czy NOTAM n (słownik z normalize()) ma iść na Discorda.
    was_original_sent(numer) -> bool - sprawdza w bazie, czy NOTAM o danym
    numerze był wcześniej faktycznie wysłany."""
    text = n["text"]

    if is_cancellation(text):
        old = cancelled_number(text)
        return bool(old and was_original_sent(old))

    if is_airport_closure(text):
        return False

    if is_area_closure(text):
        return True

    if is_obstacle(text):
        return False

    dur = duration_hours(n["start"], n["end"])
    if dur is None:
        return True  # nieznany czas trwania albo NOTAM bezterminowy - wolimy pokazać
    return dur >= MIN_GODZIN_INNE


# ---------------------------------------------------------------------
# WYGLĄD NA DISCORDZIE
# ---------------------------------------------------------------------
# Kolory embedów:
#   czerwony (0xE74C3C) - zamknięcie / zakaz (w treści jest CLSD / CLOSED / PROHIBITED)
#   żółty    (0xF1C40F) - wszystko inne, co przeszło filtr (dłuższe, istotne NOTAMy)
#   szary    (0x95A5A6) - odwołanie poprzedniego NOTAMu (NOTAMC)
#   ciemnoszary, dopisek "WYGASŁ" - NOTAM, którego termin minął (patrz cleanup_expired)
def color_for(text):
    if is_cancellation(text):
        return 0x95A5A6  # szary - anulowanie
    t = text.upper()
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


def make_expired_embed(n):
    embed = make_embed(n)
    embed["color"] = 0x2C2F33
    embed["title"] = "🗑️ WYGASŁ • " + embed["title"]
    return embed


# ---------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------
def post_to_discord(payload, wait=False):
    params = {"wait": "true"} if wait else None
    for _ in range(6):
        r = requests.post(WEBHOOK_URL, params=params, json=payload, timeout=30)
        if r.status_code == 429:  # za szybko - Discord każe chwilę poczekać
            try:
                wait_s = float(r.json().get("retry_after", 2))
            except ValueError:
                wait_s = 2
            time.sleep(wait_s + 0.5)
            continue
        r.raise_for_status()
        return r.json() if wait and r.content else None
    raise RuntimeError("Discord ciągle odpowiada 429 (limit) - spróbuję przy następnym przebiegu.")


def edit_discord_message(message_id, payload):
    url = f"{WEBHOOK_URL}/messages/{message_id}"
    for _ in range(6):
        r = requests.patch(url, json=payload, timeout=30)
        if r.status_code == 429:
            try:
                wait_s = float(r.json().get("retry_after", 2))
            except ValueError:
                wait_s = 2
            time.sleep(wait_s + 0.5)
            continue
        if r.status_code == 404:
            return False  # wiadomość ktoś usunął ręcznie - trudno, pomijamy
        r.raise_for_status()
        return True
    return False


def send_text(msg):
    post_to_discord({"username": "NOTAM PL", "content": msg[:1900]})


def send_notams(notams, on_sent):
    """Wysyła NOTAMy pojedynczo (po jednym na wiadomość) - dzięki temu każdy ma
    swoje message_id i można go później oznaczyć jako wygasły. Po każdym udanym
    wysłaniu woła on_sent(notam, message_id)."""
    for n in notams:
        msg = post_to_discord({"username": "NOTAM PL", "embeds": [make_embed(n)]}, wait=True)
        on_sent(n, (msg or {}).get("id"))
        time.sleep(1)


# ---------------------------------------------------------------------
# PAMIĘĆ (SQLite)
# ---------------------------------------------------------------------
SCHEMA_SEEN = """CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    icao TEXT, number TEXT, text TEXT,
    start_raw TEXT, end_raw TEXT,
    was_sent INTEGER DEFAULT 0,
    message_id TEXT,
    expired INTEGER DEFAULT 0,
    first_seen TEXT
)"""


def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA_SEEN)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    _migrate_seen(conn)
    return conn


def _migrate_seen(conn):
    """Dogania starszy plik notam_seen.db (sprzed dodania filtra/wygasania) do
    aktualnego schematu - dopisuje brakujące kolumny, nic nie kasuje."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(seen)")}
    want = {
        "icao": "TEXT",
        "number": "TEXT",
        "text": "TEXT",
        "start_raw": "TEXT",
        "end_raw": "TEXT",
        "was_sent": "INTEGER DEFAULT 0",
        "message_id": "TEXT",
        "expired": "INTEGER DEFAULT 0",
    }
    changed = False
    for col, coltype in want.items():
        if col not in have:
            conn.execute(f"ALTER TABLE seen ADD COLUMN {col} {coltype}")
            changed = True
    if changed:
        conn.commit()
        log.info("Baza notam_seen.db zaktualizowana do nowego schematu (dodano brakujące kolumny).")


def get_meta(conn, key, default="0"):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    if get_meta(conn, key, None) == value:
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def db_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


def is_seen(conn, uid):
    return conn.execute("SELECT 1 FROM seen WHERE id=?", (uid,)).fetchone() is not None


def was_sent(conn, number):
    row = conn.execute("SELECT 1 FROM seen WHERE number=? AND was_sent=1", (number,)).fetchone()
    return row is not None


def insert_seen(conn, n, sent, message_id=None):
    conn.execute(
        """INSERT OR IGNORE INTO seen
           (id, icao, number, text, start_raw, end_raw, was_sent, message_id, first_seen)
           VALUES (?,?,?,?,?,?,?,?, datetime('now'))""",
        (n["id"], n["icao"], n["number"], n["text"], n["start"], n["end"], int(sent), message_id),
    )
    conn.commit()


def cleanup_expired(conn):
    """Wygasłe (data C już minęła) NOTAMy, które faktycznie wysłaliśmy: oznacza
    wiadomość na Discordzie jako WYGASŁ i usuwa wpis z bazy (żeby nie rosła
    w nieskończoność i żeby przyszła lista/komenda pokazywała tylko aktywne)."""
    rows = conn.execute(
        "SELECT id, icao, number, text, start_raw, end_raw, message_id "
        "FROM seen WHERE was_sent=1 AND expired=0"
    ).fetchall()
    now = datetime.utcnow()
    n_done = 0
    for uid, icao, number, text, start_raw, end_raw, message_id in rows:
        end_dt = parse_dt(end_raw)
        if end_dt is None or end_dt > now:
            continue  # jeszcze aktywny albo bezterminowy
        n = {"id": uid, "icao": icao, "number": number, "text": text, "start": start_raw, "end": end_raw}
        if message_id:
            edit_discord_message(message_id, {"embeds": [make_expired_embed(n)]})
        conn.execute("DELETE FROM seen WHERE id=?", (uid,))
        n_done += 1
    if n_done:
        conn.commit()
    return n_done


# ---------------------------------------------------------------------
# GŁÓWNA LOGIKA
# ---------------------------------------------------------------------
def run_once(conn, raw_items, post_existing=False):
    items = {}
    for x in raw_items:
        n = normalize(x)
        items[n["id"]] = n  # słownik = automatyczne usuwanie duplikatów
    items = list(items.values())

    expired_n = cleanup_expired(conn)
    if expired_n:
        log.info("Oznaczono jako wygasłe: %d.", expired_n)

    first_run = db_empty(conn)
    new = [n for n in items if not is_seen(conn, n["id"])]
    new.sort(key=lambda n: (n["icao"], n["number"]))

    if first_run and not post_existing:
        for n in new:
            insert_seen(conn, n, sent=False)
        send_text(
            f"✅ Bot NOTAM wystartował. Zapisałem {len(new)} aktywnych NOTAMów jako "
            f"już znane - od teraz dostaniesz tylko NOWE i ISTOTNE."
        )
        log.info("Pierwsze uruchomienie: zapisano %d NOTAMów bez wysyłania.", len(new))
        return

    if not new:
        log.info("Brak nowych NOTAMów (aktywnych: %d).", len(items))
        return

    # Odwołania (NOTAMC) oceniamy w drugiej turze: jeśli oryginał, którego
    # dotyczą, przyszedł w TYM SAMYM przebiegu i właśnie idzie na Discorda,
    # to odwołanie ma przejść razem z nim (nie tylko gdy oryginał był wysłany kiedyś wcześniej).
    others = [n for n in new if not is_cancellation(n["text"])]
    cancellations = [n for n in new if is_cancellation(n["text"])]

    to_send, to_skip = [], []
    for n in others:
        (to_send if is_relevant(n, lambda num: was_sent(conn, num)) else to_skip).append(n)

    sent_this_run = {n["number"] for n in to_send}
    already_sent_or_about_to = lambda num: was_sent(conn, num) or num in sent_this_run  # noqa: E731
    for n in cancellations:
        (to_send if is_relevant(n, already_sent_or_about_to) else to_skip).append(n)

    for n in to_skip:
        insert_seen(conn, n, sent=False)

    if to_send:
        log.info("Nowych NOTAMów: %d (wysyłam), odfiltrowano: %d.", len(to_send), len(to_skip))
        send_notams(to_send, on_sent=lambda n, mid: insert_seen(conn, n, sent=True, message_id=mid))
    else:
        log.info("Nowych NOTAMów: 0 istotnych (odfiltrowano %d).", len(to_skip))


def demo_items():
    return [
        {
            "icaoId": "EPWA",
            "notamNumber": "DEMO001/26",
            "startDate": "01/01/2026 0600",
            "endDate": "01/01/2026 1800",
            "icaoMessage": "[DEMO] DEMO001/26 NOTAMN\nQ) EPWW/QMRLC/IV/NBO/A/000/999\n"
            "A) EPWA B) 2601010600 C) 2601011800\nE) RWY 15/33 CLSD\n"
            "-- krótkie (12h) zamknięcie pasa -> MA PRZEJŚĆ (zawsze wysyłamy zamknięcia)",
        },
        {
            "icaoId": "EPKK",
            "notamNumber": "DEMO002/26",
            "startDate": "01/01/2026 0600",
            "endDate": "01/02/2026 1800",
            "icaoMessage": "[DEMO] DEMO002/26 NOTAMN\nQ) EPWW/QOBCE/IV/M/A/000/999\n"
            "A) EPKK B) 2601010600 C) 2601021800\nE) CRANE ERECTED 500M W OF THR RWY 07\n"
            "-- żuraw, 36h -> NIE MA PRZEJŚĆ (zawsze wykluczamy przeszkody)",
        },
        {
            "icaoId": "EPRA",
            "notamNumber": "DEMO003/26",
            "startDate": "01/01/2026 0600",
            "endDate": "01/01/2026 1200",
            "icaoMessage": "[DEMO] DEMO003/26 NOTAMN\nQ) EPWW/QNAAS/IV/BO/A/000/999\n"
            "A) EPRA B) 2601010600 C) 2601011200\nE) ILS RWY 07 U/S DUE MAINTENANCE\n"
            "-- ILS niesprawny 6h -> NIE MA PRZEJŚĆ (krócej niż próg)",
        },
        {
            "icaoId": "EPGD",
            "notamNumber": "DEMO004/26",
            "startDate": "01/01/2026 0000",
            "endDate": "01/05/2026 0000",
            "icaoMessage": "[DEMO] DEMO004/26 NOTAMN\nQ) EPWW/QNAAS/IV/BO/A/000/999\n"
            "A) EPGD B) 2601010000 C) 2601050000\nE) VOR/DME U/S DUE MAINTENANCE\n"
            "-- niesprawny VOR/DME, 96h -> MA PRZEJŚĆ (dłużej niż próg)",
        },
        {
            "icaoId": "EPWA",
            "notamNumber": "DEMO005/26",
            "icaoMessage": "[DEMO] DEMO005/26 NOTAMC DEMO001/26\nQ) EPWW/QMRLC/IV/NBO/A/000/999\n"
            "A) EPWA\nE) RWY 15/33 CLSD - CANCELLED\n"
            "-- odwołanie NOTAMu, który wcześniej wysłaliśmy -> MA PRZEJŚĆ",
        },
        {
            "icaoId": "EPKT",
            "notamNumber": "DEMO006/26",
            "icaoMessage": "[DEMO] DEMO006/26 NOTAMC DEMO002/26\nQ) EPWW/QOBCE/IV/M/A/000/999\n"
            "A) EPKT\nE) CRANE REMOVED - CANCELLED\n"
            "-- odwołanie NOTAMu o żurawiu, którego NIE wysłaliśmy -> NIE MA PRZEJŚĆ",
        },
        {
            "icaoId": "EPPO",
            "notamNumber": "DEMO007/26",
            "icaoMessage": "[DEMO] DEMO007/26 NOTAMN\nQ) EPWW/QFALC/IV/NBO/A/000/999\n"
            "A) EPPO\nE) AD CLSD\n"
            "-- zamknięcie CAŁEGO lotniska -> NIE MA PRZEJŚĆ (nie egzekwujemy tego na VATSIM)",
        },
    ]


def main():
    ap = argparse.ArgumentParser(description="NOTAM Polska -> Discord")
    ap.add_argument("--test", action="store_true", help="wyślij wiadomość testową i zakończ")
    ap.add_argument("--demo", action="store_true", help="wyślij przykładowe NOTAMy i pokaż działanie filtra")
    ap.add_argument("--once", action="store_true", help="jedno sprawdzenie i zakończ")
    ap.add_argument(
        "--post-existing",
        action="store_true",
        help="przy pierwszym uruchomieniu wyślij też wszystkie aktualnie aktywne (i istotne) NOTAMy",
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
        conn.execute(SCHEMA_SEEN.replace("IF NOT EXISTS ", ""))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        run_once(conn, demo_items(), post_existing=True)
        print(
            "Wysłano demo-NOTAMy zgodnie z filtrem. Sprawdź kanał na Discordzie i porównaj "
            "z komentarzami '-- MA PRZEJŚĆ / NIE MA PRZEJŚĆ' w funkcji demo_items()."
        )
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
