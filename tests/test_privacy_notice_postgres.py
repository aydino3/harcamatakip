import os
import unittest
from unittest.mock import patch
import app as core
import test_privacy_notice as fixture
import test_simple_postgres as postgres
from postgres_test_support import postgres_tests_enabled


@unittest.skipUnless(postgres_tests_enabled(), 'Yalıtılmış PostgreSQL test sunucusu yapılandırılmadı.')
class PrivacyNoticePostgresTest(fixture.PrivacyNoticeTest):
    setUpClass = classmethod(postgres.SimplePostgresTest.setUpClass.__func__)
    tearDownClass = classmethod(postgres.SimplePostgresTest.tearDownClass.__func__)
    cleanup_database = postgres.SimplePostgresTest.cleanup_database

    def setUp(self):
        postgres.SimplePostgresTest.setUp(self)
        environment = patch.dict(os.environ, {'APP_ENV': 'development', 'PRIVATE_BETA': '0', 'PRIVACY_NOTICE_PATH': ''})
        environment.start()
        self.addCleanup(environment.stop)
        with core.connect() as db:
            db.execute('UPDATE users SET email=? WHERE id=?', ('fixture@example.invalid', self.user))

    def test_receipt_tables_have_rls_and_no_public_privileges(self):
        self.setup_web()
        with core.connect() as db:
            for table in ('privacy_notice_versions', 'privacy_acknowledgments'):
                row = db.execute('SELECT relrowsecurity FROM pg_class WHERE oid=?::regclass', (table,)).fetchone()
                self.assertTrue(row['relrowsecurity'])
                for role in ('anon', 'authenticated'):
                    if db.execute('SELECT 1 FROM pg_roles WHERE rolname=?', (role,)).fetchone():
                        self.assertFalse(db.execute('SELECT has_table_privilege(?,?,?) AS allowed', (role, table, 'SELECT')).fetchone()['allowed'])
