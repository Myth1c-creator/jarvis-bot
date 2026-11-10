"""Jarvis – cloudový Telegram bot (běží na GitHub Actions, PC může být vypnutý).

Zpracuje zprávy, které přišly, odpoví a skončí. Spouští ho workflow
`.github/workflows/bot.yml` každých 5 minut (Po–Pá 4:00–16:00 Praha).

  python3 posli_bot.py --sekundy 280     # poslouchej a odpovídej
  python3 posli_bot.py --dry-run         # jen vypiš, co by přišlo

Příkazy: briefing, dnes, zítra, týden, kolik mám úkolů, hotovo <název>, pomoc.
Volný chat (OpenCode) umí jen lokální bot na zapnutém PC.
"""
import argparse
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
    "• briefing / dnes / zítra / týden\n"
    "• kolik mám úkolů\n"
    "• hotovo <název úkolu>\n"
    "• nový úkol zavolat DIS zítra 15:00\n"
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


def tg(method: str, data: dict | None = None, timeout: int = 60) -> dict:
    url = API.format(token=token(), method=method)
    req = urllib.request.Request(
        url, data=json.dumps(data or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        telo = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"Telegram HTTP {e.code}: {telo}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram: {e.reason}") from e


def posli(text: str) -> None:
    for cast in [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]:
        tg("sendMessage", {"chat_id": chat_id(), "text": cast,
                           "disable_web_page_preview": True})


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
    m = re.fullmatch(r"(?:(dnes|zitra|zítra|pozitri|pozítří)\s+)?(\d{1,2})[:.](\d{2})", t)
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


HISTORIE: list = []
SYSTEM = ("Jsi Jarvis, Ondrův osobní asistent. Odpovídej česky, stručně (max 8 řádků), "
          "prakticky a bez zbytečných frází. Když něco nevíš, řekni to.")


def kontext() -> str:
    u = _ukoly()
    if not isinstance(u, list):
        return ""
    dnes = datetime.now(CASOVA_ZONA).date()

    def vypis(vyber):
        return "; ".join(f"{x['termin']:%H:%M} {x['nazev']}" for x in vyber[:8])

    d = sorted((x for x in u if x["termin"].date() == dnes), key=lambda x: x["termin"])
    z = sorted((x for x in u if x["termin"].date() == dnes + timedelta(days=1)),
               key=lambda x: x["termin"])
    casti = []
    if d:
        casti.append("Dnes: " + vypis(d))
    if z:
        casti.append("Zítra: " + vypis(z))
    return ("Ondrovy nehotové úkoly z Notionu – " + ". ".join(casti) + ".") if casti else ""


def model(text: str) -> str:
    """Volný chat přes Claude API (funguje i při vypnutém PC)."""
    klic = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not klic:
        return "Volný chat teď nemám (chybí klíč). Napiš „pomoc“."
    historie = HISTORIE[-6:] + [{"role": "user", "content": text[:4000]}]
    system = SYSTEM
    k = kontext()
    if k:
        system += "\n" + k
    telo = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 600,
                       "system": system, "messages": historie}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=telo,
        headers={"x-api-key": klic.strip(), "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"Claude: {e}", file=sys.stderr, flush=True)
        return "Model teď neodpověděl. Zkus to znovu."
    casti = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
    odpoved = "\n".join(casti).strip()
    if not odpoved:
        return "Model nic nevrátil. Zkus to znovu."
    HISTORIE.append({"role": "user", "content": text[:4000]})
    HISTORIE.append({"role": "assistant", "content": odpoved})
    del HISTORIE[:-8]
    return odpoved[:3500]


def odpoved(text: str) -> str:
    t = " ".join(_norm(text).split())
    if t in ("/start", "start", "ahoj", "cau", "nazdar"):
        return "Jarvis na příjmu (cloud).\n" + POMOC
    if t in ("pomoc", "help", "/help", "co umis"):
        return POMOC
    if "briefing" in t:
        u = _ukoly()
        return f"Notion: {u}" if not isinstance(u, list) else notion.sestav_prehled(u)
    if t in ("dnes", "co mam dnes", "co me dnes ceka", "dnesni ukoly"):
        u = _ukoly()
        return f"Notion: {u}" if not isinstance(u, list) else \
            notion.sestav_prehled(u, zitra=False, tyden=False)
    if t.startswith(("zitra", "co mam zitra", "zitrejsi")):
        u = _ukoly()
        if not isinstance(u, list):
            return f"Notion: {u}"
        d = datetime.now(CASOVA_ZONA).date()
        return notion.sestav_rozsah(u, d + timedelta(days=1), d + timedelta(days=1), "Zítra")
    if "tyden" in t:
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
    return model(text)

def zpracuj(u: dict) -> None:
    msg = u.get("message") or {}
    if str((msg.get("chat") or {}).get("id", "")) != chat_id():
        return
    kdy = msg.get("date")
    if kdy and time.time() - kdy > 900:
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    print(f"prijem: {len(text)} znaku", flush=True)
    pisni()
    try:
        posli(odpoved(text))
    except RuntimeError as e:
        print(f"telegram: {e}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sekundy", type=int, default=280)
    ap.add_argument("--okno", default="4-16", help="hodiny Praha, kdy smí běžet (od-do)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--zkouska", default="", help="pošli zkušební odpověď modelu a skonči")
    args = ap.parse_args()
    if not chat_id():
        print("chybí TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1
    if args.zkouska:
        posli(model(args.zkouska))
        print("Zkušební odpověď odeslána.")
        return 0
    try:
        od, do = (int(x) for x in args.okno.split("-"))
    except ValueError:
        od, do = 4, 16
    now = datetime.now(CASOVA_ZONA)
    if not (od <= now.hour < do):
        print(f"Mimo okno {od}–{do} Praha ({now:%H:%M}) – končím.")
        return 0
    konec_okna = now.replace(hour=do, minute=0, second=0, microsecond=0)
    zbyva = (konec_okna - now).total_seconds()
    konec = time.time() + max(30, min(args.sekundy, zbyva))
    offset = 0
    while time.time() < konec:
        try:
            data = tg("getUpdates", {"offset": offset, "timeout": 45, "limit": 10,
                                     "allowed_updates": ["message"]}, timeout=60)
        except RuntimeError as e:
            print(e, file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        for u in data.get("result", []):
            offset = u["update_id"] + 1
            if args.dry_run:
                print(f"DRY-RUN update {u['update_id']}", flush=True)
                continue
            try:
                zpracuj(u)
            except Exception as e:  # jedna zpráva nesmí shodit běh
                print(f"chyba: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    # potvrdit, co jsme viděli, ať se to příště neopakuje
    if offset:
        try:
            tg("getUpdates", {"offset": offset, "timeout": 0, "limit": 1}, timeout=20)
        except RuntimeError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
