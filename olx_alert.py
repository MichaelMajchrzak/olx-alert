import requests
import json
import os
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

# ID dzielnicy Polesie w Łodzi = 295
# Jeśli chcesz zmienić dzielnicę, wejdź na OLX, ustaw filtry
# i sprawdź parametr search[district_id] w URL przeglądarki
DISTRICT_ID = 295

# ============================================================
# FUNKCJE - tutaj już nie musisz nic zmieniać
# ============================================================

SEEN_FILE = "seen_offers.json"

# Nagłówki udające prawdziwą przeglądarkę
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
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

def pobierz_oferty(rynek):
    """
    Pobiera oferty z API OLX.
    rynek: "secondary" (wtórny) lub "primary" (pierwotny)
    Zwraca listę słowników z danymi ofert.
    """
    # OLX ma publiczne API którego używa własna strona
    # Parametry są identyczne jak w URL przeglądarki
    api_url = "https://www.olx.pl/api/v1/offers/"
    params = {
        "offset": 0,
        "limit": 50,                        # ile ofert pobieramy na raz
        "category_id": 15,                  # 15 = mieszkania
        "region_id": 7,                     # 7 = łódź (województwo)
        "city_id": 93063,                   # 93063 = Łódź (miasto)
        "district_id": DISTRICT_ID,         # 295 = Polesie
        "filter_float_m:from": POWIERZCHNIA_MIN,
        "filter_float_m:to": POWIERZCHNIA_MAX,
        "filter_enum_market[0]": rynek,
        "filter_enum_type[0]": "sell",      # tylko sprzedaż
        "sort_by": "created_at:desc",       # najnowsze pierwsze
    }

    try:
        resp = requests.get(api_url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"Błąd pobierania API ({rynek}): {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"Błąd parsowania JSON ({rynek}): {e}")
        return []

    oferty = []

    for ad in data.get("data", []):
        try:
            oferta_id = str(ad.get("id", ""))
            tytul = ad.get("title", "Brak tytułu")
            url = ad.get("url", "")

            # Wyciągamy cenę i powierzchnię z parametrów
            params_dict = {}
            for p in ad.get("params", []):
                key = p.get("key", "")
                # Cena jest w value.value, powierzchnia też
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

            cena = float(str(cena_raw).replace(" ", "").replace("\xa0", "").replace(",", "."))
            powierzchnia = float(str(powierzchnia_raw).replace(",", "."))

            # Sprawdzamy czy powierzchnia mieści się w przedziale
            # (API powinno to filtrować ale lepiej sprawdzić)
            if not (POWIERZCHNIA_MIN <= powierzchnia <= POWIERZCHNIA_MAX):
                continue

            cena_m2 = round(cena / powierzchnia)

            oferty.append({
                "id": oferta_id,
                "tytul": tytul,
                "url": url,
                "cena": int(cena),
                "powierzchnia": powierzchnia,
                "cena_m2": cena_m2,
            })

        except (ValueError, TypeError, KeyError) as e:
            # Pomijamy oferty z nieprawidłowymi danymi
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
            tresc += f"  Cena:        {o['cena']:,} zł\n".replace(",", " ")
            tresc += f"  Powierzchnia: {o['powierzchnia']} m²\n"
            tresc += f"  Cena/m²:     {o['cena_m2']:,} zł\n".replace(",", " ")
            tresc += f"  Link:        {o['url']}\n\n"

    if zmienione_oferty:
        tresc += f"ZMIANY CEN ({len(zmienione_oferty)}):\n\n"
        for o in zmienione_oferty:
            roznica = o['stara_cena_m2'] - o['cena_m2']
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

def sprawdz_rynek(rynek, cena_m2_max, seen):
    """
    Sprawdza oferty dla jednego rynku i zwraca nowe i zmienione.
    rynek: "secondary" lub "primary"
    """
    nowe = []
    zmienione = []
    nazwa = "wtórny" if rynek == "secondary" else "pierwotny"

    oferty = pobierz_oferty(rynek)
    print(f"Rynek {nazwa}: znaleziono {len(oferty)} ofert w przedziale m²")

    for o in oferty:
        oferta_id = o["id"]
        cena_m2 = o["cena_m2"]

        if oferta_id in seen:
            # Oferta już widziana — sprawdzamy czy cena się zmieniła
            stara_cena_m2 = seen[oferta_id]["cena_m2"]

            if cena_m2 != stara_cena_m2:
                seen[oferta_id]["cena_m2"] = cena_m2
                # Powiadamiamy tylko jeśli nowa cena spełnia warunek
                if cena_m2 <= cena_m2_max:
                    o["stara_cena_m2"] = stara_cena_m2
                    zmienione.append(o)
                    print(f"Zmiana ceny: {o['tytul']} — {stara_cena_m2} → {cena_m2} zł/m²")
        else:
            # Nowa oferta — zawsze zapamiętujemy
            seen[oferta_id] = {
                "tytul": o["tytul"],
                "cena_m2": cena_m2,
                "rynek": nazwa,
            }
            # Powiadamiamy tylko jeśli spełnia warunek cenowy
            if cena_m2 <= cena_m2_max:
                nowe.append(o)
                print(f"Nowa oferta: {o['tytul']} — {cena_m2} zł/m²")

    return nowe, zmienione

def main():
    print(f"Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    seen = load_seen()

    nowe_wtorny, zmienione_wtorny = sprawdz_rynek(
        "secondary", CENA_M2_WTORNY_MAX, seen
    )
    nowe_pierwotny, zmienione_pierwotny = sprawdz_rynek(
        "primary", CENA_M2_PIERWOTNY_MAX, seen
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
