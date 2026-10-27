"""Aynı sade ödeme regresyonlarını yalnızca yalıtılmış PostgreSQL'de çalıştır."""
import os,secrets,unittest
from unittest.mock import patch
from urllib.parse import urlsplit,urlunsplit
import app as core
import simple_cashflow as finance
import test_simple_cashflow as fixture
from postgres_test_support import LocalPostgresCluster,LocalPGliteDatabase,postgres_tests_enabled


@unittest.skipUnless(postgres_tests_enabled(),"Yalıtılmış PostgreSQL test sunucusu yapılandırılmadı.")
class SimplePostgresTest(fixture.SimpleCashflowTest):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.driver=psycopg2
        cls.cluster=None if os.environ.get('FINANCE_TEST_PGLITE_SERVER') else LocalPostgresCluster().start()

    @classmethod
    def tearDownClass(cls):
        if cls.cluster:cls.cluster.close()

    def setUp(self):
        from psycopg2 import sql
        self.pglite=None
        self.dbname='finance_test_'+secrets.token_hex(8)
        if self.cluster is None:
            self.pglite=LocalPGliteDatabase().start();dsn=self.pglite.dsn
        else:
            connection=self.driver.connect(self.cluster.admin_dsn)
            try:
                connection.autocommit=True
                with connection.cursor() as cursor:cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.dbname)))
            finally:connection.close()
            parsed=urlsplit(self.cluster.admin_dsn);dsn=urlunsplit(parsed._replace(path='/'+self.dbname))
        self.previous_url=core._DATABASE_URL
        self.previous_errors=core._DB_INTEGRITY_ERRORS
        self.environment=patch.dict(os.environ,{'EXPENSE_DATABASE_URL':dsn});self.environment.start()
        core._DATABASE_URL=dsn;core._DB_INTEGRITY_ERRORS=(self.driver.IntegrityError,)
        self.addCleanup(self.cleanup_database)
        core.init_db();finance.initialize()
        self.user=core.create_user('cashflow-fixture@example.invalid','synthetic-only-password')
        self.other=core.create_user('other-cashflow@example.invalid','synthetic-only-password')

    def cleanup_database(self):
        from psycopg2 import sql
        core._DATABASE_URL=self.previous_url;core._DB_INTEGRITY_ERRORS=self.previous_errors
        self.environment.stop()
        if self.pglite:self.pglite.close();return
        connection=self.driver.connect(self.cluster.admin_dsn)
        try:
            connection.autocommit=True
            with connection.cursor() as cursor:cursor.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.dbname)))
        finally:connection.close()
