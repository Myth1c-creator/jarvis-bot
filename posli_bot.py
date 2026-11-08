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
    "• nebo napiš název úkolu – najdu ho\n\n"
    "Připomínky a volný chat umí jen bot na zapnutém PC."
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
        propadle = [x for x in u if x["termin"].date() < d]
        return f"Nehotových úkolů: {len(u)} (dnes {len(dnesni)}, propadlých {len(propadle)})."
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
    u = _ukoly()
    if isinstance(u, list):
        nalez = notion.najdi_ukol(u, text)
        if nalez:
            radky = [f"Našel jsem v Notionu ({len(nalez)}):"]
            radky += [f"• {x['termin']:%d.%m. %H:%M} – {x['nazev']}" for x in nalez[:5]]
            return "\n".join(radky)
    return "Tomu nerozumím (cloud režim). Napiš „pomoc“."


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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not chat_id():
        print("chybí TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1
    konec = time.time() + args.sekundy
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
