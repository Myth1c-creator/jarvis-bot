"""Jarvis – čtení a úpravy úkolů v Notionu (data source Tasks).

Token: NOTION_TOKEN v prostředí; na Windows ho najde i v uživatelských proměnných.
Token se nikdy nevypisuje.

  python tools/notion.py seznam        -> ranní přehled (dnes, zítra, týden, propadlé)
  python tools/notion.py dnes          -> jen dnešní + propadlé
  python tools/notion.py vse           -> všechny nehotové úkoly s termínem
  python tools/notion.py najdi "text"  -> najde úkol podle názvu
"""
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CASOVA_ZONA = ZoneInfo("Europe/Prague")
DATA_SOURCE = "0de633b3-5101-82f0-ac23-07b53add12e9"
NOTION_URL = "https://app.notion.com/p/d40633b351018266adbf01b8b046e850"
NOTION_VERSION = "2026-03-11"
API_QUERY = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE}/query"
API_PAGES = "https://api.notion.com/v1/pages"

DNY = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")
DNY_KRATCE = ("po", "út", "st", "čt", "pá", "so", "ne")

_CACHE: dict = {}
_CACHE_TTL = 20


class NotionError(RuntimeError):
    """Chyba, kterou má CLI vypsat jako větu – ne jako traceback."""


def find_token() -> str | None:
    t = os.environ.get("NOTION_TOKEN")
    if not t and os.name == "nt":
        try:
            import winreg
            t, _ = winreg.QueryValueEx(
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment"), "NOTION_TOKEN")
        except OSError:
            t = None
    return t.strip() if t else None


def _headers() -> dict:
    token = find_token()
    if not token:
        raise NotionError("chybí NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _termin(iso: str | None) -> tuple[datetime | None, bool]:
    """Vrátí (čas v Praze, celý den)."""
    if not iso:
        return None, False
    cely_den = len(iso) <= 10
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CASOVA_ZONA)
    return dt.astimezone(CASOVA_ZONA), cely_den


def nacti_ukoly(od: date | None = None) -> list[dict]:
    """Nehotové a neselhané úkoly; volitelně jen ty s termínem od daného dne.

    Krátká cache (20 s) zrychlí opakované příkazy v botovi a šetří API.
    """
    ted = time.monotonic()
    z = _CACHE.get(od)
    if z and ted - z[0] < _CACHE_TTL:
        return [dict(u) for u in z[1]]
    ukoly = _nacti_ukoly_z_api(od)
    _CACHE[od] = (ted, ukoly)
    return [dict(u) for u in ukoly]


def zapomen_cache() -> None:
    _CACHE.clear()


def _nacti_ukoly_z_api(od: date | None = None) -> list[dict]:
    headers = _headers()
    filtry = [
        {"property": "Complete", "checkbox": {"equals": False}},
        {"property": "Failed", "checkbox": {"equals": False}},
    ]
    if od is not None:
        filtry.append({"property": "Due Date", "date": {"on_or_after": od.isoformat()}})
    ukoly: list[dict] = []
    cursor = None
    for _ in range(10):  # pojistka, ať se nezacyklíme
        body = {"filter": {"and": filtry},
                "sorts": [{"property": "Due Date", "direction": "ascending"}],
                "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            API_QUERY, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise NotionError(f"Notion HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise NotionError(f"síťová chyba: {e.reason}") from e
        for page in data.get("results", []):
            props = page["properties"]
            nazev = "".join(t["plain_text"] for t in props["Tasks"]["title"]).strip()
            iso = (props.get("Due Date", {}).get("date") or {}).get("start")
            termin, cely_den = _termin(iso)
            if not nazev or not termin:
                continue
            ukoly.append({
                "id": page["id"],
                "nazev": nazev,
                "termin": termin,
                "cely_den": cely_den,
                "obtiznost": (props.get("Difficulty", {}).get("select") or {}).get("name", ""),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return ukoly


def _patch_page(page_id: str, properties: dict) -> dict:
    req = urllib.request.Request(
        f"{API_PAGES}/{page_id}",
        data=json.dumps({"properties": properties}).encode(),
        headers=_headers(), method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise NotionError(f"Notion HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise NotionError(f"síťová chyba: {e.reason}") from e


def oznac_hotovo(page_id: str) -> None:
    _patch_page(page_id, {"Complete": {"checkbox": True}})
    zapomen_cache()


def oznac_failed(page_id: str) -> None:
    _patch_page(page_id, {"Failed": {"checkbox": True}})
    zapomen_cache()


def _normalizuj(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def najdi_ukol(ukoly: list[dict], dotaz: str) -> list[dict]:
    """Přesná shoda, jinak všechny úkoly obsahující dotaz (bez diakritiky)."""
    q = _normalizuj(dotaz)
    presne = [u for u in ukoly if _normalizuj(u["nazev"]) == q]
    if presne:
        return presne
    return [u for u in ukoly if q in _normalizuj(u["nazev"])]


def _cas(u: dict) -> str:
    return "celý den" if u["cely_den"] else u["termin"].strftime("%H:%M")


def _radek(u: dict, s_datem: bool = False) -> str:
    if s_datem:
        return (f"• {DNY_KRATCE[u['termin'].weekday()]} {u['termin'].day}. "
                f"{u['termin'].month}. {_cas(u)} – {u['nazev']}")
    return f"• {_cas(u)} – {u['nazev']}"


def sestav_prehled(ukoly: list[dict], dnes: date | None = None,
                   zitra: bool = True, tyden: bool = True) -> str:
    """Ranní zpráva: dnes + zítra + tento týden + propadlé za 7 dní."""
    dnes = dnes or datetime.now(CASOVA_ZONA).date()
    dnesni = sorted((u for u in ukoly if u["termin"].date() == dnes),
                    key=lambda u: u["termin"])
    radky = [f"Dobré ráno, Ondro. Dnes je {DNY[dnes.weekday()]} {dnes.day}. {dnes.month}.", ""]

    if dnesni:
        radky.append(f"Dnes ({len(dnesni)}):")
        radky += [_radek(u) for u in dnesni[:12]]
        if len(dnesni) > 12:
            radky.append(f"• … a dalších {len(dnesni) - 12}")
    else:
        radky.append("Dnes nic naplánováno.")

    if zitra:
        zitrejsi = sorted((u for u in ukoly if u["termin"].date() == dnes + timedelta(days=1)),
                          key=lambda u: u["termin"])
        if zitrejsi:
            radky += ["", f"Zítra ({len(zitrejsi)}):"]
            radky += [_radek(u) for u in zitrejsi[:8]]
            if len(zitrejsi) > 8:
                radky.append(f"• … a dalších {len(zitrejsi) - 8}")

    if tyden:
        do = dnes + timedelta(days=6 - dnes.weekday())  # do neděle
        tydenni = sorted((u for u in ukoly if dnes + timedelta(days=1) < u["termin"].date() <= do),
                         key=lambda u: u["termin"])
        if tydenni:
            radky += ["", f"Tento týden ({len(tydenni)}):"]
            radky += [_radek(u, s_datem=True) for u in tydenni[:8]]
            if len(tydenni) > 8:
                radky.append(f"• … a dalších {len(tydenni) - 8}")

    radky += ["", f"Všechny úkoly: {NOTION_URL}"]
    return "\n".join(radky)


def sestav_rozsah(ukoly: list[dict], od: date, do: date, nadpis: str) -> str:
    """Jednoduchý seznam úkolů v rozsahu dnů (pro bota)."""
    vyber = sorted((u for u in ukoly if od <= u["termin"].date() <= do),
                   key=lambda u: u["termin"])
    if not vyber:
        return f"{nadpis}: nic naplánováno."
    radky = [f"{nadpis} ({len(vyber)}):"]
    radky += [_radek(u, s_datem=True) for u in vyber[:15]]
    if len(vyber) > 15:
        radky.append(f"• … a dalších {len(vyber) - 15}")
    return "\n".join(radky)


def main() -> None:
    prikaz = sys.argv[1] if len(sys.argv) > 1 else "seznam"
    try:
        if prikaz == "najdi" and len(sys.argv) > 2:
            ukoly = nacti_ukoly()
            nalezeno = najdi_ukol(ukoly, " ".join(sys.argv[2:]))
            if not nalezeno:
                print("Nic nenalezeno.")
            for u in nalezeno:
                print(f"{u['termin']:%Y-%m-%d %H:%M} | {u['nazev']}")
            return
        ukoly = nacti_ukoly()
    except NotionError as e:
        sys.exit(f"Notion: {e}")
    if prikaz == "vse":
        for u in ukoly:
            print(f"{u['termin']:%Y-%m-%d %H:%M} | {u['nazev']}")
    elif prikaz == "dnes":
        print(sestav_prehled(ukoly, zitra=False, tyden=False))
    elif prikaz == "seznam":
        print(sestav_prehled(ukoly))
    else:
        sys.exit("Použití: python tools/notion.py [seznam|dnes|vse|najdi text]")


if __name__ == "__main__":
    main()
