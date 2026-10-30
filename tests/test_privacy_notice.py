"""Privacy declaration tests use isolated synthetic accounts, never real data."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as core
import privacy_notice as privacy
import webapp


def notice_fixture():
    keys = ('controller', 'data_and_purposes', 'legal_basis_and_collection',
            'recipients_and_transfers', 'retention', 'rights_and_application')
    return {'publication_ready': True, 'version': 'synthetic-v1',
            'controller': {'name': 'Synthetic controller', 'email': 'privacy@example.invalid'},
            'languages': {language: {'title': 'Synthetic notice', 'sections': [
                {'key': key, 'title': key, 'text': 'Synthetic information: ' + key}
                for key in keys]} for language in ('tr', 'en')}}


class PrivacyNoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(core, 'DB_PATH', Path(self.temp.name) / 'synthetic.sqlite3').start()
        patch.object(core, '_DATABASE_URL', '').start()
        patch.dict(os.environ, {'APP_ENV': 'development', 'PRIVATE_BETA': '0', 'PRIVACY_NOTICE_PATH': '', 'PRIVACY_NOTICE_ALLOW_DRAFT': '0'}).start()
        core.init_db()
        self.user = core.create_user('fixture@example.invalid', 'synthetic-password')
        self.other = core.create_user('other@example.invalid', 'synthetic-password')

    def setup_web(self, raw=None):
        self.raw = raw or notice_fixture()
        self.web = webapp.create_app({'TESTING': True, 'PRIVATE_BETA': False, 'PRIVACY_NOTICE': self.raw})
        self.notice = self.web.extensions['privacy_notice']
        self.client = self.web.test_client()

    def proof(self, language='tr'):
        doc = self.notice['documents'][language]
        return {'read': True, 'version': doc['version'], 'sha256': doc['sha256'], 'language': language}

    def counts(self):
        with core.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) AS n FROM ' + table).fetchone()['n']
                         for table in ('users', 'auth_sessions_v2', 'privacy_acknowledgments'))

    def signup(self, proof):
        return self.client.post('/api/register', json={'email': 'new@example.invalid', 'username': 'new.account',
            'password': 'synthetic-password', 'privacy_acknowledgment': proof})

    def test_disabled_gate_preserves_existing_login(self):
        core.create_user('disabled@example.invalid', 'synthetic-password', username='disabled.fixture')
        client = webapp.create_app({'TESTING': True, 'PRIVATE_BETA': False, 'PRIVACY_NOTICE': None}).test_client()
        self.assertEqual(client.get('/api/privacy-notice').get_json(), {'enabled': False})
        self.assertEqual(client.post('/api/login', json={'identifier': 'disabled.fixture', 'password': 'synthetic-password'}).status_code, 200)

    def test_public_document_contains_exact_hashed_text_but_no_account_records(self):
        self.setup_web()
        response = self.client.get('/api/privacy-notice')
        self.assertEqual(response.status_code, 200)
        doc = response.get_json()['documents']['tr']
        digest = doc.pop('sha256')
        self.assertEqual(digest, hashlib.sha256(privacy.canonical(doc).encode()).hexdigest())
        self.assertNotIn('fixture@example.invalid', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/session').get_json()['privacy_notice']['version'], 'synthetic-v1')
        self.assertEqual(self.client.get('/api/privacy-acknowledgments').status_code, 401)

    def test_missing_or_non_boolean_declaration_cannot_create_account_or_session(self):
        self.setup_web()
        before = self.counts()
        for value in (None, {}, {'read': False}, {'read': 'true'}, {'read': 1}):
            with self.subTest(value=value):
                self.assertEqual(self.signup(value).status_code, 428)
        self.assertEqual(self.counts(), before)

    def test_stale_or_altered_proof_is_rejected_before_registration(self):
        self.setup_web()
        before = self.counts()
        for key, value in (('version', 'v0'), ('sha256', '0'*64), ('language', 'xx')):
            response = self.signup({**self.proof(), key: value})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()['code'], 'privacy_notice_changed')
        self.assertEqual(self.counts(), before)

    def test_registration_and_login_record_authenticated_server_generated_receipts(self):
        self.setup_web()
        self.assertEqual(self.signup(self.proof()).status_code, 200)
        receipts = self.client.get('/api/privacy-acknowledgments').get_json()['acknowledgments']
        self.assertEqual(len(receipts), 1)
        row = receipts[0]
        self.assertEqual(row['action'], 'register')
        self.assertEqual(row['declaration'], 'notice_read_and_informed')
        self.assertEqual(row['notice_sha256'], self.proof()['sha256'])
        self.assertTrue(row['acknowledged_at'].endswith('+00:00'))
        self.assertNotIn('session_token_hash', row)
        client = self.web.test_client()
        response = client.post('/api/login', json={'identifier': 'NEW.ACCOUNT', 'password': 'synthetic-password',
            'privacy_acknowledgment': self.proof('en')})
        self.assertEqual(response.status_code, 200)
        receipts = client.get('/api/privacy-acknowledgments').get_json()['acknowledgments']
        self.assertEqual({row['action'] for row in receipts}, {'register', 'login'})
        self.assertEqual({row['language'] for row in receipts}, {'tr', 'en'})
        with core.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM privacy_acknowledgments WHERE session_token_hash IS NOT NULL').fetchone()['n'], 1)

    def test_wrong_password_does_not_create_receipt(self):
        self.setup_web()
        before = self.counts()
        response = self.client.post('/api/login', json={'identifier': 'fixture@example.invalid', 'password': 'wrong-password', 'privacy_acknowledgment': self.proof()})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.counts(), before)

    def test_record_failure_rolls_back_user_and_session(self):
        self.setup_web()
        before = self.counts()
        acknowledgment = privacy.prepare_acknowledgment(self.notice, self.proof(), 'register')
        with patch.object(privacy, 'record_acknowledgment', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                core.create_user('rollback@example.invalid', 'synthetic-password', username='rollback', privacy_ack=acknowledgment)
            with self.assertRaises(RuntimeError):
                core.create_session(self.user, privacy_ack=privacy.prepare_acknowledgment(self.notice, self.proof(), 'login'))
        self.assertEqual(self.counts(), before)

    def test_published_version_cannot_change_in_place(self):
        self.setup_web()
        changed = copy.deepcopy(self.raw)
        changed['languages']['tr']['sections'][0]['text'] = 'Changed'
        with self.assertRaisesRegex(ValueError, 'yeni sürüm'):
            privacy.initialize(privacy.load_notice(changed))
        changed['version'] = 'synthetic-v2'
        privacy.initialize(privacy.load_notice(changed))
        with self.assertRaises(privacy.StaleNotice):
            privacy.prepare_acknowledgment(privacy.load_notice(changed), self.proof(), 'login')

    def test_receipts_are_owner_scoped_and_removed_with_account(self):
        self.setup_web()
        acknowledgment = privacy.prepare_acknowledgment(self.notice, self.proof(), 'login')
        token, _ = core.create_session(self.user, privacy_ack=acknowledgment)
        core.create_session(self.other, privacy_ack=acknowledgment)
        self.client.set_cookie(core.COOKIE, token)
        self.assertEqual(len(self.client.get('/api/privacy-acknowledgments').get_json()['acknowledgments']), 1)
        with core.connect() as db:
            db.execute('DELETE FROM users WHERE id=?', (self.user,))
        self.assertEqual(privacy.acknowledgments_for_user(self.user), [])
        self.assertEqual(len(privacy.acknowledgments_for_user(self.other)), 1)

    def test_draft_and_incomplete_notice_cannot_be_enabled_in_production(self):
        raw = notice_fixture()
        raw['publication_ready'] = False
        with self.assertRaises(ValueError):
            privacy.load_notice(raw)
        self.assertTrue(privacy.load_notice(raw, allow_draft=True)['draft'])
        with patch.dict(os.environ, {'APP_ENV': 'production', 'PRIVATE_BETA': '0'}):
            with self.assertRaises(ValueError):
                webapp.create_app({'TESTING': True, 'PRIVACY_NOTICE': raw, 'PRIVACY_NOTICE_ALLOW_DRAFT': True})
        raw['publication_ready'] = True
        raw['languages']['tr']['sections'].pop()
        with self.assertRaises(ValueError):
            privacy.load_notice(raw)

    def test_explicit_operator_setting_can_publish_draft_without_marking_it_final(self):
        raw = notice_fixture()
        raw['publication_ready'] = False
        with patch.dict(os.environ, {'APP_ENV': 'production', 'PRIVATE_BETA': '0', 'PRIVACY_NOTICE_ALLOW_DRAFT': '1'}):
            application = webapp.create_app({'TESTING': True, 'PRIVACY_NOTICE': raw})
        client = application.test_client()
        self.assertTrue(client.get('/api/privacy-notice').get_json()['draft'])
        self.assertEqual(client.post('/api/login', json={'identifier': 'anything', 'password': 'synthetic-password'}).status_code, 428)
