import requests
from bs4 import BeautifulSoup
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ============================================================
# USTAWIENIA - tutaj zmieniaj parametry bez dotykania reszty
# ============================================================

POWIERZCHNIA_MIN = 35       # minimalna powierzchnia w m²
POWIERZCHNIA_MAX = 45       # maksymalna powierzchnia w m²

CENA_M2_WTORNY_MAX = 7800   # max cena za m² dla rynku wtórnego
CENA_M2_PIERWOTNY_MAX = 9000 # max cena za m² dla rynku pierwotnego

# URL-e do scrapowania - oddzielnie wtórny i pierwotny
# Jeśli chcesz zmienić dzielnicę/miasto, wejdź na OLX, ustaw filtry
# ręcznie i skopiuj URL z przeglądarki w to miejsce
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

# Plik w którym bot zapamiętuje już widziane oferty
SEEN_FILE = "seen_offers.json"

# ============================================================
# FUNKCJE - tutaj już nie musisz nic zmieniać
# ============================================================

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
    """Pobiera oferty z OLX i zwraca listę słowników z danymi."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Błąd pobierania strony: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    oferty = []

    # OLX trzyma dane ofert w tagu <script> jako JSON - szukamy tego tagu
    for script in soup.find_all("script"):
        if script.string and "window.__PRERENDERED_STATE__" in (script.string or ""):
            try:
                # Wyciągamy JSON z tagu script
                raw = script.string.split("window.__PRERENDERED_STATE__= ")[1]
                raw = raw.split(";\n")[0]
                data = json.loads(raw)

                # Nawigujemy do listy ofert w strukturze JSON
                listings = (
                    data.get("listing", {})
                    .get("listing", {})
                    .get("ads", [])
                )

                for ad in listings:
                    try:
                        oferta_id = str(ad.get("id", ""))
                        tytul = ad.get("title", "Brak tytułu")
                        link = ad.get("url", "")

                        # Wyciągamy cenę i powierzchnię z parametrów oferty
                        params = {
                            p["key"]: p.get("value", {}).get("value")
                            for p in ad.get("params", [])
                            if "value" in p
                        }

                        cena = params.get("price")
                        powierzchnia = params.get("m")

                        # Pomijamy oferty bez ceny lub powierzchni
                        if not cena or not powierzchnia:
                            continue

                        cena = float(str(cena).replace(" ", "").replace(",", "."))
                        powierzchnia = float(str(powierzchnia).replace(",", "."))

                        # Sprawdzamy czy powierzchnia mieści się w przedziale
                        if not (POWIERZCHNIA_MIN <= powierzchnia <= POWIERZCHNIA_MAX):
                            continue

                        cena_m2 = round(cena / powierzchnia)

                        oferty.append({
                            "id": oferta_id,
                            "tytul": tytul,
                            "link": link,
                            "cena": cena,
                            "powierzchnia": powierzchnia,
                            "cena_m2": cena_m2,
                        })
                    except (ValueError, TypeError, KeyError):
                        # Pomijamy oferty z nieprawidłowymi danymi
                        continue

            except (IndexError, json.JSONDecodeError, KeyError):
                continue

    return oferty

def wyslij_maila(nowe_oferty, zmienione_oferty):
    """Wysyła maila z nowymi i zmienionymi ofertami."""
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_PASSWORD"]
    odbiorcy = os.environ["NOTIFY_EMAILS"].split(",")

    # Budujemy treść maila
    teraz = datetime.now().strftime("%d.%m.%Y %H:%M")
    tresc = f"OLX Alert — {teraz}\n"
    tresc += "=" * 50 + "\n\n"

    if nowe_oferty:
        tresc += f"🆕 NOWE OFERTY ({len(nowe_oferty)}):\n\n"
        for o in nowe_oferty:
            tresc += f"📌 {o['tytul']}\n"
            tresc += f"   Cena: {int(o['cena']):,} zł\n".replace(",", " ")
            tresc += f"   Powierzchnia: {o['powierzchnia']} m²\n"
            tresc += f"   Cena/m²: {o['cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"   Link: {o['link']}\n\n"

    if zmienione_oferty:
        tresc += f"📉 ZMIANY CEN ({len(zmienione_oferty)}):\n\n"
        for o in zmienione_oferty:
            tresc += f"📌 {o['tytul']}\n"
            tresc += f"   Stara cena/m²: {o['stara_cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"   Nowa cena/m²: {o['cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"   Powierzchnia: {o['powierzchnia']} m²\n"
            tresc += f"   Link: {o['link']}\n\n"

    tresc += "=" * 50 + "\n"
    tresc += "Bot sprawdza OLX co 10 minut automatycznie.\n"

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(odbiorcy)
    msg["Subject"] = (
        f"OLX Alert Łódź — "
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

def sprawdz_oferty(url, rynek, cena_m2_max, seen):
    """
    Główna funkcja sprawdzająca oferty.
    rynek: "wtórny" lub "pierwotny"
    Zwraca listy nowych i zmienionych ofert.
    """
    nowe = []
    zmienione = []

    oferty = pobierz_oferty(url)
    print(f"Rynek {rynek}: znaleziono {len(oferty)} ofert w przedziale m²")

    for o in oferty:
        oferta_id = o["id"]
        cena_m2 = o["cena_m2"]

        if oferta_id in seen:
            # Oferta już widziana — sprawdzamy czy cena się zmieniła
            stara_cena_m2 = seen[oferta_id]["cena_m2"]

            if cena_m2 != stara_cena_m2:
                # Cena się zmieniła — aktualizujemy i sprawdzamy czy spełnia warunek
                seen[oferta_id]["cena_m2"] = cena_m2
                if cena_m2 <= cena_m2_max:
                    o["stara_cena_m2"] = stara_cena_m2
                    zmienione.append(o)
                    print(f"Zmiana ceny: {o['tytul']} — {stara_cena_m2} → {cena_m2} zł/m²")
        else:
            # Nowa oferta — zapamiętujemy ją
            seen[oferta_id] = {
                "tytul": o["tytul"],
                "cena_m2": cena_m2,
                "rynek": rynek,
            }
            # Wysyłamy powiadomienie tylko jeśli spełnia warunek cenowy
            if cena_m2 <= cena_m2_max:
                nowe.append(o)
                print(f"Nowa oferta: {o['tytul']} — {cena_m2} zł/m²")

    return nowe, zmienione

def main():
    print(f"Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    # Wczytujemy zapamiętane oferty
    seen = load_seen()

    # Sprawdzamy oba rynki
    nowe_wtorny, zmienione_wtorny = sprawdz_oferty(
        URL_WTORNY, "wtórny", CENA_M2_WTORNY_MAX, seen
    )
    nowe_pierwotny, zmienione_pierwotny = sprawdz_oferty(
        URL_PIERWOTNY, "pierwotny", CENA_M2_PIERWOTNY_MAX, seen
    )

    # Łączymy wyniki z obu rynków
    wszystkie_nowe = nowe_wtorny + nowe_pierwotny
    wszystkie_zmienione = zmienione_wtorny + zmienione_pierwotny

    # Wysyłamy maila tylko jeśli jest coś do wysłania
    if wszystkie_nowe or wszystkie_zmienione:
        wyslij_maila(wszystkie_nowe, wszystkie_zmienione)
    else:
        print("Brak nowych ofert spełniających kryteria.")

    # Zapisujemy zaktualizowane dane
    save_seen(seen)
    print("Gotowe.")

if __name__ == "__main__":
    main()
