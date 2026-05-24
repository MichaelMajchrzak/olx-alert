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

POWIERZCHNIA_MIN = 35        # minimalna powierzchnia w m²
POWIERZCHNIA_MAX = 45        # maksymalna powierzchnia w m²

CENA_M2_WTORNY_MAX = 7800    # max cena za m² dla rynku wtórnego
CENA_M2_PIERWOTNY_MAX = 9000 # max cena za m² dla rynku pierwotnego

# URL-e skopiowane bezpośrednio z przeglądarki po ustawieniu filtrów
# Jeśli chcesz zmienić dzielnicę/filtry - wejdź na OLX, ustaw filtry
# ręcznie i skopiuj nowy URL tutaj
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

# ============================================================
# FUNKCJE - tutaj już nie musisz nic zmieniać
# ============================================================

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
    """Wczytuje zapamiętane oferty z pliku JSON."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_seen(seen):
    """Zapisuje oferty do pliku JSON."""
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)

def pobierz_oferty(url):
    """
    Pobiera oferty z OLX przez scraping strony HTML.
    OLX wbudowuje dane ofert jako JSON w tagu <script> na stronie.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Błąd pobierania strony: {e}")
        return []

    # Tymczasowe debugowanie - zapisujemy HTML do pliku
    with open("debug_olx.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
    print(f"Pobrano stronę: {len(resp.text)} znaków, status: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")

    # OLX wbudowuje wszystkie dane ofert w JSON wewnątrz tagu <script id="olx-init-config">
    # lub jako window.__PRERENDERED_STATE__ - szukamy obu
    raw_json = None

    # Metoda 1: szukamy tagu script z id="olx-init-config"
    script_tag = soup.find("script", {"id": "olx-init-config"})
    if script_tag and script_tag.string:
        raw_json = script_tag.string

    # Metoda 2: szukamy window.__PRERENDERED_STATE__ w dowolnym tagu script
    if not raw_json:
        for script in soup.find_all("script"):
            if script.string and "__PRERENDERED_STATE__" in script.string:
                match = re.search(
                    r'window\.__PRERENDERED_STATE__\s*=\s*({.*?});?\s*\n',
                    script.string,
                    re.DOTALL
                )
                if match:
                    raw_json = match.group(1)
                    break

    # Metoda 3: szukamy nextjs __NEXT_DATA__
    if not raw_json:
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if script_tag and script_tag.string:
            raw_json = script_tag.string

    if not raw_json:
        print("Nie znaleziono danych JSON - lista tagów script na stronie:")
        for i, s in enumerate(soup.find_all("script")):
            sid = s.get("id", "brak-id")
            stype = s.get("type", "brak-type")
            content_preview = (s.string or "")[:80].replace("\n", " ")
            print(f"  script[{i}] id={sid} type={stype} | {content_preview}")
        return []

    # Parsujemy JSON i nawigujemy do listy ofert
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"Błąd parsowania JSON: {e}")
        return []

    # Szukamy listy ofert w różnych miejscach struktury JSON
    # (OLX czasem zmienia strukturę)
    ads = []

    # Próba 1: standardowa ścieżka
    ads = (
        data.get("listing", {})
        .get("listing", {})
        .get("ads", [])
    )

    # Próba 2: ścieżka nextjs
    if not ads:
        ads = (
            data.get("props", {})
            .get("pageProps", {})
            .get("ads", [])
        )

    # Próba 3: płaska lista
    if not ads:
        ads = data.get("ads", [])

    if not ads:
        print(f"Znaleziono JSON ale brak listy ofert. Klucze: {list(data.keys())}")
        return []

    oferty = []

    for ad in ads:
        try:
            oferta_id = str(ad.get("id", ""))
            tytul = ad.get("title", "Brak tytułu")
            link = ad.get("url", "")

            # Wyciągamy parametry oferty (cena, powierzchnia)
            params_dict = {}
            for p in ad.get("params", []):
                key = p.get("key", "")
                val = p.get("value", {})
                if isinstance(val, dict):
                    params_dict[key] = val.get("value")
                else:
                    params_dict[key] = val

            cena_raw = params_dict.get("price")
            powierzchnia_raw = params_dict.get("m")

            # Pomijamy oferty bez ceny lub powierzchni
            if cena_raw is None or powierzchnia_raw is None:
                continue

            # Czyścimy i konwertujemy wartości
            cena = float(
                str(cena_raw)
                .replace(" ", "")
                .replace("\xa0", "")
                .replace(",", ".")
            )
            powierzchnia = float(
                str(powierzchnia_raw)
                .replace(",", ".")
            )

            if not (POWIERZCHNIA_MIN <= powierzchnia <= POWIERZCHNIA_MAX):
                continue

            cena_m2 = round(cena / powierzchnia)

            oferty.append({
                "id": oferta_id,
                "tytul": tytul,
                "url": link,
                "cena": int(cena),
                "powierzchnia": powierzchnia,
                "cena_m2": cena_m2,
            })

        except (ValueError, TypeError, KeyError) as e:
            print(f"Pomijam ofertę z błędem: {e}")
            continue

    return oferty

def wyslij_maila(nowe_oferty, zmienione_oferty):
    """Wysyła maila z nowymi i zmienionymi ofertami."""
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
    tresc += "Bot sprawdza OLX automatycznie co 10 minut.\n"

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
    """
    Sprawdza oferty dla jednego rynku.
    Zwraca listy nowych i zmienionych ofert.
    """
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
                    print(f"Zmiana ceny: {o['tytul']} — {stara_cena_m2} → {cena_m2} zł/m²")
        else:
            seen[oferta_id] = {
                "tytul": o["tytul"],
                "cena_m2": cena_m2,
                "rynek": nazwa,
            }
            if cena_m2 <= cena_m2_max:
                nowe.append(o)
                print(f"Nowa oferta: {o['tytul']} — {cena_m2} zł/m²")

    return nowe, zmienione

def main():
    print(f"Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    seen = load_seen()

    nowe_wtorny, zmienione_wtorny = sprawdz_rynek(
        URL_WTORNY, "secondary", CENA_M2_WTORNY_MAX, seen
    )
    nowe_pierwotny, zmienione_pierwotny = sprawdz_rynek(
        URL_PIERWOTNY, "primary", CENA_M2_PIERWOTNY_MAX, seen
    )

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
