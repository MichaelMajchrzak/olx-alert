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
# USTAWIENIA
# ============================================================

POWIERZCHNIA_MIN = 35
POWIERZCHNIA_MAX = 45
CENA_M2_WTORNY_MAX = 8000
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
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def znajdz_ads_w_dict(data, depth=0):
    """Rekurencyjnie szuka listy 'ads' z obiektami mającymi 'id'."""
    if depth > 12:
        return None
    if isinstance(data, dict):
        if "ads" in data and isinstance(data["ads"], list) and data["ads"]:
            pierwszy = data["ads"][0]
            if isinstance(pierwszy, dict) and "id" in pierwszy:
                return data["ads"]
        for val in data.values():
            wynik = znajdz_ads_w_dict(val, depth + 1)
            if wynik:
                return wynik
    elif isinstance(data, list):
        for item in data:
            wynik = znajdz_ads_w_dict(item, depth + 1)
            if wynik:
                return wynik
    return None


def wyciagnij_js_string(text, start_idx):
    """
    Od pozycji start_idx (powinien wskazywać na otwierający cudzysłów ")
    czyta string JS z poszanowaniem backslash-escape'ów i zwraca
    (zawartość_bez_cudzyslowow, indeks_po_zamykajacym_cudzyslowie).
    """
    assert text[start_idx] == '"'
    i = start_idx + 1
    n = len(text)
    out = []
    while i < n:
        ch = text[i]
        if ch == "\\":
            # zachowujemy escape razem z następnym znakiem (rozwiążemy go potem)
            if i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            else:
                break
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ValueError("Nie znaleziono zamykającego cudzysłowu")


def pobierz_oferty(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Błąd pobierania strony: {e}")
        return []

    html = resp.text
    print(f"Pobrano stronę: {len(html)} znaków")

    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", {"id": "olx-init-config"})
    if not script_tag or not script_tag.string:
        print("Brak tagu olx-init-config")
        return []

    raw = script_tag.string

    # Szukamy konkretnie __PRERENDERED_STATE__ - tu siedzą oferty.
    # Tag zawiera kilka przypisań po sobie:
    #   window.__PRERENDERED_STATE__ = "...";
    #   window.__INIT_CONFIG__ = "...";
    # Stary kod brał wszystko do końca - stąd "Extra data" przy json.loads.
    kandydaci = ["__PRERENDERED_STATE__", "__INIT_CONFIG__"]
    ads = None

    for nazwa in kandydaci:
        m = re.search(r'window\.' + re.escape(nazwa) + r'\s*=\s*"', raw)
        if not m:
            continue
        try:
            escapowany, _ = wyciagnij_js_string(raw, m.end() - 1)
        except ValueError as e:
            print(f"{nazwa}: błąd wycinania stringu: {e}")
            continue

        # JS-owe escape'y -> prawdziwe znaki.
        # encode('latin-1','backslashreplace') żeby zachować bajty unicode_escape
        try:
            json_str = escapowany.encode("utf-8").decode("unicode_escape")
            # po unicode_escape mogą zostać złamane bajty UTF-8, naprawiamy:
            json_str = json_str.encode("latin-1", "ignore").decode("utf-8", "ignore")
        except Exception as e:
            print(f"{nazwa}: błąd dekodowania escape: {e}")
            continue

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"{nazwa}: JSONDecodeError {e}; długość: {len(json_str)}")
            continue

        ads = znajdz_ads_w_dict(data)
        if ads:
            print(f"Znaleziono ogłoszenia w {nazwa}: {len(ads)} szt.")
            break
        else:
            print(f"{nazwa}: sparsowano JSON, ale brak listy 'ads'. "
                  f"Klucze top: {list(data.keys())[:10] if isinstance(data, dict) else type(data)}")

    if not ads:
        print("Nie udało się wyciągnąć listy ofert z żadnego źródła.")
        return []

    def liczba_z_tekstu(x):
        """'43,34 m²' -> 43.34 ; '10 613,75 zł/m²' -> 10613.75 ; None -> None"""
        if x is None:
            return None
        s = str(x).replace("\xa0", " ")
        # zostawiamy cyfry, kropki, przecinki i minus
        s = re.sub(r"[^\d,.\-]", "", s)
        if not s:
            return None
        # jeśli są oba separatory, ostatni traktujemy jako dziesiętny
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    def wyciagnij_cene_calkowita(ad):
        """Cena całkowita siedzi w ad['price'] (różne warianty struktur OLX)."""
        p = ad.get("price")
        if isinstance(p, dict):
            for k in ("regularPrice", "displayValue"):
                v = p.get(k)
                if isinstance(v, dict):
                    val = v.get("value")
                    if val is not None:
                        return liczba_z_tekstu(val)
                elif v is not None:
                    return liczba_z_tekstu(v)
            # czasem prosto: {"value": 350000}
            if p.get("value") is not None:
                return liczba_z_tekstu(p.get("value"))
        elif p is not None:
            return liczba_z_tekstu(p)
        return None

    oferty = []
    debug_wypisane = False
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
                    params_dict[key] = val.get("value") if val.get("value") is not None else val.get("key")
                else:
                    params_dict[key] = val

            if not debug_wypisane:
                print(f"DEBUG params pierwszej oferty: {list(params_dict.keys())}")
                print(f"DEBUG wartosci: {params_dict}")
                debug_wypisane = True

            # Powierzchnia: '43,34 m²' -> 43.34
            powierzchnia = liczba_z_tekstu(
                params_dict.get("m")
                or params_dict.get("area")
                or params_dict.get("floor_area")
            )
            if powierzchnia is None:
                continue
            if not (POWIERZCHNIA_MIN <= powierzchnia <= POWIERZCHNIA_MAX):
                continue

            # Cena/m² - OLX podaje gotową w 'price_per_m'
            cena_m2 = liczba_z_tekstu(params_dict.get("price_per_m"))

            # Cena całkowita - z ad['price'], a jeśli brak to liczona z cena_m2 * m
            cena = wyciagnij_cene_calkowita(ad)
            if cena is None and cena_m2 is not None:
                cena = cena_m2 * powierzchnia

            # Jeśli nie mamy cena_m2, ale mamy cenę całkowitą - liczymy
            if cena_m2 is None and cena is not None:
                cena_m2 = cena / powierzchnia

            if cena_m2 is None:
                continue

            cena_m2 = round(cena_m2)
            cena_int = int(cena) if cena is not None else 0

            oferty.append({
                "id": oferta_id,
                "tytul": tytul,
                "url": link,
                "cena": cena_int,
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
    odbiorcy = [a.strip() for a in os.environ["NOTIFY_EMAILS"].split(",") if a.strip()]

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
        f"OLX Alert — {len(nowe_oferty)} nowych, {len(zmienione_oferty)} zmian cen"
    )
    msg.attach(MIMEText(tresc, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, odbiorcy, msg.as_string())
    print(f"Mail wysłany do: {', '.join(odbiorcy)}")


def sprawdz_rynek(url, rynek, cena_m2_max, seen):
    nowe, zmienione = [], []
    nazwa = "wtórny" if rynek == "secondary" else "pierwotny"
    oferty = pobierz_oferty(url)
    print(f"Rynek {nazwa}: {len(oferty)} ofert w przedziale m²")
    for o in oferty:
        oid = o["id"]
        cena_m2 = o["cena_m2"]
        if oid in seen:
            stara = seen[oid]["cena_m2"]
            if cena_m2 != stara:
                seen[oid]["cena_m2"] = cena_m2
                if cena_m2 <= cena_m2_max:
                    o["stara_cena_m2"] = stara
                    zmienione.append(o)
                    print(f"Zmiana ceny: {o['tytul']} — {stara} → {cena_m2} zł/m²")
        else:
            seen[oid] = {"tytul": o["tytul"], "cena_m2": cena_m2, "rynek": nazwa}
            if cena_m2 <= cena_m2_max:
                nowe.append(o)
                print(f"Nowa oferta: {o['tytul']} — {cena_m2} zł/m²")
    return nowe, zmienione


def main():
    print(f"Start: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    seen = load_seen()
    nowe_w, zmien_w = sprawdz_rynek(URL_WTORNY, "secondary", CENA_M2_WTORNY_MAX, seen)
    nowe_p, zmien_p = sprawdz_rynek(URL_PIERWOTNY, "primary", CENA_M2_PIERWOTNY_MAX, seen)
    wszystkie_nowe = nowe_w + nowe_p
    wszystkie_zmienione = zmien_w + zmien_p
    if wszystkie_nowe or wszystkie_zmienione:
        wyslij_maila(wszystkie_nowe, wszystkie_zmienione)
    else:
        print("Brak nowych ofert spełniających kryteria.")
    save_seen(seen)
    print("Gotowe.")


if __name__ == "__main__":
    main()
