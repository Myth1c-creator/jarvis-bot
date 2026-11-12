"""Jarvis – cloudový Telegram bot (běží na GitHub Actions, PC může být vypnutý).

Zpracuje zprávy, které přišly, odpoví a skončí. Spouští ho workflow
`.github/workflows/bot.yml` každých 10 minut, celý den a celý týden. Když už
poslouchá lokální bot (HTTP 409), běh hned skončí a zkusí to příští cron.

  python3 posli_bot.py --sekundy 280     # poslouchej a odpovídej
  python3 posli_bot.py --dry-run         # jen vypiš, co by přišlo

Příkazy: briefing, dnes, zítra, týden, kolik mám úkolů, hotovo <název>, pomoc.
Fotka/screenshot s úkoly: přečte ji Claude vision, „zapiš“ je založí v Notionu.
Volný chat (Claude) umí oba boti; připomínky jen lokální bot na zapnutém PC.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notion  # noqa: E402

CASOVA_ZONA = ZoneInfo("Europe/Prague")
API = "https://api.telegram.org/bot{token}/{method}"
POMOC = (
    "Cloud režim (PC může být vypnutý):\n"
    "• napiš seznam úkolů (klidně po řádcích) – připravím je a „zapiš“ je založí\n"
    "• bez času dám před 18:00 dnešní 18:00, jinak zítra 9:00\n"
    "• briefing / dnes / zítra / týden\n"
    "• kolik mám úkolů\n"
    "• hotovo <název úkolu>\n"
    "• nový úkol zavolat DIS zítra 15:00\n"
    "• pošli fotku/screenshot s úkoly – přečtu je a „zapiš“ je založí\n"
    "• nebo napiš název úkolu – najdu ho\n\n"
    "A na cokoliv jiného ti odpovím jako normální chat.\n"
    "Připomínky 45 min před termínem umí jen bot na zapnutém PC."
)


def token() -> str:
    t = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("JARVIS_TELEGRAM_TOKEN")
    if not t:
        sys.exit("chybí TELEGRAM_TOKEN")
    return t.strip()


def chat_id() -> str:
    return (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


class KonfliktPoslechu(RuntimeError):
    """Jiný bot právě poslouchá getUpdates."""


def tg(method: str, data: dict | None = None, timeout: int = 60) -> dict:
    url = API.format(token=token(), method=method)
    req = urllib.request.Request(
        url, data=json.dumps(data or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if method == "getUpdates" and e.code == 409:
            raise KonfliktPoslechu("lokální bot poslouchá – končím") from None
        telo = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"Telegram HTTP {e.code}: {telo}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram: {e.reason}") from e


FALLBACK = "Nerozuměl jsem – napiš „pomoc“ a vypíšu, co umím."


def neprázdná(text: str) -> str:
    return text if text and text.strip() else FALLBACK


def posli(text: str) -> None:
    text = _cisty(neprázdná(text))
    for cast in [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]:
        tg("sendMessage", {"chat_id": chat_id(), "text": cast,
                           "disable_web_page_preview": True})


def _cisty(text: str) -> str:
    """Odstraní markdown, který Telegram v prostém textu zobrazuje jako znaky."""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    return t.replace("`", "")


def pisni() -> None:
    try:
        tg("sendChatAction", {"chat_id": chat_id(), "action": "typing"}, timeout=10)
    except RuntimeError:
        pass


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _ukoly():
    try:
        return notion.nacti_ukoly()
    except notion.NotionError as e:
        return e


def _kdy(text: str):
    """Miniparser času pro cloud: +2h, +30m, dnes 18:00, zítra 9:00, 2026-11-12 08:30."""
    now = datetime.now(CASOVA_ZONA).replace(second=0, microsecond=0)
    t = " ".join(text.strip().lower().split())
    m = re.fullmatch(r"\+(\d+)\s*(m|min\w*|h|hod\w*|d|den\w*|w|tyden\w*|týden\w*)", t)
    if m:
        n = int(m.group(1))
        j = m.group(2)
        if j.startswith(("m", "min")):
            return now + timedelta(minutes=n)
        if j.startswith(("h", "hod")):
            return now + timedelta(hours=n)
        if j.startswith(("w", "tyden", "týden")):
            return now + timedelta(weeks=n)
        return now + timedelta(days=n)
    m = re.fullmatch(r"(?:(dnes|zitra|zítra|pozitri|pozítří)\s+)?(?:v\s+)?(\d{1,2})[:.](\d{2})", t)
    if m:
        posun = {"zitra": 1, "zítra": 1, "pozitri": 2, "pozítří": 2}.get(m.group(1) or "", 0)
        hod, minuta = int(m.group(2)), int(m.group(3))
        if hod > 23 or minuta > 59:
            raise ValueError("neplatný čas")
        cas = now.replace(hour=hod, minute=minuta)
        if not m.group(1) and cas <= now:
            posun = 1
        return cas + timedelta(days=posun)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            pass
    raise ValueError("nerozumím času")


def novy_ukol(text: str) -> str:
    m = re.match(r"^(?:nov(?:ý|y|ej)?\s+úkol|nov(?:ý|y|ej)?\s+ukol|"
                 r"přidej(?:\s+úkol)?|pridej(?:\s+ukol)?|úkol|ukol)\s*[:\-]?\s*",
                 text, re.IGNORECASE)
    zbytek = text[m.end():].strip(" ,-") if m else text.strip()
    if not zbytek:
        return "Napiš úkol a čas. Třeba: nový úkol zavolat DIS zítra 15:00."
    slova = zbytek.split()
    for n in (3, 2, 1):
        if len(slova) <= n:
            continue
        try:
            termin = _kdy(" ".join(slova[-n:]))
        except ValueError:
            continue
        nazev = " ".join(slova[:-n]).strip(" ,-")
        if not nazev:
            break
        try:
            notion.vytvor_ukol(nazev, termin)
        except notion.NotionError as e:
            return f"Notion: {e}"
        return f"Přidáno do Notionu: {nazev} – {termin:%d.%m. %H:%M}."
    return "Nerozumím času. Třeba: nový úkol zavolat DIS zítra 15:00 (nebo +2h)."


# ---------- dávka úkolů (business režim) ----------

# Hlavičky typu „Dnes:“ nebo „Tasky:“ nejsou úkol.
_NADPIS_RE = re.compile(
    r"^(?:dnes|dneska|zitra|pozitri|tasky|ukoly|plan|seznam|moje tasky|muj plan|"
    r"poznamky|na dnes|na zitra)\s*:?\s*$", re.IGNORECASE)
# „musím“, „ještě“, „tak“ na začátku věty do názvu úkolu nepatří.
_PLNICI_RE = re.compile(
    r"^(?:a|tak|takže|no|hele|ještě|jeste|musím|musim|potřebuju|potrebuju|"
    r"potřebuji|potrebuji|mám|mam)\s+", re.IGNORECASE)

# Příkazy, které se nesmí splést s výčtem úkolů.
_NEUKOL = ("novy ukol", "novej ukol", "pridej", "ukol ", "novy task", "pripomen",
           "pripominej", "hotovo", "splneno", "stav", "pomoc", "briefing",
           "pripominky", "zapis", "uloz")
# Otázky, na které se neodpovídá jako na úkol.
_DOTAZ_SLOVA = ("ukol", "ukoly", "task", "tasky", "briefing", "prehled", "pripominky",
                "pomoc", "hotovo", "odloz", "snooze", "zapis", "kolik", "kdy", "kde",
                "kdo", "jaky", "jake", "jak", "proc")
_DOTAZ_ZACATKY = ("co", "kolik", "kdy", "kde", "kdo", "jaky", "jake", "jak", "proc",
                  "mam", "mame", "mas", "vypis", "ukaz", "rekni", "briefing",
                  "pripomen", "pripominej")

POTVRZENI = ("ano", "jo", "jj", "ok", "oki", "okey", "jasne", "sure", "yes", "yep",
             "zapis", "zapis to", "zapis vse", "uloz", "uloz to", "muzes",
             "muzes to pridat", "muzes to tam dat", "pridej", "pridej to",
             "dej to tam", "jdi do toho", "urcite")
ZRUSENI = ("ne", "nech to", "nic", "to je vse", "to je vsechno", "jeste ne",
           "zrus", "zrus to", "zapomen to", "nemusis", "nedelej to")


def _radky_ukolu(text: str) -> list:
    """Rozdělí zprávu na jednotlivé úkoly – řádky, odrážky, středníky, číslování."""
    kusy = []
    for radek in re.split(r"[\r\n]+", text or ""):
        for cast in re.split(r"\s*[;•]\s*", radek):
            c = re.sub(r"^\s*(?:\d+[.)]|[-*–])\s*", "", cast).strip()
            if len(c) >= 3:
                kusy.append(c)
    return kusy


def _vety(text: str) -> list:
    return [v.strip() for v in re.split(r"(?<=[.!?])\s+", text or "") if len(v.strip()) >= 3]


def _casti_ukolu(text: str) -> list:
    return _radky_ukolu(text) if "\n" in (text or "") else _vety(text)


def _cas_na_konci(cast: str) -> tuple:
    """(název, termin) – čas bere z konce části, stejně jako novy_ukol."""
    slova = cast.split()
    for n in (3, 2, 1):
        if len(slova) <= n:
            continue
        try:
            termin = _kdy(" ".join(slova[-n:]))
        except ValueError:
            continue
        nazev = " ".join(slova[:-n]).strip(" ,-")
        if nazev:
            return nazev, termin
    return cast.strip(" ,-"), None


def _pocet_casu(text: str) -> int:
    return sum(1 for cast in _casti_ukolu(text) if _cas_na_konci(cast)[1] is not None)


def _vychozi_termin() -> datetime:
    """Úkol bez času: před 18:00 dnešní 18:00, jinak zítra 9:00."""
    ted = datetime.now(CASOVA_ZONA)
    if ted.hour < 18:
        return ted.replace(hour=18, minute=0, second=0, microsecond=0)
    return (ted + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)


def _bez_interpunkce(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", " ".join(_norm(text).split())).strip()


def _je_potvrzeni(text: str) -> bool:
    t = _bez_interpunkce(text)
    if t in POTVRZENI:
        return True
    slova = t.split()
    if not slova or slova[0] not in ("ano", "jo", "jj", "ok", "jasne", "urcite", "yes", "yep"):
        return False
    return any(s in slova for s in ("zapis", "uloz", "pridej", "dej", "muzes", "to"))


def _je_zruseni(text: str) -> bool:
    return _bez_interpunkce(text) in ZRUSENI


def _je_dotaz(text: str) -> bool:
    t = " ".join(_norm(text).split())
    if not t:
        return False
    if t.endswith("?"):
        return True
    slova = re.findall(r"\w+", t)
    if slova and slova[0] in _DOTAZ_ZACATKY:
        return True
    return any(s in slova for s in _DOTAZ_SLOVA)


def _je_davka(text: str) -> bool:
    """Vypadá to jako výčet úkolů? (víc řádků/vět s časy, nebo klíčové slovo)"""
    t = " ".join(_norm(text).split())
    if t.startswith(("tasky", "muj plan", "plan na dnes", "dnes mam",
                     "seznam ukolu", "tyhle tasky", "moje tasky")):
        return True
    casti = _casti_ukolu(text)
    if not casti:
        return False
    if _NADPIS_RE.match(casti[0].strip()):
        return len(casti) >= 2
    if " ".join(_norm(casti[0]).split()).startswith(_NEUKOL) or _je_dotaz(text):
        return False
    if len(casti) == 1:
        return _cas_na_konci(casti[0])[1] is not None
    if _pocet_casu(text) >= 2:
        return True
    if len(casti) >= 3:
        return True
    return all(len(c.split()) <= 6 for c in casti)


def prehled_davky() -> str:
    """Co je připravené k zápisu + otázka, jestli přidat a jestli je ještě něco."""
    if not CEKA_NA_ZAPIS:
        return "Zatím nic k zápisu – napiš úkoly po řádcích."
    radky = []
    for i, (nazev, termin) in enumerate(CEKA_NA_ZAPIS, 1):
        try:
            kdy = f"{datetime.fromisoformat(str(termin)):%d.%m. %H:%M}"
        except ValueError:
            kdy = "bez času"
        radky.append(f"{i}. {nazev} – {kdy}")
    return ("Mám to takhle:\n" + "\n".join(radky) +
            "\n\nPřidat do Notionu? Napiš „zapiš“."
            "\nJeště něco?")


def davka(text: str) -> str:
    """Výčet úkolů → připraví je k zápisu; chybějící čas doplní rozumným výchozím."""
    casti = _casti_ukolu(text)
    if not casti:
        return ("Napiš úkoly po řádcích, třeba:\n"
                "• obepsat 5 lidí DIS dnes 18:00\n• zavolat Tomášovi zítra 9:00")
    nove = []
    for cast in casti:
        if _NADPIS_RE.match(cast.strip()):
            continue
        nazev, termin = _cas_na_konci(cast)
        if termin is None:
            termin = _vychozi_termin()
        nazev = _PLNICI_RE.sub("", nazev).strip(" ,.-–!?")
        if len(nazev) < 3:
            continue
        polozka = (nazev[:200], termin.isoformat(timespec="minutes"))
        if polozka not in CEKA_NA_ZAPIS:
            nove.append(polozka)
    if not nove:
        if CEKA_NA_ZAPIS:
            return prehled_davky()
        return "Nenašel jsem v tom žádný úkol. Napiš je po řádcích."
    CEKA_NA_ZAPIS.extend(nove)
    return prehled_davky()


HISTORIE: list = []
SYSTEM = (
    "Jsi Jarvis, Ondrův pracovní asistent v Telegramu (běžíš v cloudu, když je PC vypnuté). "
    "Řešíš hlavně úkoly a termíny – odpovídej česky, stručně (max 8 řádků), věcně.\n"
    "• Prioritu mají úkoly, termíny a další krok.\n"
    "• Neptej se na detaily, které si umíš domyslet (čas úkolu, pořadí). Chybějící čas "
    "doplň: před 18:00 dnešní 18:00, jinak zítra 9:00.\n"
    "• Když Ondra vypíše víc úkolů, ber to jako seznam k založení – shrň je a čekej na „zapiš“.\n"
    "• K úkolům a termínům vycházej z přehledu níže – nic si nevymýšlej.\n"
    "• Když něco nevíš nebo ti chybí údaj, řekni to jednou větou a rovnou se zeptej.\n"
    "• Nepiš „Samozřejmě“, „Rád pomohu“ a neopakuj jeho otázku.\n"
    "• Nepoužívej markdown (** , #, `) – Telegram ho zobrazí jako znaky.\n"
    "• Víc položek vypisuj jako • odrážky, ne do odstavce.\n"
    "Umíš: přehled úkolů (dnes/zítra/týden), založit úkol nebo celý seznam, "
    "„hotovo <název>“, čtení úkolů ze screenshotu. Připomínky a hlasovky umí jen "
    "lokální bot na PC."
)

_DNY = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")


def ted_text() -> str:
    """Aktuální čas pro model – bez něj plete dny."""
    ted = datetime.now(CASOVA_ZONA)
    return f"Teď je {_DNY[ted.weekday()]} {ted:%d.%m.%Y %H:%M} (Praha)."



def kontext() -> str:
    u = _ukoly()
    if not isinstance(u, list):
        return ""
    dnes = datetime.now(CASOVA_ZONA).date()

    def vypis(vyber):
        return "; ".join(f"{x['termin']:%H:%M} {x['nazev']}" for x in vyber[:8])

    ted = datetime.now(CASOVA_ZONA)
    d = sorted((x for x in u if x["termin"].date() == dnes and x["termin"] >= ted),
               key=lambda x: x["termin"])
    z = sorted((x for x in u if x["termin"].date() == dnes + timedelta(days=1)),
               key=lambda x: x["termin"])
    casti = []
    if d:
        casti.append("Dnes: " + vypis(d))
    if z:
        casti.append("Zítra: " + vypis(z))
    return ("Ondrovy nehotové úkoly z Notionu – " + ". ".join(casti) + ".") if casti else ""


def stahni(file_id: str, limit_mb: int = 15) -> bytes:
    """Stáhne soubor z Telegramu (fotka, screenshot)."""
    info = tg("getFile", {"file_id": file_id})
    cesta = (info.get("result") or {}).get("file_path")
    if not cesta:
        raise RuntimeError("getFile: chybí file_path")
    url = f"https://api.telegram.org/file/bot{token()}/{cesta}"
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        raise RuntimeError(f"soubor je větší než {limit_mb} MB")
    return data


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _pocet(n: int, jeden: str, malo: str, hodne: str) -> str:
    if n == 1:
        return f"{n} {jeden}"
    if 2 <= n <= 4:
        return f"{n} {malo}"
    return f"{n} {hodne}"


def vision(data: bytes, mime: str, dotaz: str) -> str:
    """Přečte obrázek přes Claude (Fable 5.2); prázdné = nevyšlo."""
    klic = (os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not klic:
        return ""
    telo = json.dumps({
        "model": "claude-fable-5-2", "max_tokens": 1500,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime,
                                         "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": dotaz}]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=telo,
        headers={"x-api-key": klic, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            odp = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"vision: {e}", file=sys.stderr, flush=True)
        return ""
    casti = [c.get("text", "") for c in odp.get("content", []) if c.get("type") == "text"]
    return "\n".join(casti).strip()


FOTO_DOTAZ = (
    "Podívej se na obrázek a najdi na něm úkoly, termíny nebo poznámky. "
    "Vrať POUZE JSON, bez markdown bloků:\n"
    '{"ukoly": [{"nazev": "krátký název česky", "termin": "2026-11-11T15:00"}], '
    '"poznamka": "jedna věta, co na obrázku je"}\n'
    "Když termín chybí, dej termin null. Když žádné úkoly nejsou, vrať prázdný seznam."
)

CEKA_NA_ZAPIS: list = []


def foto(msg: dict) -> str:
    """Fotka/screenshot → přečte úkoly a nabídne jejich založení."""
    fota = msg.get("photo") or []
    dok = msg.get("document") or {}
    try:
        if fota:
            file_id = max(fota, key=lambda f: f.get("file_size", 0))["file_id"]
        elif str(dok.get("mime_type", "")).startswith("image/"):
            file_id = dok["file_id"]
        else:
            return ""
        data = stahni(file_id)
    except (RuntimeError, urllib.error.URLError) as e:
        return f"Obrázek se nepodařilo načíst: {e}"
    pisni()
    odpoved = vision(data, _mime(data), FOTO_DOTAZ)
    if not odpoved:
        return "Model obrázek nepřečetl. Zkus to znovu."
    cisty = odpoved.strip().strip("`")
    if cisty.lower().startswith("json"):
        cisty = cisty[4:].strip()
    try:
        vysledek = json.loads(cisty[cisty.index("{"):cisty.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return odpoved[:1500]
    ukoly = vysledek.get("ukoly") or []
    poznamka = (vysledek.get("poznamka") or "").strip()
    if not ukoly:
        return f"Na obrázku nevidím žádné úkoly. {poznamka}".strip()
    CEKA_NA_ZAPIS.clear()
    radky = [f"Na obrázku vidím {_pocet(len(ukoly), 'úkol', 'úkoly', 'úkolů')}:"]
    for u in ukoly[:12]:
        nazev = (u.get("nazev") or "").strip()
        termin = (u.get("termin") or "").strip()
        ukaz = "bez termínu"
        if termin:
            try:
                ukaz = f"{datetime.fromisoformat(termin.replace('Z', '')):%d.%m. %H:%M}"
            except ValueError:
                ukaz = termin
        radky.append(f"• {nazev} – {ukaz}")
        CEKA_NA_ZAPIS.append((nazev, termin))
    radky.append("")
    radky.append("Napiš „zapiš“ a založím je do Notionu.")
    if poznamka:
        radky.append(f"({poznamka})")
    return "\n".join(radky)[:3500]


def zapis_z_fotky() -> str:
    """Založí připravené úkoly (z fotky, z výčtu) a zeptá se, jestli je ještě něco."""
    if not CEKA_NA_ZAPIS:
        return "Nemám co zapsat. Napiš úkoly (klidně po řádcích) nebo pošli fotku se seznamem."
    hotovo, chyby = [], 0
    for nazev, termin in CEKA_NA_ZAPIS:
        if not nazev or not termin:
            chyby += 1
            continue
        try:
            cas = datetime.fromisoformat(str(termin).replace("Z", ""))
        except ValueError:
            chyby += 1
            continue
        try:
            notion.vytvor_ukol(nazev, cas)
            hotovo.append(f"• {nazev} – {cas:%d.%m. %H:%M}")
        except notion.NotionError as e:
            print(f"notion: {e}", file=sys.stderr, flush=True)
            chyby += 1
    CEKA_NA_ZAPIS.clear()
    if not hotovo:
        return ("Nic jsem nezaložil – u úkolů chyběl čitelný termín. "
                "Napiš je znovu, klidně i bez času.")
    text = f"Založeno {_pocet(len(hotovo), 'úkol', 'úkoly', 'úkolů')}:\n" + "\n".join(hotovo)
    if chyby:
        text += f"\n{chyby} jsem přeskočil (chyběl termín nebo Notion zlobil)."
    return text + "\n\nJeště něco?"


def model(text: str) -> str:
    """Volný chat přes Claude API (funguje i při vypnutém PC)."""
    klic = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not klic:
        return "Volný chat teď nemám (chybí klíč). Napiš „pomoc“."
    historie = HISTORIE[-6:] + [{"role": "user", "content": text[:4000]}]
    system = SYSTEM + "\n" + ted_text()
    k = kontext()
    if k:
        system += "\n" + k
    telo = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 600,
                       "system": system, "messages": historie}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=telo,
        headers={"x-api-key": klic.strip(), "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"}, method="POST")
    data = None
    for pokus in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            print(f"Claude: HTTP {e.code}", file=sys.stderr, flush=True)
            if e.code in (429, 500, 502, 503, 504) and pokus == 1:
                time.sleep(2)
                continue
            return "Model teď neodpověděl. Zkus to znovu."
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"Claude: {type(e).__name__}", file=sys.stderr, flush=True)
            if pokus == 1:
                time.sleep(1)
                continue
            return "Model teď neodpověděl. Zkus to znovu."
    if not data:
        return "Model teď neodpověděl. Zkus to znovu."
    casti = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
    odpoved = "\n".join(casti).strip()
    if not odpoved:
        return "Model nic nevrátil. Zkus to znovu."
    HISTORIE.append({"role": "user", "content": text[:4000]})
    HISTORIE.append({"role": "assistant", "content": odpoved})
    del HISTORIE[:-8]
    return odpoved[:3500]


def stav() -> str:
    """Rychlá diagnóza pro případ, že něco neodpovídá – běží v cloudu."""
    radky = ["Jsem cloudový Jarvis (GitHub Actions)."]
    try:
        d = _obsah_tepu()
        cas = datetime.fromisoformat(str(d["cas"]))
        if cas.tzinfo is None:
            cas = cas.replace(tzinfo=CASOVA_ZONA)
        vek = int((datetime.now(CASOVA_ZONA) - cas).total_seconds())
        stav_bota = "žije" if vek < TEP_STARI else "neběží"
        radky.append(f"Lokální bot: tep před {vek} s ({stav_bota}).")
    except Exception:  # noqa: BLE001
        radky.append("Lokální bot: tep se nepodařilo přečíst.")
    u = _ukoly()
    if isinstance(u, list):
        radky.append(f"Úkoly: {len(u)} nehotových.")
    else:
        radky.append(f"Notion: {u}")
    radky.append("Připomínky a hlasovky umí jen lokální bot na PC.")
    return "\n".join(radky)


def odpoved(text: str) -> str:
    t = " ".join(_norm(text).split())
    if t in ("/start", "start", "ahoj", "cau", "nazdar"):
        return "Jarvis na příjmu (cloud).\n" + POMOC
    if t in ("pomoc", "help", "/help", "co umis"):
        return POMOC
    if t in ("stav", "status", "/stav", "jak jsi na tom"):
        return stav()
    if t in ("zapis", "zapis to", "uloz", "uloz to", "zapis vse"):
        return zapis_z_fotky()
    if CEKA_NA_ZAPIS:
        # Rozdělaná dávka: potvrzení zapíše, „ne“ zahodí, cokoli dalšího přidá.
        if _je_potvrzeni(text):
            return zapis_z_fotky()
        if _je_zruseni(text):
            CEKA_NA_ZAPIS.clear()
            return "Dobře, nechal jsem to být. Kdybys chtěl, napiš úkoly znovu."
        if t in ("diky", "dekuju", "super", "parada", "fajn"):
            return "Není zač. Kdyby něco, jsem tu."
        if _je_davka(text):  # přidání dalšího úkolu k rozdělané dávce
            return davka(text)
    if not t.startswith(_NEUKOL) and _je_davka(text):
        return davka(text)
    if t.startswith(("novy ukol", "novej ukol", "pridej", "ukol ", "novy task")):
        return novy_ukol(text)
    prehled = not t.startswith(("hotovo", "splneno"))
    slova = set(re.findall(r"\w+", t))
    if "briefing" in t:
        u = _ukoly()
        return f"Notion: {u}" if not isinstance(u, list) else notion.sestav_prehled(u)
    if prehled and slova & {"dnes", "dneska", "dnesni"}:
        u = _ukoly()
        return f"Notion: {u}" if not isinstance(u, list) else \
            notion.sestav_prehled(u, zitra=False, tyden=False)
    if prehled and slova & {"zitra", "zitrejsi"}:
        u = _ukoly()
        if not isinstance(u, list):
            return f"Notion: {u}"
        d = datetime.now(CASOVA_ZONA).date()
        return notion.sestav_rozsah(u, d + timedelta(days=1), d + timedelta(days=1), "Zítra")
    if prehled and "tyden" in slova:
        u = _ukoly()
        if not isinstance(u, list):
            return f"Notion: {u}"
        d = datetime.now(CASOVA_ZONA).date()
        return notion.sestav_rozsah(u, d + timedelta(days=1), d + timedelta(days=7), "Příštích 7 dní")
    if "kolik" in t and "ukol" in t:
        u = _ukoly()
        if not isinstance(u, list):
            return f"Notion: {u}"
        d = datetime.now(CASOVA_ZONA).date()
        dnesni = [x for x in u if x["termin"].date() == d]
        return f"Nehotových úkolů: {len(u)} (dnes {len(dnesni)})."
    if t.startswith(("hotovo", "splneno")):
        dotaz = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not dotaz:
            return "Napiš, co mám označit: hotovo název úkolu."
        u = _ukoly()
        if not isinstance(u, list):
            return f"Notion: {u}"
        nalez = notion.najdi_ukol(u, dotaz)
        if not nalez:
            return f"Nic jsem nenašel pro „{dotaz}“."
        if len(nalez) > 1:
            radky = [f"Našel jsem {len(nalez)} úkolů – upřesni:"]
            radky += [f"• {x['termin']:%d.%m. %H:%M} – {x['nazev']}" for x in nalez[:5]]
            return "\n".join(radky)
        try:
            notion.oznac_hotovo(nalez[0]["id"])
        except notion.NotionError as e:
            return f"Notion: {e}"
        return f"Hotovo: {nalez[0]['nazev']}."
    if t.startswith(("novy ukol", "novej ukol", "pridej", "ukol", "novy task")):
        return novy_ukol(text)
    u = _ukoly()
    if isinstance(u, list):
        nalez = notion.najdi_ukol(u, text)
        if nalez:
            radky = [f"Našel jsem v Notionu ({len(nalez)}):"]
            radky += [f"• {x['termin']:%d.%m. %H:%M} – {x['nazev']}" for x in nalez[:5]]
            return "\n".join(radky)
    return neprázdná(model(text))


def zprava(msg: dict) -> str:
    if msg.get("date") and time.time() - msg["date"] > 900:
        return "Zpráva dorazila se zpožděním přes 15 minut – pošli ji prosím znovu."
    if any(k in msg for k in ("voice", "audio")):
        return "Hlasovku v cloudu nepřepíšu – napiš to prosím textem."
    if any(k in msg for k in ("video", "video_note", "animation")):
        return "Video v cloudu nepřečtu – pošli screenshot nebo napiš úkol."
    if "sticker" in msg:
        return "Samolepku v cloudu nepřečtu – pošli screenshot nebo napiš úkol."
    dok = msg.get("document") or {}
    if msg.get("photo") or str(dok.get("mime_type", "")).startswith("image/"):
        return neprázdná(foto(msg))
    if "document" in msg:
        return "Tento dokument v cloudu nepřečtu – pošli screenshot nebo napiš úkol."
    text = (msg.get("text") or "").strip()
    if text:
        return neprázdná(odpoved(text))
    return "Tento typ zprávy v cloudu nepřečtu – napiš úkol nebo pošli screenshot."


def zpracuj(u: dict) -> bool | None:
    """True = odpovězeno, False = selhalo odeslání, None = cizí chat."""
    msg = u.get("message") or {}
    if str((msg.get("chat") or {}).get("id", "")) != chat_id():
        return None
    try:
        odp = zprava(msg)
    except Exception as e:
        print(f"zpracovani: {type(e).__name__}", file=sys.stderr, flush=True)
        odp = "Zprávu se nepodařilo zpracovat – zkus to prosím znovu."
    try:
        posli(neprázdná(odp))
        return True
    except Exception as e:
        print(f"odeslani: {type(e).__name__}", file=sys.stderr, flush=True)
        return False


REPO = os.environ.get("JARVIS_REPO") or "Myth1c-creator/jarvis-bot"
TEP_VETEV = "heartbeat"
# Pozor: raw.githubusercontent.com je CDN a content se zpožďuje až minuty,
# proto se tep čte přes API (a když to selže, zkusí se raw jako záloha).
TEP_URL = os.environ.get("JARVIS_HEARTBEAT_URL") or \
    f"https://api.github.com/repos/{REPO}/contents/heartbeat.json?ref={TEP_VETEV}"
TEP_RAW = f"https://raw.githubusercontent.com/{REPO}/{TEP_VETEV}/heartbeat.json"
TEP_TOKEN = os.environ.get("PAT") or os.environ.get("JARVIS_PAT") or ""
TEP_STARI = 420  # sekund; lokální bot píše tep každých 300 s, rezerva 2 min


def _obsah_tepu() -> dict:
    """Přečte heartbeat.json – primárně přes API, při chybě přes raw (může být zpožděný)."""
    try:
        hlavicky = {"Accept": "application/vnd.github+json", "User-Agent": "jarvis-bot"}
        if TEP_TOKEN:
            hlavicky["Authorization"] = f"Bearer {TEP_TOKEN}"
        req = urllib.request.Request(TEP_URL, headers=hlavicky)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict) and "content" in data:
            data = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        return data
    except Exception:
        with urllib.request.urlopen(TEP_RAW, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))


def lokal_zije(limit: int = TEP_STARI) -> bool:
    """Přečte srdeční tep lokálního bota. Čerstvý tep = PC poslouchá, cloud mlčí."""
    try:
        data = _obsah_tepu()
        cas = datetime.fromisoformat(str(data["cas"]))
        if cas.tzinfo is None:
            cas = cas.replace(tzinfo=CASOVA_ZONA)
        return (datetime.now(CASOVA_ZONA) - cas).total_seconds() < limit
    except Exception:
        return False


CELY_DEN = (0, 23)


def v_okne(now: datetime, od: int, do: int) -> bool:
    # 0-23 znamená celý den včetně 23:00–23:59; jiná okna končí hodinou „do“
    return (od, do) == CELY_DEN or od <= now.hour < do


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sekundy", type=int, default=280)
    ap.add_argument("--okno", default="0-23",
                    help="hodiny Praha, kdy smí běžet (od-do), každý den; 0-23 = celý den")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diagnostika", action="store_true", help="vypíše dobu poslechu a počty zpráv")
    ap.add_argument("--zkouska", default="", help="pošli zkušební odpověď modelu a skonči")
    args = ap.parse_args()
    if not chat_id():
        print("chybí TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1
    if args.zkouska:
        posli(neprázdná(model(args.zkouska)))
        print("Zkušební odpověď odeslána.")
        return 0
    try:
        od, do = (int(x) for x in args.okno.split("-"))
        if not 0 <= od < do <= 23:
            raise ValueError()
    except ValueError:
        ap.error("okno musí být od-do, 0 <= od < do <= 23")
    now = datetime.now(CASOVA_ZONA)
    if not v_okne(now, od, do):
        print(f"Mimo okno {od}–{do} Praha ({now:%H:%M}) – končím.")
        if args.diagnostika:
            print("diagnostika: poslech 0.0 s, zpráv 0, bez odpovědi 0, konflikt: ne")
        return 0
    start = time.monotonic()
    konec = start + max(0, args.sekundy)
    konflikt = False
    minuly_tep = None
    offset = prijato = bez_odpovedi = 0
    try:
        while time.monotonic() < konec:
            now = datetime.now(CASOVA_ZONA)
            if not v_okne(now, od, do):
                break
            zbyva = konec - time.monotonic()
            if (od, do) != CELY_DEN:
                zbyva = min(zbyva,
                            (now.replace(hour=do, minute=0, second=0, microsecond=0) - now).total_seconds())
            if zbyva < 1:
                break
            if lokal_zije():
                # PC je zapnuté a lokální bot poslouchá – cloud do toho nemluví.
                if minuly_tep is None or time.monotonic() - minuly_tep > 600:
                    print(f"{datetime.now(CASOVA_ZONA):%H:%M:%S} lokální bot žije – čekám", flush=True)
                    minuly_tep = time.monotonic()
                time.sleep(min(30, max(0, zbyva)))
                continue
            try:
                data = tg("getUpdates", {"offset": offset, "timeout": min(45, int(zbyva)),
                                         "limit": 10, "allowed_updates": ["message"]},
                          timeout=min(60, max(1, zbyva)))
            except KonfliktPoslechu:
                konflikt = True
                print("lokální bot poslouchá – končím", flush=True)
                return 0
            except Exception as e:
                print(f"cyklus {now:%H:%M:%S} Praha, chyba {type(e).__name__}", flush=True)
                time.sleep(min(5, max(0, zbyva)))
                continue
            updates = data.get("result", [])
            print(f"cyklus {datetime.now(CASOVA_ZONA):%H:%M:%S} Praha, {len(updates)} novych zprav", flush=True)
            for u in updates:
                if not v_okne(datetime.now(CASOVA_ZONA), od, do):
                    break  # nepotvrdit neobsloužené zprávy
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id", "")) != chat_id():
                    continue
                prijato += 1
                if args.dry_run:
                    print(f"DRY-RUN update {u['update_id']}", flush=True)
                    bez_odpovedi += 1
                    continue
                if zpracuj(u) is not True:
                    bez_odpovedi += 1
        if offset and v_okne(datetime.now(CASOVA_ZONA), od, do):
            try:
                tg("getUpdates", {"offset": offset, "timeout": 0, "limit": 1}, timeout=1)
            except KonfliktPoslechu:
                konflikt = True
                print("lokální bot poslouchá – končím", flush=True)
            except Exception:
                pass
    finally:
        if args.diagnostika:
            print(f"diagnostika: poslech {time.monotonic() - start:.1f} s, "
                  f"zpráv {prijato}, bez odpovědi {bez_odpovedi}, "
                  f"konflikt: {'ano' if konflikt else 'ne'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
