import contextlib
import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import posli_bot as bot


class BotTests(unittest.TestCase):
    def setUp(self):
        for name, kwargs in (
            ('urllib.request.urlopen', {'side_effect': AssertionError('Síť zakázána')}),
            ('chat_id', {'return_value': '123'}),
            ('notion.nacti_ukoly', {'return_value': []}),
            ('model', {'return_value': ''}),
            ('lokal_zije', {'return_value': False}),
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
                    self.assertEqual(posli.call_args.args[0], 'Hlasovku v cloudu nepřepíšu – napiš to prosím textem.')

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
        with patch.object(bot, 'datetime') as dt, patch.object(bot, 'tg', side_effect=telegram) as tg, patch.object(bot, 'zpracuj', side_effect=zpracuj), patch('sys.argv', ['bot', '--okno', '4-17', '--diagnostika']), contextlib.redirect_stdout(io.StringIO()) as out:
            dt.now.side_effect = lambda zone: hodiny[0]
            self.assertEqual(bot.main(), 0)
            self.assertEqual(tg.call_count, 1)
            self.assertIn('2 novych zprav', out.getvalue())
            self.assertIn('zpráv 2, bez odpovědi 1', out.getvalue())
            self.assertIn('konflikt: ne', out.getvalue())

    def test_pozdrav_hranice(self):
        for hour, expected in ((3,'Dobrý večer'),(4,'Dobré ráno'),(9,'Dobré ráno'),
                               (10,'Dobré dopoledne'),(11,'Dobré dopoledne'),
                               (12,'Dobré odpoledne'),(17,'Dobré odpoledne'),(18,'Dobrý večer')):
            with self.subTest(hour=hour):
                self.assertEqual(bot.notion._pozdrav(datetime(2026,11,11,hour)), expected)

    def test_prehled_pouziva_prazsky_cas(self):
        with patch.object(bot.notion, 'datetime') as dt:
            dt.now.return_value = datetime(2026,11,11,12,tzinfo=bot.CASOVA_ZONA)
            self.assertTrue(bot.notion.sestav_prehled([]).startswith('Dobré odpoledne, Ondro.'))
            for call in dt.now.call_args_list:
                self.assertEqual(call.args, (bot.CASOVA_ZONA,))

    def test_409_ukonci_bez_zpravy_a_bez_opakovani(self):
        with patch.object(bot, 'datetime') as dt, patch.object(bot, 'token', return_value='offline'), patch.object(bot.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('offline',409,'Conflict',{},None)) as net, patch.object(bot, 'posli') as posli, patch.object(bot.time, 'sleep') as sleep, patch('sys.argv', ['bot','--diagnostika']), contextlib.redirect_stdout(io.StringIO()) as out:
            dt.now.return_value = datetime(2026,11,11,12,tzinfo=bot.CASOVA_ZONA)
            self.assertEqual(bot.main(), 0)
            self.assertEqual(net.call_count, 1)
            posli.assert_not_called()
            sleep.assert_not_called()
            self.assertIn('lokální bot poslouchá – končím', out.getvalue())
            self.assertIn('konflikt: ano', out.getvalue())

    def test_jine_chyby_dal_opakuji(self):
        for error in (urllib.error.HTTPError('offline',429,'Rate',{},io.BytesIO(b'{}')),
                      urllib.error.HTTPError('offline',503,'Unavailable',{},io.BytesIO(b'{}')),
                      urllib.error.URLError('offline')):
            with self.subTest(error=type(error).__name__), patch.object(bot, 'datetime') as dt, patch.object(bot, 'token', return_value='offline'), patch.object(bot.urllib.request, 'urlopen', side_effect=[error,urllib.error.HTTPError('offline',409,'Conflict',{},None)]) as net, patch.object(bot.time, 'sleep') as sleep, patch('sys.argv', ['bot']), contextlib.redirect_stdout(io.StringIO()):
                dt.now.return_value = datetime(2026,11,11,12,tzinfo=bot.CASOVA_ZONA)
                self.assertEqual(bot.main(),0)
                self.assertEqual(net.call_count,2)
                sleep.assert_called_once()

    def test_hlasovka_se_nestahuje_ani_neprepisuje(self):
        with patch.object(bot,'stahni') as stahni, patch.object(bot,'posli') as posli, patch.object(bot,'model') as model:
            for typ in ('voice','audio'):
                self.assertTrue(bot.zpracuj({'message': {'chat': {'id': 123},typ: {'file_id': 'offline'}}}))
            stahni.assert_not_called()
            model.assert_not_called()
            self.assertEqual(posli.call_count,2)
            for call in posli.call_args_list:
                self.assertEqual(call.args[0],'Hlasovku v cloudu nepřepíšu – napiš to prosím textem.')

    def test_okno_0_23_je_cely_den_i_vikend(self):
        sobota = datetime(2026, 11, 14, 23, 30, tzinfo=bot.CASOVA_ZONA)
        for now in (sobota, sobota.replace(hour=0, minute=0), sobota.replace(day=15, hour=12)):
            with self.subTest(now=now):
                self.assertTrue(bot.v_okne(now, 0, 23))
        self.assertTrue(bot.v_okne(sobota.replace(hour=10), 4, 17))
        self.assertFalse(bot.v_okne(sobota, 4, 17))

    def test_vychozi_okno_posloucha_v_sobotu_ve_2330(self):
        with patch.object(bot, 'datetime') as dt, patch.object(bot, 'token', return_value='offline'), patch.object(bot.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('offline',409,'Conflict',{},None)) as net, patch('sys.argv', ['bot','--diagnostika']), contextlib.redirect_stdout(io.StringIO()) as out:
            dt.now.return_value = datetime(2026, 11, 14, 23, 30, tzinfo=bot.CASOVA_ZONA)
            self.assertEqual(bot.main(), 0)
            self.assertEqual(net.call_count, 1)
            self.assertNotIn('Mimo okno', out.getvalue())
            self.assertIn('konflikt: ano', out.getvalue())


class TepTests(unittest.TestCase):
    """Srdeční tep lokálního bota: čerstvý = cloud neposlouchá."""

    def _tep(self, posun: int) -> bytes:
        cas = datetime.now(bot.CASOVA_ZONA) + timedelta(seconds=posun)
        return json.dumps({'cas': cas.isoformat(timespec='seconds')}).encode()

    def test_cerstvy_tep_znamena_lokalni_bot_zije(self):
        with patch.object(bot.urllib.request, 'urlopen', return_value=io.BytesIO(self._tep(-30))):
            self.assertTrue(bot.lokal_zije())

    def test_stary_tep_znamena_pc_spi(self):
        with patch.object(bot.urllib.request, 'urlopen', return_value=io.BytesIO(self._tep(-600))):
            self.assertFalse(bot.lokal_zije())

    def test_chybejici_tep_je_false(self):
        with patch.object(bot.urllib.request, 'urlopen',
                          side_effect=urllib.error.URLError('offline')):
            self.assertFalse(bot.lokal_zije())

    def test_kdyz_lokal_zije_cloud_vubec_neposloucha(self):
        with patch.object(bot, 'lokal_zije', return_value=True), \
                patch.object(bot, 'chat_id', return_value='123'), \
                patch.object(bot, 'token', return_value='offline'), \
                patch.object(bot.urllib.request, 'urlopen') as net, \
                patch.object(bot.time, 'sleep'), \
                patch('sys.argv', ['bot', '--sekundy', '1', '--diagnostika']), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bot.main(), 0)
            net.assert_not_called()
            self.assertIn('lokální bot žije', out.getvalue())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        cesta = Path(__file__).resolve().parent / '.github' / 'workflows' / 'bot.yml'
        self.radky = cesta.read_text(encoding='utf-8').splitlines()

    def test_cron_je_zaloha_pro_pripad_preruseneho_retezeni(self):
        crony = [r for r in self.radky if r.strip().startswith('- cron:')]
        self.assertEqual(len(crony), 1)
        self.assertIn('"21 */6 * * *"', crony[0])

    def test_retezeni_spousti_dalsi_beh_pres_PAT(self):
        vse = '\n'.join(self.radky)
        self.assertIn('secrets.PAT', vse)
        self.assertIn('gh workflow run bot.yml', vse)
        self.assertIn('status=queued', vse)
        self.assertIn('if: always()', vse)

    def test_prikaz_ma_okno_0_23(self):
        prikazy = [r for r in self.radky if 'posli_bot.py --sekundy' in r]
        self.assertEqual(len(prikazy), 1)
        self.assertIn('--okno 0-23', prikazy[0])

    def test_beh_se_nerusi(self):
        self.assertIn('cancel-in-progress: false', [r.strip() for r in self.radky])


if __name__ == '__main__':
    unittest.main()
