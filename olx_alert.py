import requests
from bs4 import BeautifulSoup
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ============================================================
# USTAWIENIA - tutaj zmieniaj parametry bez dotykania reszty
# ============================================================

POWIERZCHNIA_MIN = 35
POWIERZCHNIA_MAX = 45
CENA_M2_WTORNY_MAX = 7800
CENA_M2_PIERWOTNY_MAX = 9000

URL_WTORNY = (
    "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/lodz/"
    "?search[district_id]=295"
    "&search[order]=created_at:desc"
    "&search[filter_float_m:from]=35"
    "&search[filter_float_m:to]=45"
    "&search[filter_enum_market][0]=secondary"
)

URL_PIERWOTNY = (
    "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/lodz/"
    "?search[district_id]=295"
    "&search[order]=created_at:desc"
    "&search[filter_float_m:from]=35"
    "&search[filter_float_m:to]=45"
    "&search[filter_enum_market][0]=primary"
)

SEEN_FILE = "seen_offers.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)

def pobierz_oferty(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Błąd pobierania strony: {e}")
        return []

    print(f"Pobrano stronę: {len(resp.text)} znaków")
    soup = BeautifulSoup(resp.text, "html.parser")

    # Wypisujemy WSZYSTKIE tagi script żeby znaleźć ten z ofertami
    print("=== WSZYSTKIE TAGI SCRIPT ===")
    for i, s in enumerate(soup.find_all("script")):
        sid = s.get("id", "")
        content = s.string or ""
        # Szukamy tagów które mogą zawierać oferty
        interesujacy = any(kw in content for kw in [
            "ads", "listing", "offers", "PRERENDERED", "NEXT_DATA",
            "pageProps", "title", "price"
        ])
        if interesujacy or sid:
            print(f"  [{i}] id='{sid}' len={len(content)} | {content[:150].replace(chr(10),' ')}")

    return []

def wyslij_maila(nowe_oferty, zmienione_oferty):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_PASSWORD"]
    odbiorcy = os.environ["NOTIFY_EMAILS"].split(",")

    teraz = datetime.now().strftime("%d.%m.%Y %H:%M")
    tresc = f"OLX Alert Łódź Polesie — {teraz}\n"
    tresc += "=" * 50 + "\n\n"

    if nowe_oferty:
        tresc += f"NOWE OFERTY ({len(nowe_oferty)}):\n\n"
        for o in nowe_oferty:
            tresc += f"  {o['tytul']}\n"
            tresc += f"  Cena:         {o['cena']:,} zł\n".replace(",", " ")
            tresc += f"  Powierzchnia: {o['powierzchnia']} m²\n"
            tresc += f"  Cena/m²:      {o['cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"  Link:         {o['url']}\n\n"

    if zmienione_oferty:
        tresc += f"ZMIANY CEN ({len(zmienione_oferty)}):\n\n"
        for o in zmienione_oferty:
            roznica = o["stara_cena_m2"] - o["cena_m2"]
            tresc += f"  {o['tytul']}\n"
            tresc += f"  Stara cena/m²: {o['stara_cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"  Nowa cena/m²:  {o['cena_m2']:,} zł  (taniej o {roznica:,} zł/m²)\n".replace(",", " ")
            tresc += f"  Powierzchnia:  {o['powierzchnia']} m²\n"
            tresc += f"  Link:          {o['url']}\n\n"

    tresc += "=" * 50 + "\n"
    tresc += "Bot sprawdza OLX automatycznie.\n"

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(odbiorcy)
    msg["Subject"] = (
        f"OLX Alert — "
        f"{len(nowe_oferty)} nowych, {len(zmienione_oferty)} zmian cen"
    )
    msg.attach(MIMEText(tresc, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, odbiorcy, msg.as_string())
        print(f"Mail wysłany do: {', '.join(odbiorcy)}")
    except Exception as e:
        print(f"Błąd wysyłania maila: {e}")
        raise

def sprawdz_rynek(url, rynek, cena_m2_max, seen):
    nowe = []
    zmienione = []
    nazwa = "wtórny" if rynek == "secondary" else "pierwotny"
    oferty = pobierz_oferty(url)
    print(f"Rynek {nazwa}: znaleziono {len(oferty)} ofert w przedziale m²")
    for o in oferty:
        oferta_id = o["id"]
        cena_m2 = o["cena_m2"]
        if oferta_id in seen:
            stara_cena_m2 = seen[oferta_id]["cena_m2"]
            if cena_m2 != stara_cena_m2:
                seen[oferta_id]["cena_m2"] = cena_m2
                if cena_m2 <= cena_m2_max:
                    o["stara_cena_m2"] = stara_cena_m2
                    zmienione.append(o)
        else:
            seen[oferta_id] = {"tytul": o["tytul"], "cena_m2": cena_m2, "rynek": nazwa}
            if cena_m2 <= cena_m2_max:
                nowe.append(o)
    return nowe, zmienione

def main():
    print(f"Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    seen = load_seen()
    nowe_wtorny, zmienione_wtorny = sprawdz_rynek(URL_WTORNY, "secondary", CENA_M2_WTORNY_MAX, seen)
    nowe_pierwotny, zmienione_pierwotny = sprawdz_rynek(URL_PIERWOTNY, "primary", CENA_M2_PIERWOTNY_MAX, seen)
    wszystkie_nowe = nowe_wtorny + nowe_pierwotny
    wszystkie_zmienione = zmienione_wtorny + zmienione_pierwotny
    if wszystkie_nowe or wszystkie_zmienione:
        wyslij_maila(wszystkie_nowe, wszystkie_zmienione)
    else:
        print("Brak nowych ofert spełniających kryteria.")
    save_seen(seen)
    print("Gotowe.")

if __name__ == "__main__":
    main()
