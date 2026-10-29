import unittest
import test_recommendations as fixture
import test_simple_postgres as postgres
from postgres_test_support import postgres_tests_enabled


@unittest.skipUnless(postgres_tests_enabled(),'Yalıtılmış PostgreSQL test sunucusu yapılandırılmadı.')
class RecommendationsPostgresTest(fixture.RecommendationsTest):
    setUpClass=classmethod(postgres.SimplePostgresTest.setUpClass.__func__)
    tearDownClass=classmethod(postgres.SimplePostgresTest.tearDownClass.__func__)
    cleanup_database=postgres.SimplePostgresTest.cleanup_database
    setUp=postgres.SimplePostgresTest.setUp
