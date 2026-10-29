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

    def test_registration_requires_explicit_username_and_preserves_chosen_name(self):
        client=webapp.create_app({'TESTING':True,'PRIVATE_BETA':False}).test_client()
        payload={'email':'chosen-name@example.invalid','password':'synthetic-only-password'}
        self.assertEqual(client.post('/api/register',json=payload).status_code,400)
        self.assertEqual(client.post('/api/register',json={**payload,'username':'x'}).status_code,400)
        self.assertEqual(client.post('/api/register',json={**payload,'username':'Chosen.Name'}).status_code,200)
        profile=client.get('/api/profile').json
        self.assertEqual(profile['username'],'chosen.name')
        self.assertEqual(profile['display_name'],'chosen.name')
        duplicate=client.post('/api/register',json={**payload,'email':'another-chosen@example.invalid','username':'CHOSEN.NAME'})
        self.assertEqual(duplicate.status_code,409)
        self.assertIn('zaten kullanılıyor',duplicate.json['error'])
        self.assertEqual(client.get('/api/profile').json['id'],profile['id'])
        with core.connect() as db:self.assertIsNone(db.execute('SELECT id FROM users WHERE email=?',('another-chosen@example.invalid',)).fetchone())

    def test_username_profile_edit_is_owned_atomic_and_case_insensitive(self):
        profiles.save_name(self.user,{'display_name':'Birinci etiket','username':'reserved_name'})
        profiles.save_name(self.other,{'display_name':'İkinci etiket','username':'second_name'})
        with self.assertRaisesRegex(ValueError,'zaten kullanılıyor'):
            profiles.save_name(self.other,{'display_name':'Kaydedilmemeli','username':'RESERVED_NAME'})
        self.assertEqual(profiles.get_profile(self.other)['display_name'],'İkinci etiket')
        self.assertEqual(profiles.get_profile(self.other)['username'],'second_name')
        profiles.save_name(self.user,{'display_name':'Yeni etiket','username':'Reserved_Name'})
        self.assertEqual(profiles.get_profile(self.user)['username'],'reserved_name')
        with self.assertRaises(core._DB_INTEGRITY_ERRORS):
            with core.connect() as db:db.execute('UPDATE users SET username=? WHERE id=?',('RESERVED_NAME',self.other))

    def test_username_concurrent_claim_has_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        gate=Barrier(2)
        def claim(index):
            gate.wait()
            try:return core.create_user(f'race-{index}@example.invalid','synthetic-only-password',username='Concurrent.Name')
            except core.UsernameUnavailable:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,(1,2)))
        self.assertEqual(sum(value is not None for value in results),1)
        with core.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM users WHERE lower(username)=?',('concurrent.name',)).fetchone()[0],1)

    def test_existing_visible_name_is_reserved_without_renaming_or_financial_changes(self):
        with core.connect() as db:
            db.execute('UPDATE users SET display_name=?,username=NULL WHERE id IN (?,?)',('Legacy.Name',self.user,self.other))
            core._initialize_usernames(db)
            core._initialize_usernames(db)
            names=db.execute('SELECT username,display_name FROM users WHERE id IN (?,?) ORDER BY id',(self.user,self.other)).fetchall()
            self.assertEqual(sum(row['username']=='legacy.name' for row in names),1)
            self.assertTrue(all(row['display_name']=='Legacy.Name' for row in names))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0],0)
        with self.assertRaises(core.UsernameUnavailable):core.create_user('legacy-claim@example.invalid','synthetic-only-password',username='LEGACY.NAME')

    def test_closing_day_edit_preserves_every_due_setting_and_legacy_cutoff(self):
        card=profiles.save_card(self.user,{'bank_name':'Kesim banka','display_name':'Kesim kart','payment_due_day':6,'statement_closing_day':26})['id']
        before=profiles.get_profile(self.user)['cards'][0]
        with core.connect() as db:stored=dict(db.execute('SELECT * FROM profile_cards WHERE user_id=?',(self.user,)).fetchone())
        profiles.save_card(self.user,{**before,'statement_closing_day':27})
        after=profiles.get_profile(self.user)['cards'][0]
        with core.connect() as db:changed=dict(db.execute('SELECT * FROM profile_cards WHERE user_id=?',(self.user,)).fetchone())
        self.assertEqual(after['statement_closing_day'],27)
        self.assertEqual({k:v for k,v in stored.items() if k not in ('statement_closing_day','version')},{k:v for k,v in changed.items() if k not in ('statement_closing_day','version')})
        with self.assertRaises(ValueError):profiles.save_card(self.user,{**before,'statement_closing_day':28})
        old=core.save_credit_card(self.user,{'bank_name':'Eski kesim','display_name':'Eski','closing_day':22,'due_day':6})
        legacy=next(row for row in profiles.get_profile(self.user)['cards'] if row['id']==f'legacy:{old}')
        profiles.save_card(self.user,{**legacy,'statement_closing_day':26})
        with core.connect() as db:self.assertEqual(db.execute('SELECT closing_day,due_day FROM credit_cards WHERE id=?',(old,)).fetchone()[0],22)

    def test_closing_day_validation_ownership_and_old_clients_preserve_reporting_day(self):
        payload={'bank_name':'Kesim','display_name':'Kart','payment_due_day':6,'statement_closing_day':26}
        for value in (0,32,True,2.5,'abc','-1'):
            with self.assertRaises(ValueError):profiles.save_card(self.user,{**payload,'statement_closing_day':value})
        card=profiles.save_card(self.user,payload)['id'];row=profiles.get_profile(self.user)['cards'][0]
        with self.assertRaises(ValueError):profiles.save_card(self.other,{**row,'statement_closing_day':20})
        old_client={k:v for k,v in row.items() if k!='statement_closing_day'}
        profiles.save_card(self.user,{**old_client,'display_name':'Eski istemci değişikliği'})
        self.assertEqual(profiles.get_profile(self.user)['cards'][0]['statement_closing_day'],26)
