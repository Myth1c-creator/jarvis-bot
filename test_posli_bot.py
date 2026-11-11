import contextlib
import io
import unittest
from datetime import datetime
from unittest.mock import patch

import posli_bot as bot


class BotTests(unittest.TestCase):
    def setUp(self):
        for name, kwargs in (
            ('urllib.request.urlopen', {'side_effect': AssertionError('Síť zakázána')}),
            ('chat_id', {'return_value': '123'}),
            ('notion.nacti_ukoly', {'return_value': []}),
            ('model', {'return_value': ''}),
        ):
            target = patch('posli_bot.' + name, **kwargs)
            target.start()
            self.addCleanup(target.stop)
        bot.notion.zapomen_cache() if hasattr(bot.notion, 'zapomen_cache') else None

    def test_dnes(self):
        with patch.object(bot.notion, 'sestav_prehled', return_value='DNES') as prehled:
            for text in ('jaké máme dneska tasky', 'co dnes', 'ukoly na dnes', 'DNEŠNÍ PLÁN'):
                with self.subTest(text=text):
                    self.assertEqual(bot.odpoved(text), 'DNES')
                    prehled.assert_called_with([], zitra=False, tyden=False)

    def test_zitra_a_tyden(self):
        with patch.object(bot, 'datetime') as dt, patch.object(bot.notion, 'sestav_rozsah', return_value='ROZSAH') as rozsah:
            now = datetime(2026, 11, 11, 10, tzinfo=bot.CASOVA_ZONA)
            dt.now.return_value = now
            for text, end, title in [('co mám zítra', 12, 'Zítra'), ('týden', 18, 'Příštích 7 dní')]:
                self.assertEqual(bot.odpoved(text), 'ROZSAH')
                rozsah.assert_called_with([], now.date().replace(day=12), now.date().replace(day=end), title)

    def test_mutace_ma_prednost(self):
        with patch.object(bot, 'novy_ukol', return_value='NOVY') as novy:
            text = 'nový úkol zavolat dnes 18:00'
            self.assertEqual(bot.odpoved(text), 'NOVY')
            novy.assert_called_once_with(text)
        self.assertIn('Nic jsem nenašel', bot.odpoved('hotovo dnešní úkol'))

    def test_prazdny_model(self):
        self.assertEqual(bot.odpoved('nevím jak dál'), bot.FALLBACK)

    def test_media_a_jine_zpravy(self):
        for druh in ('video', 'video_note', 'animation', 'sticker', 'voice', 'audio', 'document', 'contact', 'location'):
            with self.subTest(druh=druh), patch.object(bot, 'posli') as posli:
                self.assertTrue(bot.zpracuj({'message': {'chat': {'id': 123}, druh: {'file_id': 'offline'}}}))
                posli.assert_called_once()
                self.assertTrue(posli.call_args.args[0].strip())
                if druh in ('voice', 'audio'):
                    self.assertIn('zapnutém PC', posli.call_args.args[0])

    def test_obrazek_zustava_podporovany(self):
        with patch.object(bot, 'foto', return_value='OBRAZEK') as foto:
            self.assertEqual(bot.zprava({'document': {'mime_type': 'image/png'}}), 'OBRAZEK')
            foto.assert_called_once()

    def test_zpracuj_nikdy_neposle_prazdno(self):
        for text in ('', '   ', None):
            with patch.object(bot, 'odpoved', return_value=text), patch.object(bot, 'posli') as posli:
                self.assertTrue(bot.zpracuj({'message': {'chat': {'id': 123}, 'text': 'dotaz'}}))
                posli.assert_called_once_with(bot.FALLBACK)

    def test_posli_chranene_i_primo(self):
        with patch.object(bot, 'tg') as tg:
            for text in ('', '   ', None):
                bot.posli(text)
                self.assertEqual(tg.call_args.args[1]['text'], bot.FALLBACK)

    def test_stara_zprava_odpovi_bez_provedeni(self):
        with patch.object(bot, 'odpoved') as odpoved:
            self.assertIn('zpožděním', bot.zprava({'date': 1, 'text': 'hotovo úkol'}))
            odpoved.assert_not_called()

    def test_vyjimka_a_selhani_odeslani(self):
        msg = {'message': {'chat': {'id': 123}, 'text': 'dotaz'}}
        with patch.object(bot, 'zprava', side_effect=ValueError()), patch.object(bot, 'posli') as posli:
            self.assertTrue(bot.zpracuj(msg))
            self.assertTrue(posli.call_args.args[0].strip())
        with patch.object(bot, 'posli', side_effect=RuntimeError()):
            self.assertFalse(bot.zpracuj(msg))

    def test_okno_v_1630(self):
        now = datetime(2026, 11, 11, 16, 30, tzinfo=bot.CASOVA_ZONA)
        self.assertFalse(bot.v_okne(now, 4, 16))
        self.assertTrue(bot.v_okne(now, 4, 17))
        self.assertFalse(bot.v_okne(now.replace(hour=17), 4, 17))

    def test_start_mimo_okno_nevola_telegram(self):
        for hour, minute, okno in ((16, 30, '4-16'), (17, 0, '4-17'), (3, 59, '4-17')):
            with patch.object(bot, 'datetime') as dt, patch.object(bot, 'tg') as tg, patch('sys.argv', ['bot', '--okno', okno, '--diagnostika']), contextlib.redirect_stdout(io.StringIO()) as out:
                dt.now.return_value = datetime(2026, 11, 11, hour, minute, tzinfo=bot.CASOVA_ZONA)
                self.assertEqual(bot.main(), 0)
                tg.assert_not_called()
                self.assertIn('poslech 0.0 s', out.getvalue())

    def test_diagnostika_a_konec_okna(self):
        now = datetime(2026, 11, 11, 16, 30, tzinfo=bot.CASOVA_ZONA)
        hodiny = [now]
        def telegram(*args, **kwargs):
            return {'result': [{'update_id': 1, 'message': {'chat': {'id': 123}}},
                               {'update_id': 2, 'message': {'chat': {'id': 123}}}]}
        def zpracuj(update):
            if update['update_id'] == 2:
                hodiny[0] = now.replace(hour=17, minute=0)
                return False
            return True
        with patch.object(bot, 'datetime') as dt, patch.object(bot, 'tg', side_effect=telegram) as tg, patch.object(bot, 'zpracuj', side_effect=zpracuj), patch('sys.argv', ['bot', '--diagnostika']), contextlib.redirect_stdout(io.StringIO()) as out:
            dt.now.side_effect = lambda zone: hodiny[0]
            self.assertEqual(bot.main(), 0)
            self.assertEqual(tg.call_count, 1)
            self.assertIn('2 novych zprav', out.getvalue())
            self.assertIn('zpráv 2, bez odpovědi 1', out.getvalue())


if __name__ == '__main__':
    unittest.main()
