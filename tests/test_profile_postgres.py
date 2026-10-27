import unittest
import profile_settings as profiles
import test_profile_settings as fixture
import test_simple_postgres as postgres
from postgres_test_support import postgres_tests_enabled


@unittest.skipUnless(postgres_tests_enabled(),'Yalıtılmış PostgreSQL test sunucusu yapılandırılmadı.')
class ProfilePostgresTest(fixture.ProfileSettingsTest):
    setUpClass=classmethod(postgres.SimplePostgresTest.setUpClass.__func__)
    tearDownClass=classmethod(postgres.SimplePostgresTest.tearDownClass.__func__)
    cleanup_database=postgres.SimplePostgresTest.cleanup_database

    def setUp(self):
        postgres.SimplePostgresTest.setUp(self)
        self.email='cashflow-fixture@example.invalid'
        profiles.initialize()
