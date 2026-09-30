#!/usr/bin/env python3
"""
STREAMING-KATALÓGUS DE/HU – adatgyűjtő szkript (v3: filmek + sorozatok)
=======================================================================
Mit csinál? Megkérdezi a TMDB-t, mely filmek és sorozatok futnak a német és
a magyar Netflixen, Prime Videón, Disney+-on és HBO Maxon, majd az OMDb-től
elkéri az értékeléseket. Két fájlba ír:
  data/movies.json  – filmek
  data/series.json  – sorozatok
Semmit SEM töröl, csak megjelöli, ha egy szolgáltatónál már nem elérhető.

Naponta egyszer fut a GitHub Actions által (lásd .github/workflows/update.yml).
Részletes magyarázat: DOKUMENTACIO.md
"""

# --- import: kész eszközök betöltése ---------------------------------------
# json      = adat szöveggé és vissza      | os   = környezeti változók, fájlok
# sys       = kilépési kód                 | time = várakozás két kérés között
# urllib.*  = internetes lekérés           | datetime = dátumszámítás
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta

# --- beállítások (konstansok) ----------------------------------------------
# A kulcsokat NEM ide írjuk! A GitHub Secrets-ből érkeznek környezeti változóként.
TMDB_KEY = os.environ["TMDB_API_KEY"]            # kötelező, enélkül leáll
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")    # opcionális: enélkül nincs értékelés
OMDB_LIMIT = int(os.environ.get("OMDB_LIMIT", "950"))  # ingyenes keret: 1000/nap
RATING_MAX_AGE_DAYS = 45      # ennyi nap után frissítjük újra egy értékelést
TV_DETAIL_MAX_AGE_DAYS = 7    # futó sorozat adatlapját (évadok!) ennyi naponként frissítjük
REGIONS = ["DE", "HU"]        # országkódok: Németország, Magyarország
TODAY = date.today().isoformat()  # mai dátum "2026-09-26" formában

# --- a két "fajta": film és sorozat ------------------------------------------
# Minden, ami a kettő között különbözik, itt van egy helyen. A program többi
# része ugyanaz mindkettőre, csak a megfelelő beállítást kapja meg.
KINDS = {
    "movie": {
        "label": "Filmek",
        "file": "data/movies.json", "key": "movies",   # fájl és a lista neve benne
        "tmdb": "movie",                               # /discover/movie, /movie/{id}
        "date": "primary_release_date",                # szűrés dátum szerint
        "title": "title", "orig": "original_title", "released": "release_date",
    },
    "tv": {
        "label": "Sorozatok",
        "file": "data/series.json", "key": "items",
        "tmdb": "tv",                                  # /discover/tv, /tv/{id}
        "date": "first_air_date",
        "title": "name", "orig": "original_name", "released": "first_air_date",
    },
}

# --- a szolgáltatók ----------------------------------------------------------
# A TMDB-nél egy szolgáltatónak több bejegyzése is lehet (pl. "Netflix" és
# "Netflix Standard with Ads"). Ezért nem fix azonosítókat írunk be, hanem
# NÉV alapján keressük meg őket országonként.
#   kulcs  -> (megjelenített név, ellenőrző függvény a TMDB-névre)
SERVICES = {
    "netflix": ("Netflix",     lambda n: n.startswith("netflix")),
    "prime":   ("Prime Video", lambda n: n.startswith("amazon prime video")),
    "disney":  ("Disney+",     lambda n: n.startswith("disney plus") or n.startswith("disney+")),
    "hbo":     ("HBO Max",     lambda n: n.startswith("hbo max") or n == "max"),
}


# --- alapfüggvények ---------------------------------------------------------

def get_json(url, retries=3):
    """Letölt egy címet és JSON-ként adja vissza. Hiba esetén újrapróbálja.
    retries = hányszor próbálkozzon összesen; a szünet minden körben nő."""
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == retries - 1:   # utolsó próbálkozás is elbukott -> hiba tovább
                raise
            print(f"  Hiba ({e}), újra …")
            time.sleep(3 * (i + 1))


def tmdb(path, **params):
    """TMDB-hívás. A kulcsot automatikusan hozzáteszi.
    Példa: tmdb("/movie/550", language="hu-HU")"""
    params["api_key"] = TMDB_KEY
    # urlencode: a paraméterekből "a=1&b=2" formájú szöveget csinál
    return get_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}")


def resolve_services(region, tmdb_kind):
    """Megkeresi, melyik TMDB-azonosító melyik szolgáltatóhoz tartozik az
    adott országban. Visszaad: {"netflix": {"ids": [8, 1796], "logo": "/x.jpg"}, ...}
    A "Channel" nevűeket kihagyjuk: azok Prime-on belüli, külön fizetős csatornák."""
    out = {}
    for p in tmdb(f"/watch/providers/{tmdb_kind}", watch_region=region).get("results", []):
        name = p.get("provider_name", "").lower()
        if "channel" in name:
            continue
        for key, (_, match) in SERVICES.items():
            if match(name):
                s = out.setdefault(key, {"ids": [], "logo": None, "len": 999})
                s["ids"].append(p["provider_id"])
                # logónak a legrövidebb nevű bejegyzését vesszük (az a "fő" csomag)
                if len(name) < s["len"]:
                    s["len"], s["logo"] = len(name), p.get("logo_path")
    for s in out.values():
        s.pop("len")
    return out


def fetch_region(cfg, region, provider_ids):
    """Egy ország egy szolgáltatójának ÖSSZES filmje vagy sorozata.

    Trükk: a TMDB egy kereséshez max. 500 oldalt ad vissza, ezért évtizedekre
    bontjuk a keresést, és minden évtizedet külön lapozunk végig.
    provider_ids: pl. [8, 1796] – a "|" jel VAGY-ot jelent a TMDB-nél.
    Visszaad: {"550": {adatok}, ...} – kulcs a TMDB azonosító szövegként."""
    found = {}
    # dátumsávok: régiek, majd 1970-2029 évtizedenként, végül a jövő
    ranges = ([(None, "1969-12-31")]
              + [(f"{y}-01-01", f"{y+9}-12-31") for y in range(1970, 2030, 10)]
              + [("2030-01-01", None)])
    for start, end in ranges:
        page, total = 1, 1
        while page <= min(total, 500):   # lapozás, de max. 500 oldal
            p = dict(
                watch_region=region,                                  # melyik ország kínálata
                with_watch_providers="|".join(map(str, provider_ids)),  # melyik szolgáltató
                with_watch_monetization_types="flatrate",  # csak előfizetésben (nem kölcsönzés/vásárlás)
                language="de-DE",                          # német címek
                sort_by="popularity.desc",
                page=page)
            if start: p[f"{cfg['date']}.gte"] = start   # gte = "nagyobb vagy egyenlő"
            if end:   p[f"{cfg['date']}.lte"] = end     # lte = "kisebb vagy egyenlő"
            d = tmdb(f"/discover/{cfg['tmdb']}", **p)
            total = d.get("total_pages", 0)
            for m in d.get("results", []):
                found[str(m["id"])] = m
            page += 1
            time.sleep(0.05)   # udvariassági szünet, nehogy kitiltson a szerver
    return found


class OmdbLimit(Exception):
    """Jelzés: elfogyott a napi OMDb-keret, ma nem kérdezünk többet."""


def omdb(imdb_id):
    """Egy film/sorozat értékelései az OMDb-től, IMDb-azonosító alapján.
    Visszaad: {"imdb": 8.8, "rt": 81, "mc": 67, "votes": 2500000}"""
    try:
        d = get_json(f"https://www.omdbapi.com/?i={imdb_id}&apikey={OMDB_KEY}", retries=1)
    except urllib.error.HTTPError as e:
        # 401 = a napi keret elfogyott (vagy rossz a kulcs). Nincs értelme újrapróbálni.
        if e.code == 401:
            raise OmdbLimit("OMDb: napi limit elérve vagy hibás kulcs – holnap folytatódik")
        raise
    except ValueError:
        # Az OMDb néha hibás JSON-t küld (pl. idézőjel a leírásban). Ilyenkor
        # üres eredményt adunk vissza, így nem próbálkozik vele minden nap újra.
        print(f"  {imdb_id}: hibás OMDb-válasz, kihagyva")
        return {}
    if d.get("Response") == "False":            # az OMDb szövegesen jelez hibát
        if "limit" in d.get("Error", "").lower():
            raise OmdbLimit("OMDb: napi limit elérve")
        return {}                               # nincs ilyen cím -> üres eredmény
    r = {"imdb": None, "rt": None, "mc": None, "votes": None}
    # try/except: ha az adat hiányzik vagy nem szám, maradjon None (üres)
    try: r["imdb"] = float(d.get("imdbRating"))          # "8.8" -> 8.8
    except (TypeError, ValueError): pass
    try: r["votes"] = int(d.get("imdbVotes", "").replace(",", ""))  # "2,500,000" -> 2500000
    except ValueError: pass
    for s in d.get("Ratings", []):
        try:
            if s["Source"] == "Rotten Tomatoes":
                r["rt"] = int(s["Value"].rstrip("%"))    # "81%" -> 81
            elif s["Source"] == "Metacritic":
                r["mc"] = int(s["Value"].split("/")[0])  # "67/100" -> 67
        except (KeyError, ValueError):
            pass
    return r


def migrate(db):
    """Átalakítás a régi (csak Netflix) formátumról a több szolgáltatós formára.
    Régi: avail = {"DE": {"first": ..., "on": ...}}
    Új:   avail = {"DE": {"netflix": {"first": ..., "on": ...}, "prime": {...}}}"""
    since = db.setdefault("since", {})
    for e in db[db["_key"]].values():
        for region, a in list(e.get("avail", {}).items()):
            if "first" in a or "on" in a:          # ez még a régi forma
                e["avail"][region] = {"netflix": a}
                key = f"{region}:netflix"
                if a.get("first") and (key not in since or a["first"] < since[key]):
                    since[key] = a["first"]


def load_db(cfg):
    """Betölti a tegnapi fájlt, vagy üreset ad, ha még nincs."""
    db = {cfg["key"]: {}}
    if os.path.exists(cfg["file"]):
        with open(cfg["file"], encoding="utf-8") as f:
            db = json.load(f)
    db["_key"] = cfg["key"]                # segédmező, mentéskor kivesszük
    migrate(db)
    db.setdefault("since", {})
    return db


def save_db(cfg, db):
    db.pop("_key", None)
    db["updated"] = datetime.now().isoformat(timespec="minutes")  # "utoljára frissítve"
    db["regions"] = REGIONS
    with open(cfg["file"], "w", encoding="utf-8") as f:
        # ensure_ascii=False: az ékezetek maradjanak olvashatók
        # separators: felesleges szóközök nélkül -> kisebb fájl
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{cfg['label']}: {len(db[cfg['key']])} tétel mentve ({cfg['file']}).")


# --- 1. LÉPÉS: mi fut most? -------------------------------------------------

def update_availability(kind, cfg, db):
    items, since = db[cfg["key"]], db["since"]
    services_info = {}
    for region in REGIONS:
        found = resolve_services(region, cfg["tmdb"])
        for svc, (label, _) in SERVICES.items():
            if svc not in found:
                print(f"{label} {region}: nincs ilyen szolgáltató a TMDB-nél, kihagyva.")
                continue
            ids = found[svc]["ids"]
            info = services_info.setdefault(svc, {"name": label, "logo": None})
            info["logo"] = info["logo"] or found[svc]["logo"]
            print(f"{label} {region} (TMDB: {ids}) betöltése …")
            current = fetch_region(cfg, region, ids)   # a friss lista az internetről
            prev = sum(1 for m in items.values()
                       if m["avail"].get(region, {}).get(svc, {}).get("on"))
            # Ha a lista 3%-nál többel kisebb a korábbinál, az gyakran csak egy
            # pillanatnyi TMDB-kimaradás. Ilyenkor még egyszer lekérjük, és a
            # két eredményt összefésüljük.
            if prev > 100 and len(current) < 0.97 * prev:
                print(f"  {len(current)} találat (korábban {prev}) – gyanúsan kevés, újra lekérem …")
                current.update(fetch_region(cfg, region, ids))
            print(f"  {len(current)} tétel (korábban elérhető: {prev})")
            since.setdefault(f"{region}:{svc}", TODAY)   # első figyelés napja

            # Biztonsági fék: ha a friss lista még mindig gyanúsan kicsi,
            # NEM jelölünk semmit eltűntnek.
            safe = len(current) >= 0.5 * prev
            if not safe:
                print("  FIGYELEM: túl kevés találat, a kivezetés kimarad.")

            # a) ami a friss listán van: felvesszük vagy frissítjük
            for mid, m in current.items():
                e = items.setdefault(mid, {            # setdefault: ha nincs, létrehozza
                    "id": int(mid),
                    "title": m.get(cfg["title"]),      # német cím
                    "orig": m.get(cfg["orig"]),        # eredeti cím
                    "year": (m.get(cfg["released"]) or "")[:4] or None,  # "1999-10-15" -> "1999"
                    "poster": m.get("poster_path"),    # csak az útvonal, a képet a böngésző tölti
                    "added": TODAY,                    # mikor került a katalógusba
                    "avail": {},                       # ország -> szolgáltató -> elérhetőség
                    "r": None,                         # értékelések, egyelőre üres
                })
                e["title"] = m.get(cfg["title"]) or e["title"]
                e["poster"] = m.get("poster_path") or e.get("poster")
                e["genres"] = m.get("genre_ids") or e.get("genres") or []   # műfaj-azonosítók
                e["lang"] = m.get("original_language") or e.get("lang")     # eredeti nyelv
                if kind == "tv":
                    # a sorozatoknál a TMDB saját pontszámát is eltesszük (ingyen jön)
                    e["tmdb"] = m.get("vote_average")
                    e["tmdb_n"] = m.get("vote_count")
                a = e["avail"].setdefault(region, {}).setdefault(svc, {"first": TODAY})
                a.update(on=True, last=TODAY)          # on = most elérhető
                a.pop("gone", None)                    # ha visszakerült, töröljük a "gone" dátumot

            # b) ami eltűnt a listáról: nem töröljük, csak megjelöljük
            if safe:
                for mid, e in items.items():
                    a = e["avail"].get(region, {}).get(svc)
                    if a and a.get("on") and mid not in current:
                        a["on"] = False
                        a["gone"] = TODAY
    db["services"] = {**db.get("services", {}), **services_info}


# --- 2. LÉPÉS: részletek -----------------------------------------------------

def update_details(kind, cfg, db):
    items = db[cfg["key"]]
    if kind == "movie":
        # filmeknél elég egyszer lekérni
        todo = [e for e in items.values()
                if "imdb_id" not in e or "title_hu" not in e
                or "runtime" not in e or "country" not in e]
    else:
        # sorozatoknál: új tétel, VAGY futó sorozat, aminek az adatlapja 7 napnál régebbi
        # (így megjönnek az új évadok is)
        cutoff = (date.today() - timedelta(days=TV_DETAIL_MAX_AGE_DAYS)).isoformat()
        todo = [e for e in items.values()
                if not e.get("d_date") or (e.get("status") == "run" and e["d_date"] < cutoff)]
    print(f"{cfg['label']}: részletek lekérése {len(todo)} tételhez …")
    for e in todo:
        try:
            # append_to_response: két lekérés egyben (adatlap + külső azonosítók)
            d = tmdb(f"/{cfg['tmdb']}/{e['id']}", language="hu-HU", append_to_response="external_ids")
            e["title_hu"] = d.get(cfg["title"]) or e.get("orig")
            pc = d.get("production_countries") or []
            # fő ország: elsőként az origin_country, ha nincs, az első gyártó ország
            e["country"] = (d.get("origin_country") or [None])[0] or (pc[0]["iso_3166_1"] if pc else None)
            e["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
            if kind == "movie":
                e["runtime"] = d.get("runtime") or None           # játékidő percben
            else:
                # állapot: "run" = még készül/fut, "end" = befejeződött vagy törölték
                e["status"] = "end" if d.get("status") in ("Ended", "Canceled") else "run"
                e["end"] = (d.get("last_air_date") or "")[:4] or None   # utolsó adás éve
                e["n_s"] = d.get("number_of_seasons")                  # évadok száma
                e["n_e"] = d.get("number_of_episodes")                 # részek száma
                # évadok: szám, részek, megjelenés, TMDB-pont (a 0. "évad" = különkiadások, kihagyjuk)
                e["seasons"] = [
                    {"n": s["season_number"], "ep": s.get("episode_count"),
                     "date": s.get("air_date"), "vote": s.get("vote_average") or None}
                    for s in d.get("seasons") or [] if s.get("season_number", 0) > 0]
                e["d_date"] = TODAY
        except Exception as ex:
            print(f"  {e['id']}: {ex}")   # egy hibás tétel nem állítja meg a futást
        time.sleep(0.03)


# --- 3. LÉPÉS: értékelések ---------------------------------------------------

def rating_todo(db, key):
    items = db[key]
    cutoff = (datetime.now() - timedelta(days=RATING_MAX_AGE_DAYS)).date().isoformat()
    # bárhol elérhető? (bármelyik országban, bármelyik szolgáltatónál)
    is_on = lambda e: any(s.get("on") for r in e["avail"].values() for s in r.values())
    # 1) akinek még egyáltalán nincs értékelése
    todo = [e for e in items.values() if e.get("imdb_id") and not e.get("r_date")]
    # 2) akinek régi (45 napnál idősebb), és most is elérhető – a legrégebbi előre
    todo += sorted([e for e in items.values()
                    if e.get("imdb_id") and e.get("r_date")
                    and e["r_date"] < cutoff and is_on(e)],
                   key=lambda e: e["r_date"])
    return todo


def update_ratings(dbs):
    if not OMDB_KEY:
        print("FIGYELEM: nincs OMDB_API_KEY – az értékelések kimaradnak. "
              "Ellenőrizd: Settings → Secrets and variables → Actions.")
        return
    # a napi keret közös: előbb a filmek, utána a sorozatok
    todo = []
    for kind, cfg in KINDS.items():
        part = rating_todo(dbs[kind], cfg["key"])
        print(f"{cfg['label']}: hiányzó/régi értékelés: {len(part)}")
        todo += part
    print(f"Összesen {len(todo)}, ma legfeljebb {OMDB_LIMIT}")
    for e in todo[:OMDB_LIMIT]:            # [:950] = csak az első 950 elem
        try:
            e["r"] = omdb(e["imdb_id"])
            e["r_date"] = TODAY            # mikor kértük le -> később ez alapján frissül
        except OmdbLimit as ex:
            print(f"  {ex}"); break        # limit elérve: kilépünk a ciklusból
        except Exception as ex:
            print(f"  {e['imdb_id']}: {ex}")
        time.sleep(0.05)


# --- 4. LÉPÉS: műfajnevek -----------------------------------------------------

def update_genres(cfg, db):
    """Műfajnevek németül és magyarul (filmeknél és sorozatoknál eltérnek)."""
    try:
        names = {}
        for lang, code in (("de", "de-DE"), ("hu", "hu-HU")):
            for g in tmdb(f"/genre/{cfg['tmdb']}/list", language=code).get("genres", []):
                names.setdefault(str(g["id"]), {})[lang] = g["name"]
        db["genres"] = names
    except Exception as ex:
        print(f"Műfajok ({cfg['label']}): {ex}")


# --- főprogram --------------------------------------------------------------

def main():
    os.makedirs("data", exist_ok=True)     # a data mappa létrehozása, ha nincs
    dbs = {kind: load_db(cfg) for kind, cfg in KINDS.items()}
    for kind, cfg in KINDS.items():
        print(f"===== {cfg['label']} =====")
        update_availability(kind, cfg, dbs[kind])
        update_details(kind, cfg, dbs[kind])
        update_genres(cfg, dbs[kind])
    print("===== Értékelések =====")
    update_ratings(dbs)
    for kind, cfg in KINDS.items():
        save_db(cfg, dbs[kind])


# Ez a sor csak akkor indítja a main()-t, ha a fájlt közvetlenül futtatjuk.
if __name__ == "__main__":
    sys.exit(main())
