"""Profil/kart tanımları için sentetik sahiplik ve kalıcılık testleri."""
from pathlib import Path
import tempfile,unittest
import app as core
import profile_settings as profiles
import simple_cashflow as finance
import webapp


class ProfileSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();previous=core.DB_PATH
        core.DB_PATH=Path(self.temp.name)/'profile.sqlite3'
        self.addCleanup(self.temp.cleanup);self.addCleanup(setattr,core,'DB_PATH',previous)
        core.init_db();finance.initialize();profiles.initialize()
        self.email='profile-fixture@example.invalid'
        self.user=core.create_user(self.email,'synthetic-only-password')
        self.other=core.create_user('other-profile@example.invalid','synthetic-only-password')

    def test_current_profile_name_email_and_isolation(self):
        first=profiles.get_profile(self.user)
        self.assertEqual(first['email'],self.email)
        profiles.save_name(self.user,{'display_name':'Deneme profili'})
        self.assertEqual(profiles.get_profile(self.user)['display_name'],'Deneme profili')
        self.assertNotEqual(profiles.get_profile(self.other)['display_name'],'Deneme profili')

    def test_legacy_cards_remain_visible_and_specific_due_date_does_not_create_payments(self):
        card=core.save_credit_card(self.user,{'bank_name':'Sentetik banka','display_name':'Eski kart','closing_day':22,'due_day':3})
        first=profiles.get_profile(self.user)['cards'][0]
        self.assertEqual(first['id'],f'legacy:{card}')
        self.assertEqual(first['payment_due_day'],3)
        profiles.save_card(self.user,{**first,'payment_due_day':9})
        changed=profiles.get_profile(self.user)['cards'][0]
        self.assertEqual(changed['payment_due_day'],9)
        with core.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],0)
            row=db.execute('SELECT closing_day,due_day FROM credit_cards WHERE id=?',(card,)).fetchone()
            self.assertEqual((row[0],row[1]),(22,3))

    def test_new_card_without_invented_cutoff_date_is_repeat_safe_and_owned(self):
        payload={'bank_name':'Sentetik banka','display_name':'Yeni kart','last_four':'1234','payment_due_on':'2027-05-08'}
        first=profiles.save_card(self.user,payload)
        self.assertEqual(profiles.save_card(self.user,payload),first)
        cards=profiles.get_profile(self.user)['cards']
        self.assertEqual(len(cards),1)
        self.assertIsNone(cards[0]['monthly_due_day'])
        self.assertEqual(profiles.get_profile(self.other)['cards'],[])
        with self.assertRaises(ValueError):profiles.save_card(self.other,{**cards[0],'payment_due_on':'2027-05-10'})
        with core.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM credit_cards').fetchone()[0],0)

    def test_invalid_due_date_name_and_stale_card_edit_are_rejected(self):
        payload={'bank_name':'Sentetik','display_name':'Kart','payment_due_on':'2027-05-08'}
        for changes in ({'payment_due_on':''},{'payment_due_on':'2027-02-30'},{'last_four':'ab12'},{'bank_name':''}):
            with self.assertRaises(ValueError):profiles.save_card(self.user,{**payload,**changes})
        profiles.save_card(self.user,payload)
        card=profiles.get_profile(self.user)['cards'][0]
        profiles.save_card(self.user,{**card,'payment_due_day':9})
        with self.assertRaises(ValueError):profiles.save_card(self.user,{**card,'payment_due_day':10})

    def test_profile_api_requires_login_and_csrf(self):
        client=webapp.create_app({'TESTING':True,'PRIVATE_BETA':False}).test_client()
        self.assertEqual(client.get('/api/profile').status_code,401)
        login=client.post('/api/login',json={'email':self.email,'password':'synthetic-only-password'})
        self.assertEqual(client.get('/api/profile').json['email'],self.email)
        self.assertEqual(client.put('/api/profile',json={'display_name':'Yeni ad'}).status_code,403)
        self.assertEqual(client.put('/api/profile',json={'display_name':'Yeni ad'},headers={'X-CSRF-Token':login.json['csrf']}).status_code,200)

    def test_duplicate_card_edit_is_validation_error_and_preserves_both_cards(self):
        base={'bank_name':'Sentetik','payment_due_on':'2027-05-08'}
        profiles.save_card(self.user,{**base,'display_name':'Birinci'})
        profiles.save_card(self.user,{**base,'display_name':'İkinci'})
        second=next(card for card in profiles.get_profile(self.user)['cards'] if card['display_name']=='İkinci')
        with self.assertRaisesRegex(ValueError,'zaten tanımlı'):
            profiles.save_card(self.user,{**second,'display_name':'Birinci'})
        self.assertEqual({c['display_name'] for c in profiles.get_profile(self.user)['cards']},{'Birinci','İkinci'})

    def test_existing_statement_due_is_shown_without_inventing_or_changing_dates(self):
        card=core.save_credit_card(self.user,{'bank_name':'Sentetik','display_name':'Ekstreli kart','closing_day':22,'due_day':3})
        with core.connect() as db:
            db.execute('INSERT INTO card_statements(user_id,card_id,period_start,closing_on,due_on) VALUES(?,?,?,?,?)',(self.user,card,'2027-04-01','2027-04-22','2027-05-03'))
        self.assertEqual(profiles.get_profile(self.user)['cards'][0]['payment_due_day'],3)
        with core.connect() as db:self.assertEqual(db.execute('SELECT due_on FROM card_statements WHERE user_id=?',(self.user,)).fetchone()[0],'2027-05-03')
