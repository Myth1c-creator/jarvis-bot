# Jarvis – cloudový Telegram bot

Odpovídá na zprávy i když je Ondrův počítač vypnutý. Běží na GitHub Actions
jako krátká úloha každých 5 minut (Po–Pá 4:00–16:00 Praha, tedy přes školu).

## Co umí

- `briefing` – dnes, zítra, tento týden, propadlé
- `dnes`, `zítra`, `týden`
- `kolik mám úkolů`
- `hotovo <název>` – označí úkol v Notionu
- název úkolu → najde ho (např. `call bebe`)
- `pomoc`

Připomínky 45 min před termínem a volný chat (OpenCode) umí jen lokální bot
na zapnutém počítači – cloud tyhle věci neumí.

## Jak to funguje

- Telegram drží zprávy, dokud si je někdo nevyzvedne. Bot při každém běhu
  zavolá `getUpdates`, odpoví a na konci je potvrdí, takže se neopakují.
- Stav se nikam neukládá – běh je bez souborů, takže se nemůže rozbít.
- Odpovědět smí jen chat z `TELEGRAM_CHAT_ID`.

## Secrets

`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTION_TOKEN` – v Settings → Secrets and
variables → Actions. Nikdy nejsou v kódu.

## Ruční test

Actions → **Jarvis bot (cloud)** → Run workflow, pak napiš botovi do Telegramu.

## Vypnutí

Actions → workflow → Disable workflow. Lokální bot na PC funguje dál mimo
školní okno (4:00–16:00 Praha, Po–Pá), aby si nekonkurovali.
