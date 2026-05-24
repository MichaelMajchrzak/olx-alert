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

def znajdz_ads_w_dict(data, depth=0):
    """
    Rekurencyjnie szuka klucza 'ads' w zagnieżdżonym słowniku.
    Zwraca pierwszą listę która wygląda jak lista ofert (ma id i title).
    """
    if depth > 10:
        return None
    if isinstance(data, dict):
        # Sprawdzamy czy ten słownik ma klucz 'ads' z listą
        if "ads" in data and isinstance(data["ads"], list) and len(data["ads"]) > 0:
            # Sprawdzamy czy to wygląda jak oferty OLX
            pierwszy = data["ads"][0]
            if isinstance(pierwszy, dict) and "id" in pierwszy:
                return data["ads"]
        # Szukamy rekurencyjnie w wartościach
        for key, val in data.items():
            wynik = znajdz_ads_w_dict(val, depth + 1)
            if wynik:
                return wynik
    elif isinstance(data, list):
        for item in data:
            wynik = znajdz_ads_w_dict(item, depth + 1)
            if wynik:
                return wynik
    return None

def pobierz_oferty(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Błąd pobierania strony: {e}")
        return []

    print(f"Pobrano stronę: {len(resp.text)} znaków")
    soup = BeautifulSoup(resp.text, "html.parser")

    # Szukamy tagu olx-init-config który zawiera 3MB danych
    script_tag = soup.find("script", {"id": "olx-init-config"})
    if not script_tag or not script_tag.string:
        print("Brak tagu olx-init-config")
        return []

    raw = script_tag.string.strip()

    # OLX zapisuje tam: window.__INIT_CONFIG__ = "{ JSON jako string }"
    # Musimy wyciągnąć wartość po = i odpakować podwójną serializację
    match = re.search(r'window\.__INIT_CONFIG__\s*=\s*"(.+)";\s*$', raw, re.DOTALL)
    if not match:
        # Próbujemy bez cudzysłowów
        match = re.search(r'window\.__INIT_CONFIG__\s*=\s*(\{.+\})\s*;?\s*$', raw, re.DOTALL)
        if not match:
            print("Nie znaleziono __INIT_CONFIG__ w tagu")
            print(f"Pierwsze 300 znaków tagu: {raw[:300]}")
            return []
        json_str = match.group(1)
    else:
        # Odpakowanie: string był serializowany dwukrotnie, odkodowujemy escape'y
        json_str = match.group(1).encode().decode("unicode_escape")

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"Błąd parsowania JSON pierwszej warstwy: {e}")
        # Może dane ofert są w innym kluczu - szukamy "listing" w surowym tekście
        # i próbujemy wyciągnąć fragment
        listing_match = re.search(r'"listing"\s*:\s*(\{[^}]{100,}\})', raw[:50000])
        if listing_match:
            print(f"Znaleziono fragment listing: {listing_match.group(0)[:200]}")
        return []

    print(f"Sparsowano JSON, klucze główne: {list(data.keys())[:10]}")

    # Sprawdzamy czy jest klucz listing lub podobny
    for key in data.keys():
        if "listing" in key.lower() or "offer" in key.lower() or "ad" in key.lower():
            print(f"Interesujący klucz: {key} -> typ: {type(data[key])}")

    # Szukamy listy ofert rekurencyjnie
    ads = znajdz_ads_w_dict(data)

    if not ads:
        print(f"Nie znaleziono listy ofert. Klucze główne: {list(data.keys())}")
        # Sprawdzamy czy wartości kluczy to stringi (kolejna warstwa serializacji)
        for key, val in list(data.items())[:5]:
            if isinstance(val, str) and len(val) > 100:
                print(f"Klucz '{key}' zawiera string długości {len(val)}, próba parsowania...")
                try:
                    subdata = json.loads(val)
                    sub_ads = znajdz_ads_w_dict(subdata)
                    if sub_ads:
                        print(f"Znaleziono oferty w kluczu '{key}'!")
                        ads = sub_ads
                        break
                except json.JSONDecodeError:
                    pass
        if not ads:
            return []

    print(f"Znaleziono {len(ads)} ogłoszeń łącznie")

    oferty = []
    for ad in ads:
        try:
            oferta_id = str(ad.get("id", ""))
            tytul = ad.get("title", "Brak tytułu")
            link = ad.get("url", "")

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

            if cena_raw is None or powierzchnia_raw is None:
                continue

            cena = float(
                str(cena_raw)
                .replace(" ", "")
                .replace("\xa0", "")
                .replace(",", ".")
            )
            powierzchnia = float(str(powierzchnia_raw).replace(",", "."))

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
