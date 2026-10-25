Warning: truncated output (original token count: 24741)
Total output lines: 1305

import sqlite3
import tempfile
import unittest
import os
import stat
from datetime import date, timedelta
import http.cookiejar
import json
import base64
import threading
import contextlib
import io
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from werkzeug.serving import make_server

import app
import webapp


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original_path=app.DB_PATH
        self.original_backup_dir=app.BACKUP_DIR
        app.DB_PATH=Path(self.temp.name)/"test.sqlite3"
        app.BACKUP_DIR=Path(self.temp.name)/"backups"
        app.init_db()
        self.user_a=app.create_user("a@example.com","long-test-pass")
        self.user_b=app.create_user("b@example.com","long-test-pass")

    def tearDown(self):
        app.DB_PATH=self.original_path
        app.BACKUP_DIR=self.original_backup_dir
        self.temp.cleanup()

    def category_and_method(self,user_id):
        opts=app.user_options(user_id)
        return opts["categories"][0]["id"],opts["payment_methods"][0]["id"]

    def test_production_mode_requires_external_server_and_secure_cookie(self):
        error_output=io.StringIO()
        with patch.dict(os.environ,{"APP_ENV":"production"}), patch.object(app,"init_db") as initialize, contextlib.redirect_stderr(error_output):
            self.assertEqual(app.main([]),1)
            initialize.assert_not_called()
            self.assertTrue(app.secure_cookie_enabled())
        self.assertIn("development-only",error_output.getvalue())

    def test_production_backup_cli_requires_explicit_persistent_configuration(self):
        error_output=io.StringIO()
        with patch.dict(os.environ,{"APP_ENV":"production","PRIVATE_BETA":"1","EXPENSE_DB_PATH":"","PERSISTENT_DATA_DIR":""}), patch.object(app,"create_backup") as backup, contextlib.redirect_stderr(error_output):
            self.assertEqual(app.main(["backup"]),1)
            backup.assert_not_called()
        self.assertIn("requires EXPENSE_DB_PATH",error_output.getvalue())

    def test_private_beta_configuration_is_strict_in_production_and_open_by_default_in_development(self):
        with patch.dict(os.environ,{"APP_ENV":"development"},clear=False):
            os.environ.pop("PRIVATE_BETA",None)
            self.assertFalse(app.private_beta_enabled())
        with patch.dict(os.environ,{"APP_ENV":"production"},clear=False):
            os.environ.pop("PRIVATE_BETA",None)
            with self.assertRaisesRegex(ValueError,"requires PRIVATE_BETA"):
                app.private_beta_enabled(strict=True)
            os.environ["PRIVATE_BETA"]="yes"
            with self.assertRaisesRegex(ValueError,"must be either 0 or 1"):
                app.private_beta_enabled(strict=True)
            os.environ["PRIVATE_BETA"]="1"
            self.assertTrue(app.private_beta_enabled(strict=True))
        persistent=Path(self.temp.name)/"beta-production"
        persistent.mkdir()
        configured={"APP_ENV":"production","EXPENSE_DB_PATH":str(persistent/"expenses.sqlite3"),"EXPENSE_BACKUP_DIR":str(persistent/"backups"),"PERSISTENT_DATA_DIR":str(persistent),"REQUIRE_PERSISTENT_MOUNT":"0","PORT":"10000"}
        with patch.dict(os.environ,configured,clear=False):
            os.environ.pop("PRIVATE_BETA",None)
            with self.assertRaisesRegex(ValueError,"requires PRIVATE_BETA"):
                app.validate_runtime_configuration()

    def test_invitation_hash_expiry_and_revocation(self):
        invitation_id,token=app.create_invitation(24)
        digest=app.hashlib.sha256(token.encode()).hexdigest()
        with app.connect() as db:
            row=db.execute("SELECT token_hash FROM invitations WHERE id=?",(invitation_id,)).fetchone()
            self.assertEqual(row["token_hash"],digest)
            self.assertNotEqual(row["token_hash"],token)
        self.assertTrue(app.revoke_invitation(invitation_id))
        self.assertFalse(app.revoke_invitation(invitation_id))
        with self.assertRaises(app.InvalidInvitation):
            app.create_user("revoked@example.com","long-test-pass",token,True)
        with patch.dict(os.environ,{"PRIVATE_BETA":"1","APP_ENV":"development"}):
            with self.assertRaises(app.InvalidInvitation):
                app.create_user("bypass@example.com","long-test-pass",None,False)

    def test_operator_invitation_cli_displays_token_once_and_revokes_by_id(self):
        output=io.StringIO()
        with patch.dict(os.environ,{"PRIVATE_BETA":"1","APP_ENV":"development"}),contextlib.redirect_stdout(output):
            self.assertEqual(app.main(["invite","create","--expires-hours","48"]),0)
        lines=output.getvalue().strip().splitlines()
        self.assertEqual(len(lines),2)
        invitation_id=int(lines[0].split()[1])
        token=lines[1]
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT token_hash FROM invitations WHERE id=?",(invitation_id,)).fetchone()[0],app.hashlib.sha256(token.encode()).hexdigest())
        with patch.dict(os.environ,{"PRIVATE_BETA":"1","APP_ENV":"development"}),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["invite","revoke",str(invitation_id)]),0)

    def test_production_database_path_requires_absolute_mounted_writable_directory(self):
        original_path,original_backup=app.DB_PATH,app.BACKUP_DIR
        try:
            with patch.dict(os.environ,{"APP_ENV":"production","PERSISTENT_DATA_DIR":self.temp.name,"REQUIRE_PERSISTENT_MOUNT":"0"},clear=False):
                os.environ.pop("EXPENSE_DB_PATH",None)
                with self.assertRaisesRegex(ValueError,"requires EXPENSE_DB_PATH"):
                    app.validate_runtime_configuration()
                with patch.dict(os.environ,{"EXPENSE_DB_PATH":"relative.sqlite3"}):
                    with self.assertRaisesRegex(ValueError,"absolute path"):
                        app.validate_runtime_configuration()
                missing=Path(self.temp.name)/"missing"/"expenses.sqlite3"
                with patch.dict(os.environ,{"EXPENSE_DB_PATH":str(missing)}):
                    with self.assertRaisesRegex(ValueError,"must already exist"):
                        app.validate_runtime_configuration()
                with patch.dict(os.environ,{"EXPENSE_DB_PATH":str(Path(self.temp.name)/"expenses.sqlite3"),"REQUIRE_PERSISTENT_MOUNT":"1"}):
                    with self.assertRaisesRegex(ValueError,"not detected as a mounted filesystem"):
                        app.validate_runtime_configuration()
                valid_db=Path(self.temp.name)/"persistent"/"expenses.sqlite3"
                valid_db.parent.mkdir()
                backup_dir=valid_db.parent/"backups"
                with patch.dict(os.environ,{"PRIVATE_BETA":"1","EXPENSE_DB_PATH":str(valid_db),"EXPENSE_BACKUP_DIR":str(backup_dir),"PERSISTENT_DATA_DIR":str(valid_db.parent),"REQUIRE_PERSISTENT_MOUNT":"0","PORT":"10000"}):
                    self.assertTrue(app.validate_runtime_configuration())
                    self.assertEqual(app.DB_PATH,valid_db)
                    self.assertEqual(app.BACKUP_DIR,backup_dir)
                    self.assertFalse(valid_db.exists())
                if os.name!="nt":
                    read_only=valid_db.parent/"read-only.sqlite3"
                    read_only.touch(); read_only.chmod(0o444)
                    try:
                        with patch.dict(os.environ,{"EXPENSE_DB_PATH":str(read_only),"PERSISTENT_DATA_DIR":str(valid_db.parent),"REQUIRE_PERSISTENT_MOUNT":"0"}):
                            with self.assertRaisesRegex(ValueError,"database file is not writable"):
                                app.validate_runtime_configuration()
                    finally:
                        read_only.chmod(0o600)
        finally:
            app.DB_PATH,app.BACKUP_DIR=original_path,original_backup

    def test_vercel_runtime_uses_temp_database_without_persistent_mount_requirements(self):
        original_path, original_backup = app.DB_PATH, app.BACKUP_DIR
        try:
            env = {
                "APP_ENV": "production",
                "VERCEL": "1",
                "PRIVATE_BETA": "0",
                "COOKIE_SECURE": "1",
                "PORT": "10000",
                "TRUST_PROXY": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("EXPENSE_DB_PATH", None)
                os.environ.pop("PERSISTENT_DATA_DIR", None)
                os.environ.pop("REQUIRE_PERSISTENT_MOUNT", None)
                self.assertTrue(app.validate_runtime_configuration())
                self.assertEqual(app.DB_PATH, Path("/tmp/expense-tracker.sqlite3"))
                self.assertEqual(app.BACKUP_DIR, Path("/tmp/expense-tracker-backups"))
        finally:
            app.DB_PATH, app.BACKUP_DIR = original_path, original_backup

    def test_supabase_vercel_pooler_url_takes_precedence_over_direct_database_url(self):
        pooler_url = "postgresql://pooler.example.invalid:6543/postgres"
        direct_url = "postgresql://direct.example.invalid:5432/postgres"
        with patch.dict(os.environ, {"POSTGRES_URL": pooler_url, "DATABASE_URL": direct_url}), \
             patch.object(app, "_DATABASE_URL", None), \
             patch("pg_compat.connect_pg", return_value=object()) as connect_pg:
            app.connect()
        connect_pg.assert_called_once_with(pooler_url)

        with patch.dict(os.environ, {"POSTGRES_URL": pooler_url, "APP_ENV": "production"}, clear=False):
            self.assertTrue(app.validate_runtime_configuration())

    def test_production_wsgi_entry_validates_then_initializes_database(self):
        persistent=Path(self.temp.name)/"mounted"
        persistent.mkdir()
        database=persistent/"expenses.sqlite3"
        environment={**os.environ,"APP_ENV":"production","PRIVATE_BETA":"1","EXPENSE_DB_PATH":str(database),"EXPENSE_BACKUP_DIR":str(persistent/"backups"),"PERSISTENT_DATA_DIR":str(persistent),"REQUIRE_PERSISTENT_MOUNT":"0","PORT":"10000","COOKIE_SECURE":"1","TRUST_PROXY":"0"}
        valid=subprocess.run([sys.executable,"-c","import wsgi; print(wsgi.app.test_client().get('/healthz').status_code)"],cwd=app.ROOT,env=environment,capture_output=True,text=True,timeout=15)
        self.assertEqual(valid.returncode,0,valid.stderr)
        self.assertEqual(valid.stdout.strip(),"200")
        self.assertTrue(database.is_file())
        self.assertTrue((persistent/".expenses.sqlite3.initialized").is_file())
        invalid_env={key:value for key,value in environment.items() if key!="EXPENSE_DB_PATH"}
        invalid=subprocess.run([sys.executable,"-c","import wsgi"],cwd=app.ROOT,env=invalid_env,capture_output=True,text=True,timeout=15)
        self.assertNotEqual(invalid.returncode,0)
        self.assertIn("requires EXPENSE_DB_PATH",invalid.stderr)

    def test_production_refuses_to_bootstrap_after_initialized_database_disappears(self):
        persistent=Path(self.temp.name)/"initialized"
        persistent.mkdir()
        database=persistent/"expenses.sqlite3"
        marker=persistent/".expenses.sqlite3.initialized"
        marker.write_text("expense-db-initialized-v1\n")
        environment={"APP_ENV":"production","PRIVATE_BETA":"1","EXPENSE_DB_PATH":str(database),"EXPENSE_BACKUP_DIR":str(persistent/"backups"),"PERSISTENT_DATA_DIR":str(persistent),"REQUIRE_PERSISTENT_MOUNT":"0","PORT":"10000"}
        with patch.dict(os.environ,environment,clear=False):
            with self.assertRaisesRegex(ValueError,"Previously initialized production database is missing"):
                app.validate_runtime_configuration()
        self.assertFalse(database.exists())

    def test_money_parsing_and_month_boundaries(self):
        self.assertEqual(app.money_to_minor("1.234,56"),123456)
        self.assertEqual(app.money_to_minor("1234.56"),123456)
        self.assertEqual(app.money_to_minor("1,20"),120)
        self.assertEqual(app.month_bounds("2024-02"),("2024-02-01","2024-03-01"))
        self.assertEqual(app.month_bounds("2024-12"),("2024-12-01","2025-01-01"))
        with self.assertRaises(ValueError):app.month_bounds("2024-13")
        for malformed in ("2024-2", "2024-02-01", "24-02", "0000-01"):
            with self.assertRaises(ValueError):app.month_bounds(malformed)
        with self.assertRaises(ValueError):app.money_to_minor("999999999999999999999999999")
        self.assertEqual(app.summary(self.user_a,"0001-01")["previous_minor"],0)

    def test_expense_crud_budget_dashboard_and_isolation(self):
        category,payment=self.category_and_method(self.user_a)
        other_category,_=self.category_and_method(self.user_b)
        expense={"amount":"123,45","spent_on":"2024-02-29","description":"Market","category_id":category,"payment_method_id":payment}
        expense_id=app.save_expense(self.user_a,expense)
        app.save_expense(self.user_a,{**expense,"amount":"50.00","spent_on":"2024-01-31","description":"Prior month"})
        self.assertEqual(app.summary(self.user_a,"2024-02")["spent_minor"],12345)
        self.assertEqual(app.summary(self.user_a,"2024-03")["spent_minor"],0)
        self.assertEqual(app.summary(self.user_b,"2024-02")["spent_minor"],0)
        with self.assertRaises(sqlite3.IntegrityError):
            app.save_expense(self.user_a,{**expense,"category_id":other_category})
        self.assertEqual(app.save_expense(self.user_b,{**expense,"id":expense_id,"amount":"9"}),0)
        app.save_expense(self.user_a,{**expense,"id":expense_id,"amount":"200.00"})
        app.save_budget(self.user_a,"2024-02","500.00")
        app.save_budget(self.user_a,"2024-02","450.00")
        app.save_budget(self.user_a,"2024-02","300.00",category)
        snapshot=app.summary(self.user_a,"2024-02")
        self.assertEqual(snapshot["spent_minor"],20000)
        self.assertEqual(snapshot["budget_minor"],45000)
        self.assertEqual(snapshot["remaining_minor"],25000)
        self.assertEqual(snapshot["change_minor"],20000-5000)
        self.assertEqual(snapshot["categories"][0]["budget_minor"],30000)
        self.assertTrue(app.delete_expense(self.user_a,expense_id))
        self.assertFalse(app.delete_expense(self.user_b,expense_id))
        self.assertEqual(app.summary(self.user_a,"2024-02")["spent_minor"],0)

    def test_zero_budget_and_uncategorized_expenses_remain_visible(self):
        app.save_budget(self.user_a,"2024-02","0")
        app.save_expense(self.user_a,{"amount":"25.00","spent_on":"2024-02-01","description":"Uncategorized"})
        snapshot=app.summary(self.user_a,"2024-02")
        self.assertEqual(snapshot["budget_minor"],0)
        self.assertEqual(snapshot["remaining_minor"],-2500)
        self.assertEqual(snapshot["percent"],100)
        self.assertEqual(snapshot["categories"][0]["name"],"Kategorisiz")
        self.assertEqual(sum(row["amount_minor"] for row in snapshot["categories"]),snapshot["spent_minor"])
        app.save_budget(self.user_a,"2024-02","0",None)
        self.assertEqual(app.summary(self.user_a,"2024-02")["budget_minor"],0)

    def test_zero_category_budget_is_distinct_from_missing_budget(self):
        category,_=self.category_and_method(self.user_a)
        app.save_budget(self.user_a,"2024-02","0",category)
        app.save_expense(self.user_a,{"amount":"10.00","spent_on":"2024-02-10","description":"Expense","category_id":category})
        row=app.summary(self.user_a,"2024-02")["categories"][0]
        self.assertEqual(row["budget_minor"],0)

    def test_monthly_total_cap_prevents_unsafe_browser_amounts(self):
        maximum_amount=f"{app.MAX_MINOR//100}"
        app.save_expense(self.user_a,{"amount":maximum_amount,"spent_on":"2024-02-01","description":"Upper-bound expense"})
        with self.assertRaises(ValueError):
            app.save_expense(self.user_a,{"amount":"0.01","spent_on":"2024-02-02","description":"Over-bound expense"})

    def test_budget_legacy_duplicates_are_repaired_to_last_saved_value(self):
        with app.connect() as db:
            db.execute("DROP INDEX budget_overall_unique")
            db.execute("INSERT INTO budgets(user_id,month,category_id,amount_minor) VALUES(?,?,NULL,?)",(self.user_a,"2024-02",10000))
            db.execute("INSERT INTO budgets(user_id,month,category_id,amount_minor) VALUES(?,?,NULL,?)",(self.user_a,"2024-02",20000))
        app.init_db()
        with app.connect() as db:
            rows=db.execute("SELECT amount_minor FROM budgets WHERE user_id=? AND month=? AND category_id IS NULL",(self.user_a,"2024-02")).fetchall()
        self.assertEqual([row["amount_minor"] for row in rows],[20000])
        app.save_budget(self.user_a,"2024-02","300.00")
        self.assertEqual(app.summary(self.user_a,"2024-02")["budget_minor"],30000)

    def test_invalid_future_expense_and_foreign_category_budget_are_rejected(self):
        category_a,_=self.category_and_method(self.user_a)
        category_b,_=self.category_and_method(self.user_b)
        with app.connect() as db:db.execute("INSERT INTO categories(user_id,name) VALUES(?,?)",(self.user_a,"Yalnızca A"))
        tomorrow=(date.today()+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):app.save_expense(self.user_a,{"amount":"1.00","spent_on":tomorrow,"description":"Future","category_id":category_a})
        with self.assertRaises(sqlite3.IntegrityError):app.save_budget(self.user_a,"2024-02","10",category_b)
        with self.assertRaises(ValueError):app.save_expense(self.user_a,{"amount":"1.00","spent_on":"2024-02-01","description":"Currency","currency":"USD"})

    def test_password_hashing_and_session_user_scope(self):
        self.assertEqual(app.authenticate("A@example.com","long-test-pass"),self.user_a)
        self.assertIsNone(app.authenticate("a@example.com","wrong-password"))
        token,csrf=app.create_session(self.user_a)
        session=app.session_user(token)
        self.assertEqual(session["user_id"],self.user_a)
        self.assertEqual(session["csrf_token"],csrf)
        self.assertIsNone(app.session_user("unknown"))
        second_token,_=app.create_session(self.user_a)
        app.revoke_session(token)
        self.assertIsNone(app.session_user(token))
        self.assertEqual(app.session_user(second_token)["user_id"],self.user_a)
        with app.connect() as db:
            db.execute("UPDATE sessions SET expires_at=datetime('now','-1 second') WHERE token_hash=?",(app.hashlib.sha256(second_token.encode()).hexdigest(),))
        self.assertIsNone(app.session_user(second_token))

    def test_unknown_and_known_failed_logins_both_run_password_hashing(self):
        with patch.object(app,"password_digest",wraps=app.password_digest) as digest:
            self.assertIsNone(app.authenticate("missing@example.invalid","wrong-password"))
            digest.assert_called_once()
            digest.reset_mock()
            self.assertIsNone(app.authenticate("a@example.com","wrong-password"))
            digest.assert_called_once()

    def test_database_file_is_private_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits are not available on Windows")
        self.assertEqual(stat.S_IMODE(os.stat(app.DB_PATH).st_mode),0o600)

    def test_normal_database_connections_do_not_recreate_a_missing_active_database(self):
        app.DB_PATH.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            app.connect()
        self.assertFalse(app.DB_PATH.exists())

    def test_csv_turkish_formats_validation_duplicates_and_restart_persistence(self):
        csv_text="Tarih;Tutar;Açıklama;Kategori;Ödeme yöntemi;Not\n31.01.2024;1.234,56;Kışlık mont;Yemek;Nakit;İlk kayıt\n2024-02-01;bad;Eksik tutar;Yemek;Nakit;\n2024-02-02;10.5;Geçerli;Bilinmeyen;Nakit;\n"
        preview=app.parse_expense_csv(self.user_a,csv_text)
        self.assertEqual(preview[0]["amount_minor"],123456)
        self.assertEqual(preview[0]["date"],"2024-01-31")
        self.assertFalse(preview[0]["errors"])
        self.assertEqual(len(preview[1]["errors"]),1)
        self.assertTrue(any("Kategori" in error for error in preview[2]["errors"]))
        self.assertEqual(app.import_csv_rows(self.user_a,csv_text,[2]),{"imported":1,"invalid_rows":[3,4],"duplicate_rows":[]})
        repeated=app.parse_expense_csv(self.user_a,csv_text)
        self.assertTrue(repeated[0]["duplicate"])
        with self.assertRaises(ValueError):app.parse_expense_csv(self.user_a,"Tarih;Tutar\n")
        # A fresh database connection after schema initialization sees persisted expense, category and budget records.
        app.save_budget(self.user_a,"2024-01","2000")
        app.init_db()
        self.assertEqual(app.summary(self.user_a,"2024-01")["spent_minor"],123456)
        self.assertEqual(app.summary(self.user_a,"2024-01")["budget_minor"],200000)
        self.assertIn("Yemek",[c["name"] for c in app.user_options(self.user_a)["categories"]])

    def test_csv_export_and_import_only_use_authenticated_users_records(self):
        category_a,_=self.category_and_method(self.user_a)
        category_b,_=self.category_and_method(self.user_b)
        app.save_expense(self.user_a,{"amount":"12,50","spent_on":"2024-03-02","description":"Kendi harcamam","category_id":category_a})
        app.save_expense(self.user_a,{"amount":"1","spent_on":"2024-03-03","description":"=HYPERLINK(\"https://example.invalid\")","category_id":category_a})
        app.save_expense(self.user_b,{"amount":"90","spent_on":"2024-03-02","description":"Diğer hesap","category_id":category_b})
        exported=app.export_csv(self.user_a)
        self.assertIn("Kendi harcamam",exported)
        self.assertIn("'=HYPERLINK",exported)
        self.assertNotIn("Diğer hesap",exported)
        preview=app.parse_expense_csv(self.user_b,"Tarih;Tutar;Açıklama;Kategori\n03.03.2024;3,00;Yetkisiz kategori;Yalnızca A\n")
        self.assertTrue(any("Kategori" in e for e in preview[0]["errors"]))
        with self.assertRaises(ValueError):app.parse_expense_csv(self.user_a,"Tarih;Tutar;Açıklama\n01.01.2024;"+"1"*1_000_001+";Büyük\n")

    def test_recurrence_frequencies_month_end_and_leap_years(self):
        monthly=date(2024,1,31)
        self.assertEqual(app.recurrence_date(monthly,"monthly",1),date(2024,2,29))
        self.assertEqual(app.recurrence_date(monthly,"monthly",2),date(2024,3,31))
        self.assertEqual(app.recurrence_date(monthly,"monthly",3),date(2024,4,30))
        leap=date(2024,2,29)
        self.assertEqual(app.recurrence_date(leap,"yearly",1),date(2025,2,28))
        self.assertEqual(app.recurrence_date(leap,"yearly",4),date(2028,2,29))
        self.assertEqual(app.recurrence_date(date(2024,1,1),"weekly",2),date(2024,1,15))
        self.assertEqual(app.recurrence_index_on_or_after(monthly,"monthly",date(2024,2,1)),1)
        self.assertEqual(app.recurrence_index_on_or_after(monthly,"monthly",date(2024,2,29)),1)

    def test_credit_card_cycle_closing_day_short_month_and_year_boundary(self):
        self.assertEqual(app.card_cycle(31,20,date(2024,2,29)),(date(2024,2,1),date(2024,2,29),date(2024,3,20)))
        self.assertEqual(app.card_cycle(15,25,date(2024,12,15)),(date(2024,11,16),date(2024,12,15),date(2024,12,25)))
        self.assertEqual(app.card_cycle(15,25,date(2024,12,16)),(date(2024,12,16),date(2025,1,15),date(2025,1,25)))
        self.assertEqual([app._month_close(2024,month,31) for month in (1,2,3)], [date(2024,1,31),date(2024,2,29),date(2024,3,31)])

    def test_card_due_day_pair_uses_same_or_following_month_and_clamps_short_months(self):
        for due_day in (0,32):
            with self.assertRaises(ValueError):
                app.save_credit_card(self.user_a,{"bank_name":"Synthetic","display_name":"Invalid day","closing_day":28,"due_day":due_day})
        self.assertEqual(app.card_cycle(28,17,date(2024,10,28)),(date(2024,9,29),date(2024,10,28),date(2024,11,17)))
        self.assertEqual(app.card_cycle(5,25,date(2024,10,5)),(date(2024,9,6),date(2024,10,5),date(2024,10,25)))
        self.assertEqual(app.card_cycle(31,20,date(2024,1,31)),(date(2024,1,1),date(2024,1,31),date(2024,2,20)))
        self.assertEqual(app.card_cycle(31,20,date(2024,2,29)),(date(2024,2,1),date(2024,2,29),date(2024,3,20)))
        self.assertEqual(app.card_cycle(5,31,date(2024,4,5)),(date(2024,3,6),date(2024,4,5),date(2024,4,30)))
        self.assertEqual(app.card_cycle(31,30,date(2023,12,31)),(date(2023,12,1),date(2023,12,31),date(2024,1,30)))
        # Configured due dates must remain after statement close, including
        # equal day values and dates clamped onto the same day in short months.
        self.assertEqual(app.statement_due_date(31,31,date(2024,2,29)),date(2024,3,31))
        self.assertEqual(app.statement_due_date(30,31,date(2024,2,29)),date(2024,3,31))

    def test_due_day_card_assignment_calendar_forecast_and_historical_edits(self):
        card=app.save_credit_card(self.user_a,{"bank_name":"Synthetic Bank","display_name":"Due Day Test","closing_day":28,"due_day":17})
        purchase=app.save_expense(self.user_a,{"amount":"80","spent_on":"2024-10-28","description":"Due day purchase","card_id":card})
        with app.connect() as db:
            statement_id=db.execute("SELECT statement_id FROM expenses WHERE id=? AND user_id=?",(purchase,self.user_a)).fetchone()[0]
        self.assertTrue(app.edit_statement_dates(self.user_a,statement_id,"2024-10-29","2024-11-19"))
        card_data={"id":card,"bank_name":"Synthetic Bank","display_name":"Due Day Test","closing_day":5,"due_day":25}
        self.assertEqual(app.save_credit_card(self.user_a,card_data),card)
        statement=next(row for row in app.list_card_statements(self.user_a,card) if row["id"]==statement_id)
        self.assertEqual((statement["closing_on"],statement["due_on"]),("2024-10-29","2024-11-19"))
        calendar_events=app.payment_calendar(self.user_a,"2024-11-01","2024-12-01",date(2024,11,1))
        card_event=next(event for event in calendar_events if event["source_type"]=="card_statement")
        self.assertEqual(card_event["date"],"2024-11-19")
        app.save_opening_cash(self.user_a,"1000","2024-10-28")
        flow=app.cash_flow_forecast(self.user_a,"2024-11-01","2024-11-30",date(2024,11,1))
        projected_statement=next(event for event in flow["projection_events"] if event["event_type"]=="card_statement")
        self.assertEqual(projected_statement["date"],"2024-11-19")

    def test_due_day_additive_migration_preserves_card_history_and_manual_statement_dates(self):
        card=app.save_credit_card(self.user_a,{"bank_name":"Legacy Bank","display_name":"Legacy Cycle","closing_day":28,"due_day":17})
        purchase=app.save_expense(self.user_a,{"amount":"25","spent_on":"2024-10-28","description":"Legacy card purchase","card_id":card})
        with app.connect() as db:
            row=db.execute("SELECT statement_id,due_days_after_close FROM expenses JOIN credit_cards ON credit_cards.id=expenses.card_id WHERE expenses.id=?",(purchase,)).fetchone()
            statement_id=row["statement_id"]
            legacy_due_day=app._legacy_due_day(28,row["due_days_after_close"])
        app.edit_statement_dates(self.user_a,statement_id,"2024-10-29","2024-11-21")
        app.record_card_repayment(self.user_a,card,{"amount":"5","paid_on":"2024-11-10","statement_id":statement_id})
        with app.connect() as db:
            db.execute("ALTER TABLE credit_cards DROP COLUMN due_day")
        app.init_db()
        with app.connect() as db:
            migrated=db.execute("SELECT due_day FROM credit_cards WHERE id=? AND user_id=?",(card,self.user_a)).fetchone()
            statement=db.execute("SELECT closing_on,due_on FROM card_statements WHERE id=? AND user_id=?",(statement_id,self.user_a)).fetchone()
            repayment=db.execute("SELECT amount_minor FROM card_repayments WHERE card_id=? AND user_id=?",(card,self.user_a)).fetchone()
            linked_expense=db.execute("SELECT statement_id FROM expenses WHERE id=? AND user_id=?",(purchase,self.user_a)).fetchone()
        self.assertEqual(migrated["due_day"],legacy_due_day)
        self.assertEqual(tuple(statement),("2024-10-29","2024-11-21"))
        self.assertEqual(repayment["amount_minor"],500)
        self.assertEqual(linked_expense["statement_id"],statement_id)
        app.validate_database(app.DB_PATH)

    def test_card_rule_edit_recalculates_only_future_unpaid_unadjusted_statements(self):
        card=app.save_credit_card(self.user_a,{"bank_name":"Synthetic Bank","display_name":"Editable Dates","closing_day":28,"due_day":17})
        today=date.today()
        starts=[(today+timedelta(days=1)).isoformat(),(today+timedelta(days=2)).isoformat(),(today+timedelta(days=3)).isoformat()]
        with app.connect() as db:
            auto_id=db.execute("INSERT INTO card_statements(user_id,card_id,period_start,closing_on,due_on) VALUES(?,?,?,?,?)",(self.user_a,card,starts[0],(today+timedelta(days=10)).isoformat(),(today+timedelta(days=30)).isoformat())).lastrowid
            manual_id=db.execute("INSERT INTO card_statements(user_id,card_id,period_start,closing_on,due_on,dates_manually_corrected) VALUES(?,?,?,?,?,1)",(self.user_a,card,starts[1],(today+timedelta(days=11)).isoformat(),(today+timedelta(days=31)).isoformat())).lastrowid
            repaid_id=db.execute("INSERT INTO card_statements(user_id,card_id,period_start,closing_on,due_on) VALUES(?,?,?,?,?)",(self.user_a,card,starts[2],(today+timedelta(days=12)).isoformat(),(today+timedelta(days=32)).isoformat())).lastrowid
            db.execute("INSERT INTO card_repayments(user_id,card_id,statement_id,amount_minor,paid_on) VALUES(?,?,?,?,?)",(self.user_a,card,repaid_id,100,today.isoformat()))
        app.save_credit_card(self.user_a,{"id":card,"bank_name":"Synthetic Bank","display_name":"Editable Dates","closing_day":5,"due_day":25})
        with app.connect() as db:
            auto=db.execute("SELECT period_start,closing_on,due_on FROM card_statements WHERE id=?",(auto_id,)).fetchone()
            manual=db.execute("SELECT closing_on,due_on FROM card_statements WHERE id=?",(manual_id,)).fetchone()
            repaid=db.execute("SELECT closing_on,due_on FROM card_statements WHERE id=?",(repaid_id,)).fetchone()
        expected_close=app.card_closing_on_or_after(date.fromisoformat(starts[0]),5)
        expected_due=app.statement_due_date(5,25,expected_close)
        self.assertEqual((auto["closing_on"],auto["due_on"]),(expected_close.isoformat(),expected_due.isoformat()))
        self.assertEqual(tuple(manual),( (today+timedelta(days=11)).isoformat(),(today+timedelta(days=31)).isoformat()))
        self.assertEqual(tuple(repaid),( (today+timedelta(days=12)).isoformat(),(today+timedelta(days=32)).isoformat()))

    def test_credit_card_ownership_statement_repayments_and_installment_rounding(self):
        card=app.save_credit_card(self.user_a,{"bank_name":"Synthetic Bank","display_name":"Everyday","last_four":"1234","closing_day":31,"due_day":20,"credit_limit":"5000","opening_balance":"10"})
        self.assertEqual(app.save_credit_card(self.user_b,{"id":card,"bank_name":"X","display_name":"X","closing_day":1,"due_day":1}),0)
        category,method=self.category_and_method(self.user_a)
        today=date.today()
        with app.connect() as db: before=db.execute("SELECT count(*) FROM expenses WHERE user_id=?",(self.user_a,)).fetchone()[0]
        with self.assertRaises(ValueError):
            app.save_expense(self.user_a,{"amount":"10","spent_on":today.isoformat(),"description":"Invalid plan","card_id":card,"installments_count":3,"first_installment_on":(today-timedelta(days=1)).isoformat()})
        with app.connect() as db: self.assertEqual(db.execute("SELECT count(*) FROM expenses WHERE user_id=?",(self.user_a,)).fetchone()[0],before)
        purchase=app.save_expense(self.user_a,{"amount":"10.00","spent_on":today.isoformat(),"description":"Card purchase","category_id":category,"payment_method_id":method,"card_id":card})
        with app.connect() as db:
            card_method=db.execute("SELECT id FROM payment_methods WHERE user_id=? AND name='Kredi kartı'",(self.user_a,)).fetchone()[0]
            self.assertEqual(db.execute("SELECT payment_method_id FROM expenses WHERE id=?",(purchase,)).fetchone()[0],card_method)
        expense=app.save_expense(self.user_a,{"amount":"10.00","spent_on":today.isoformat(),"description":"Installment purchase","category_id":category,"card_id":card})
        plan=app.create_installment_plan(self.user_a,expense,3,today.isoformat())
        self.assertTrue(plan)
        occurrences=app.list_installments(self.user_a,card)
        plan_items=[row for row in occurrences if row["expense_id"]==expense]
        self.assertEqual([row["amount_minor"] for row in plan_items],[334,333,333])
        self.assertEqual(sum(row["amount_minor"] for row in plan_items),1000)
        self.assertEqual(app.create_installment_plan(self.user_a,expense,3,today.isoformat()),0)
        summary=app.summary(self.user_a,today.strftime("%Y-%m"))
        self.assertEqual(summary["spent_minor"],2000)
        self.assertEqual(app.list_credit_cards(self.user_a)[0]["outstanding_minor"],3000)
        self.assertEqual(app.record_card_repayment(self.user_a,card,{"amount":"5","paid_on":today.isoformat()} )>0,True)
        self.assertEqual(app.list_credit_cards(self.user_a)[0]["outstanding_minor"],2500)
        with self.assertRaises(ValueError): app.delete_expense(self.user_a,purchase)
        self.assertTrue(app.set_installment_status(self.user_a,plan_items[0]["id"],"paid",today.isoformat()))
        self.assertTrue(app.set_installment_status(self.user_a,plan_items[0]["id"],"paid",today.isoformat()))
        self.assertEqual(app.list_credit_cards(self.user_a)[0]["outstanding_minor"],2166)
        self.assertEqual(app.list_credit_cards(self.user_b),[])
        self.assertEqual(app.record_card_repayment(self.user_b,card,{"amount":"1"}),0)
        statements=app.list_card_statements(self.user_a,card)
        with app.connect() as db: statement_id=db.execute("SELECT statement_id FROM expenses WHERE id=?",(purchase,)).fetchone()[0]
        self.assertEqual(next(row for row in statements if row["id"]==statement_id)["total_minor"],1334)
        self.assertEqual(app.summary(self.user_a,today.strftime("%Y-%m"))["spent_minor"],2000)
        self.assertTrue(app.edit_statement_dates(self.user_a,statement_id,(today-timedelta(days=1)).isoformat(),(today+timedelta(days=20)).isoformat()))
        corrected=next(row for row in app.list_card_statements(self.user_a,card) if row["id"]==statement_id)
        self.assertEqual(corrected["total_minor"],1334)
        card_data={"id":card,"bank_name":"Synthetic Bank","display_name":"Everyday","closing_day":31,"due_day":20,"active":0}
        self.assertEqual(app.save_credit_card(self.user_a,card_data),card)
        card_data.pop("active")
        card_data["nickname"]="Updated"
        app.save_credit_card(self.user_a,card_data)
        self.assertEqual(app.list_credit_cards(self.user_a)[0]["active"],0)

    def test_card_schema_migration_preserves_existing_expenses(self):
        category,_=self.category_and_method(self.user_a)
        saved=app.save_expense(self.user_a,{"amount":"5","spent_on":date.today().isoformat(),"description":"Preserved legacy row","category_id":category})
        with app.connect() as db:
            db.execute("ALTER TABLE expenses DROP COLUMN installments_count")
            db.execute("ALTER TABLE expenses DROP COLUMN statement_id")
            db.execute("ALTER TABLE expenses DROP COLUMN card_id")
        app.init_db()
        with app.connect() as db:
            row=db.execute("SELECT id,card_id,statement_id,installments_count FROM expenses WHERE id=? AND user_id=?",(saved,self.user_a)).fetchone()
        self.assertEqual(tuple(row),(saved,None,None,1))

    def test_restore_migrates_previous_schema_backup_without_losing_expenses(self):
        category,_=self.category_and_method(self.user_a)
        saved=app.save_expense(self.user_a,{"amount":"9.25","spent_on":date.today().isoformat(),"description":"Legacy backup expense","category_id":category})
        app.BACKUP_DIR.mkdir(parents=True)
        legacy=app.BACKUP_DIR/"legacy-pre-card.sqlite3"
        app._copy_database_snapshot(app.DB_PATH,legacy)
        with sqlite3.connect(legacy) as db:
            for table in ("installment_occurrences","installment_plans","card_repayments","card_statements","credit_cards"):
                db.execute(f'DROP TABLE "{table}"')
            for column in ("installments_count","statement_id","card_id"):
                db.execute(f'ALTER TABLE expenses DROP COLUMN "{column}"')
        with app.connect() as db:
            db.execute("DELETE FROM expenses WHERE id=?",(saved,))
        result=app.restore_backup(legacy)
        self.assertTrue(result["recovery_backup"])
        self.assertEqual(app.summary(self.user_a,date.today().strftime("%Y-%m"))["spent_minor"],925)
        app.validate_database(app.DB_PATH)

    def test_income_recurrence_supports_weekly_month_end_last_day_salary_and_leap_years(self):
        start=date(2024,1,31)
        self.assertEqual(app.income_recurrence_date(start,"daily",4),date(2024,2,4))
        self.assertEqual(app.income_recurrence_index(start,"daily",date(2024,2,4)),4)
        self.assertEqual(app.income_recurrence_date(start,"weekly",1),date(2024,2,7))
        self.assertEqual(app.income_recurrence_date(start,"monthly",1),date(2024,2,29))
        self.assertEqual(app.income_recurrence_date(start,"monthly",2),date(2024,3,31))
        self.assertEqual(app.income_recurrence_date(date(2024,2,29),"yearly",1),date(2025,2,28))
        self.assertEqual(app.income_recurrence_date(date(2024,2,29),"yearly",4),date(2028,2,29))
        self.assertEqual(app.income_recurrence_date(date(2024,2,15),"monthly",0,"last_day"),date(2024,2,29))
        self.assertEqual(app.income_recurrence_date(date(2024,2,15),"monthly",10,"last_day"),date(2024,12,31))

    def test_income_daily_frequency_migration_preserves_existing_rows_and_foreign_keys(self):
        original=app.DB_PATH
        legacy=Path(self.temp.name)/"legacy-income.sqlite3"
        app.DB_PATH=legacy
        try:
            db=sqlite3.connect(legacy)
            db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT UNIQUE,password_hash BLOB,password_salt BLOB,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO users(id,email,password_hash,password_salt) VALUES(1,'migration@example.invalid',X'01',X'02');
            CREATE TABLE income_rules(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,income_type TEXT NOT NULL,description TEXT NOT NULL,frequency TEXT NOT NULL CHECK(frequency IN ('weekly','monthly','yearly')),schedule_mode TEXT NOT NULL DEFAULT 'anchored',start_on TEXT NOT NULL,end_on TEXT,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(id,user_id));
            CREATE TABLE income_rule_amounts(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,rule_id INTEGER NOT NULL,effective_on TEXT NOT NULL,amount_minor INTEGER NOT NULL,UNIQUE(rule_id,effective_on),FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE);
            CREATE TABLE income_records(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,rule_id INTEGER,income_type TEXT NOT NULL,description TEXT NOT NULL,amount_minor INTEGER NOT NULL,currency TEXT NOT NULL DEFAULT 'TRY',expected_on TEXT NOT NULL,received_on TEXT,status TEXT NOT NULL DEFAULT 'expected',note TEXT NOT NULL DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(rule_id,expected_on),FOREIGN KEY(rule_id,user…4741 tokens truncated…ect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM expenses WHERE user_id=?",(self.user_a,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recurring_occurrences WHERE recurring_id=?",(rule,)).fetchone()[0],1)

    def test_budget_warnings_and_month_end_estimate_data_thresholds(self):
        category,_=self.category_and_method(self.user_a)
        app.save_budget(self.user_a,"2024-04","10",category)
        app.save_expense(self.user_a,{"amount":"9","spent_on":"2024-04-01","description":"Yakın bütçe","category_id":category})
        early=app.summary(self.user_a,"2024-04",date(2024,4,3))
        self.assertEqual(early["estimate_reason"],"insufficient_days")
        self.assertEqual(early["categories"][0]["budget_status"],"near")
        enough=app.summary(self.user_a,"2024-04",date(2024,4,10))
        self.assertEqual(enough["month_end_estimate_minor"],2_700)
        app.save_expense(self.user_a,{"amount":"2","spent_on":"2024-04-10","description":"Aşım","category_id":category})
        self.assertEqual(app.summary(self.user_a,"2024-04",date(2024,4,10))["categories"][0]["budget_status"],"over")
        self.assertEqual(app.summary(self.user_a,"2024-03",date(2024,4,10))["estimate_reason"],"not_current_month")

    def test_database_backup_and_restore_preserve_records_and_create_recovery_copy(self):
        category,payment=self.category_and_method(self.user_a)
        today=date.today().isoformat()
        app.save_expense(self.user_a,{"amount":"42.50","spent_on":today,"description":"Backup record","category_id":category,"payment_method_id":payment})
        app.save_budget(self.user_a,today[:7],"250")
        rule=app.save_recurring(self.user_a,{"description":"Backup recurring","amount":"15","category_id":category,"frequency":"monthly","start_on":today})
        app.complete_recurring_occurrence(self.user_a,rule,today)
        backup=app.create_backup()
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent,app.BACKUP_DIR.resolve())
        if os.name!="nt":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode),0o700)
        extra_user=app.create_user("restore-extra@example.invalid","long-test-pass")
        result=app.restore_backup(backup)
        self.assertEqual(result["restored_from"],backup)
        self.assertEqual(app.summary(self.user_a,today[:7])["spent_minor"],5750)
        self.assertIsNone(app.authenticate("restore-extra@example.invalid","long-test-pass"))
        self.assertTrue(app.authenticate("a@example.com","long-test-pass"))
        self.assertTrue(result["recovery_backup"].is_file())
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recurring_expenses WHERE user_id=?",(self.user_a,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recurring_occurrences WHERE user_id=?",(self.user_a,)).fetchone()[0],1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])
        recovery=app._readonly_connection(result["recovery_backup"])
        try:
            self.assertIsNotNone(recovery.execute("SELECT id FROM users WHERE id=?",(extra_user,)).fetchone())
        finally:
            recovery.close()

    def test_invalid_or_incompatible_backup_is_rejected_without_touching_live_database(self):
        app.save_expense(self.user_a,{"amount":"11","spent_on":date.today().isoformat(),"description":"Keep this record"})
        original_bytes=app.DB_PATH.read_bytes()
        app.BACKUP_DIR.mkdir(mode=0o700)
        corrupt=app.BACKUP_DIR/"corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(ValueError):app.restore_backup(corrupt)
        self.assertEqual(app.DB_PATH.read_bytes(),original_bytes)
        incompatible=app.BACKUP_DIR/"incompatible.sqlite3"
        with sqlite3.connect(incompatible) as db:db.execute("CREATE TABLE users(id INTEGER)")
        with self.assertRaises(ValueError):app.restore_backup(incompatible)
        self.assertEqual(app.summary(self.user_a,date.today().strftime("%Y-%m"))["spent_minor"],1100)
        with self.assertRaises(ValueError):app.restore_backup(Path(self.temp.name)/"outside.sqlite3")

    def test_encrypted_backup_restores_and_creates_recovery_backup(self):
        key = base64.urlsafe_b64encode(b"k" * 32).decode()
        with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY": key, "APP_ENV": "development"}):
            app.save_expense(self.user_a,{"amount":"23.45","spent_on":date.today().isoformat(),"description":"Encrypted source"})
            backup = app.create_backup(encrypted=True)
            self.assertTrue(backup.name.endswith(".sqlite3.enc"))
            self.assertTrue(backup.read_bytes().startswith(app.ENCRYPTED_BACKUP_MAGIC))
            app.save_expense(self.user_a,{"amount":"7","spent_on":date.today().isoformat(),"description":"Later record"})
            result = app.restore_backup(backup)
            self.assertEqual(app.summary(self.user_a,date.today().strftime("%Y-%m"))["spent_minor"],2345)
            self.assertTrue(result["recovery_backup"].name.endswith(".sqlite3.enc"))
            decrypted = Path(self.temp.name) / "recovery-validation.sqlite3"
            app._decrypt_file(result["recovery_backup"], decrypted, b"k" * 32)
            self.assertTrue(app.validate_database(decrypted))

    def test_encrypted_backup_restores_to_separate_database_and_cleans_staging_files(self):
        key = base64.urlsafe_b64encode(b"r" * 32).decode()
        temp_root = Path(tempfile.gettempdir())
        before = set(temp_root.glob("expense-private-*.sqlite3"))
        with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY": key, "APP_ENV": "development"}):
            app.save_expense(self.user_a,{"amount":"31.20","spent_on":date.today().isoformat(),"description":"Separate restore"})
            backup = app.create_backup(encrypted=True)
            alternate_db = Path(self.temp.name) / "separate" / "expenses.sqlite3"
            with patch.object(app, "DB_PATH", alternate_db):
                result = app.restore_backup(backup)
                self.assertIsNone(result["recovery_backup"])
                self.assertTrue(app.validate_database(alternate_db))
                self.assertEqual(app.summary(self.user_a,date.today().strftime("%Y-%m"))["spent_minor"],3120)
        self.assertEqual(set(temp_root.glob("expense-private-*.sqlite3")), before)

    def test_wrong_key_tamper_and_truncation_reject_encrypted_restore_unchanged(self):
        good_key = base64.urlsafe_b64encode(b"a" * 32).decode()
        bad_key = base64.urlsafe_b64encode(b"b" * 32).decode()
        temp_root = Path(tempfile.gettempdir())
        staging_before = set(temp_root.glob("expense-private-*.sqlite3"))
        with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY": good_key, "APP_ENV": "development"}):
            app.save_expense(self.user_a,{"amount":"19","spent_on":date.today().isoformat(),"description":"Protected current"})
            original = app.DB_PATH.read_bytes()
            backup = app.create_backup(encrypted=True)
            backup_bytes = backup.read_bytes()
            for damaged in (backup_bytes[:-3], backup_bytes[:-1] + bytes([backup_bytes[-1] ^ 1])):
                backup.write_bytes(damaged)
                with self.assertRaises(ValueError): app.restore_backup(backup)
                self.assertEqual(app.DB_PATH.read_bytes(), original)
            backup.write_bytes(backup_bytes)
            with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY": bad_key}):
                with self.assertRaises(ValueError): app.restore_backup(backup)
            self.assertEqual(app.DB_PATH.read_bytes(), original)
        self.assertEqual(set(temp_root.glob("expense-private-*.sqlite3")), staging_before)

    def test_incompatible_encrypted_database_is_rejected_before_live_replacement(self):
        key = base64.urlsafe_b64encode(b"z" * 32).decode()
        temp_root = Path(tempfile.gettempdir())
        staging_before = set(temp_root.glob("expense-private-*.sqlite3"))
        with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY":key, "APP_ENV":"development"}):
            app.save_expense(self.user_a,{"amount":"8","spent_on":date.today().isoformat(),"description":"Keep current"})
            original = app.DB_PATH.read_bytes()
            incompatible = Path(self.temp.name) / "incompatible.sqlite3"
            with sqlite3.connect(incompatible) as db:
                db.execute("CREATE TABLE users(id INTEGER)")
            app.BACKUP_DIR.mkdir(mode=0o700)
            encrypted = app.BACKUP_DIR / "incompatible.sqlite3.enc"
            app._encrypt_file(incompatible, encrypted, b"z" * 32)
            with self.assertRaises(ValueError): app.restore_backup(encrypted)
            self.assertEqual(app.DB_PATH.read_bytes(), original)
        self.assertEqual(set(temp_root.glob("expense-private-*.sqlite3")), staging_before)

    def test_encryption_key_is_required_and_production_rejects_plaintext_restore(self):
        app.save_expense(self.user_a,{"amount":"5","spent_on":date.today().isoformat(),"description":"Keep current"})
        original = app.DB_PATH.read_bytes()
        with patch.dict(os.environ, {"EXPENSE_BACKUP_KEY":"", "APP_ENV":"development"}):
            with self.assertRaises(ValueError): app.create_backup(encrypted=True)
        plain = app.create_backup()
        with patch.dict(os.environ, {"APP_ENV":"production"}):
            with self.assertRaises(ValueError): app.restore_backup(plain)
        self.assertEqual(app.DB_PATH.read_bytes(), original)

    def test_production_restore_command_rejects_plaintext_backup(self):
        app.save_expense(self.user_a,{"amount":"4","spent_on":date.today().isoformat(),"description":"Keep current"})
        original = app.DB_PATH.read_bytes()
        plain = app.create_backup()
        output = io.StringIO()
        with patch.dict(os.environ, {"APP_ENV":"production"}), patch.object(app,"validate_runtime_configuration"), contextlib.redirect_stderr(output):
            self.assertEqual(app.main(["restore", str(plain)]), 1)
        self.assertIn("encrypted backup", output.getvalue())
        self.assertEqual(app.DB_PATH.read_bytes(), original)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.original_path=app.DB_PATH
        app.DB_PATH=Path(cls.temp.name)/"http.sqlite3"
        app.init_db()
        cls.flask_app=webapp.create_app({"TESTING":True})
        private_error_message="private expense payload must not be logged"
        def fail_request():
            raise RuntimeError(private_error_message)
        cls.flask_app.add_url_rule("/test-internal-error", view_func=fail_request)
        cls.server=make_server("127.0.0.1",0,cls.flask_app,threaded=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.thread.join();cls.server.server_close()
        app.DB_PATH=cls.original_path
        cls.temp.cleanup()

    def setUp(self):
        with app.connect() as db:
            db.execute("DELETE FROM users")
            db.execute("DELETE FROM auth_failures")
        self.a=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.b=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf_a=self.register(self.a,"api-a@example.invalid")
        self.csrf_b=self.register(self.b,"api-b@example.invalid")

    def request(self,opener,path,method="GET",data=None,csrf=None):
        headers={};body=None
        if data is not None:
            body=json.dumps(data).encode();headers["Content-Type"]="application/json"
        if csrf is not None:headers["X-CSRF-Token"]=csrf
        req=Request(self.base+path,data=body,headers=headers,method=method)
        try:
            with opener.open(req) as response:return response.status,json.loads(response.read() or b"{}")
        except HTTPError as error:return error.code,json.loads(error.read() or b"{}")

    def register(self,opener,email):
        code,data=self.request(opener,"/api/register","POST",{"email":email,"password":"local-api-test-pass"})
        self.assertEqual(code,200)
        return data["csrf"]

    def test_private_beta_rejects_open_invalid_expired_reused_invites_and_exposes_no_secret_route(self):
        previous=self.flask_app.config["PRIVATE_BETA"]
        self.flask_app.config["PRIVATE_BETA"]=True
        client=self.flask_app.test_client()
        try:
            self.assertEqual(client.post("/api/register",json={"email":"open-beta@example.invalid","password":"local-api-test-pass"}).status_code,403)
            self.assertEqual(client.post("/api/register",json={"email":"bad-beta@example.invalid","password":"local-api-test-pass","invitation_token":"invalid-token-value-123456"}).status_code,403)
            expired_id,expired=app.create_invitation(1)
            with app.connect() as db:db.execute("UPDATE invitations SET expires_at=datetime('now','-1 hour') WHERE id=?",(expired_id,))
            self.assertEqual(client.post("/api/register",json={"email":"expired-beta@example.invalid","password":"local-api-test-pass","invitation_token":expired}).status_code,403)
            invitation_id,token=app.create_invitation(24)
            registered=client.post("/api/register",json={"email":"invited-beta@example.invalid","password":"local-api-test-pass","invitation_token":token})
            self.assertEqual(registered.status_code,200,registered.get_json())
            session=client.get("/api/session").get_json()
            self.assertTrue(session["private_beta"])
            self.assertNotIn(token,str(session))
            reused=client.post("/api/register",json={"email":"reuse-beta@example.invalid","password":"local-api-test-pass","invitation_token":token})
            self.assertEqual(reused.status_code,403)
            with app.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM users WHERE email='reuse-beta@example.invalid'").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT used_at IS NOT NULL FROM invitations WHERE id=?",(invitation_id,)).fetchone()[0],1)
            self.assertEqual(client.get("/api/invitations").status_code,404)
            self.assertEqual(client.get("/api/invite").status_code,404)
            self.assertNotIn(token,client.get("/api/session").get_data(as_text=True))
            self.assertEqual(client.get("/api/options").status_code,200)
        finally:
            self.flask_app.config["PRIVATE_BETA"]=previous

    def test_invitation_registration_is_user_isolated_and_keeps_csrf_protection(self):
        previous=self.flask_app.config["PRIVATE_BETA"]
        self.flask_app.config["PRIVATE_BETA"]=True
        client=self.flask_app.test_client()
        try:
            _,token=app.create_invitation(24)
            response=client.post("/api/register",json={"email":"invitation-owner@example.invalid","password":"local-api-test-pass","invitation_token":token})
            self.assertEqual(response.status_code,200)
            csrf=response.get_json()["csrf"]
            owner_id=response.get_json()["user_id"]
            other_id=app.create_user("invitation-other@example.invalid","local-api-test-pass")
            options=app.user_options(other_id)
            category=options["categories"][0]["id"]
            app.save_expense(other_id,{"amount":"42","spent_on":date.today().isoformat(),"description":"private other record","category_id":category})
            self.assertEqual(client.get("/api/expenses?month="+date.today().strftime("%Y-%m")).get_json(),[])
            self.assertEqual(client.post("/api/categories",json={"name":"CSRF blocked"}).status_code,403)
            self.assertIsNotNone(app.session_user(client.get_cookie(app.COOKIE).value))
            self.assertNotEqual(owner_id,other_id)
        finally:
            self.flask_app.config["PRIVATE_BETA"]=previous

    def test_api_data_scoping_validation_and_csrf(self):
        _,opts_a=self.request(self.a,"/api/options")
        _,opts_b=self.request(self.b,"/api/options")
        self.assertNotEqual([c["id"] for c in opts_a["categories"]],[c["id"] for c in opts_b["categories"]])
        category_a=opts_a["categories"][0]["id"]
        category_b=opts_b["categories"][0]["id"]
        payload={"amount":"35.75","spent_on":date.today().isoformat(),"description":"API isolation","category_id":category_a}
        self.assertEqual(self.request(self.a,"/api/expenses","POST",payload)[0],403)
        code,saved=self.request(self.a,"/api/expenses","POST",payload,self.csrf_a)
        self.assertEqual(code,200)
        self.assertEqual(self.request(self.b,"/api/expenses?month="+date.today().strftime("%Y-%m"))[1],[])
        self.assertEqual(self.request(self.b,f"/api/expenses","POST",{**payload,"id":saved["id"]},self.csrf_b)[0],404)
        self.assertEqual(self.request(self.b,f"/api/expenses/{saved['id']}","DELETE",{},self.csrf_b)[0],404)
        self.assertEqual(self.request(self.a,"/api/budgets","POST",{"month":date.today().strftime("%Y-%m"),"amount":"100","category_id":category_b},self.csrf_a)[0],400)
        self.assertEqual(self.request(self.a,"/api/budgets","POST",{"month":date.today().strftime("%Y-%m"),"amount":"0"},self.csrf_a)[0],200)
        summary_code,summary_data=self.request(self.a,"/api/summary?month="+date.today().strftime("%Y-%m"))
        self.assertEqual(summary_code,200)
        self.assertEqual(summary_data["remaining_minor"],-3575)
        self.assertEqual(self.request(self.b,"/api/budgets","POST",{"month":date.today().strftime("%Y-%m"),"amount":"100"},self.csrf_b)[0],200)
        self.assertEqual(self.request(self.b,"/api/summary?month="+date.today().strftime("%Y-%m"))[1]["budget_minor"],10000)
        self.assertEqual(self.request(self.a,"/api/summary?month="+date.today().strftime("%Y-%m"))[1]["budget_minor"],0)
        self.assertEqual(self.request(self.a,"/api/categories","POST",{"name":"Audit kategori"},self.csrf_a)[0],201)
        self.assertNotIn("Audit kategori",[category["name"] for category in self.request(self.b,"/api/options")[1]["categories"]])
        self.assertEqual(self.request(self.a,f"/api/expenses/{saved['id']}","DELETE",{},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.a,f"/api/expenses/{saved['id']}","DELETE",{},self.csrf_a)[0],404)

    def test_credit_card_api_requires_csrf_and_enforces_owner_scope(self):
        payload={"bank_name":"Synthetic","display_name":"Travel","last_four":"9876","closing_day":15,"due_day":20,"credit_limit":"1000"}
        self.assertEqual(self.request(self.a,"/api/cards","POST",payload)[0],403)
        code,card=self.request(self.a,"/api/cards","POST",payload,self.csrf_a)
        self.assertEqual(code,200)
        self.assertEqual(self.request(self.b,f"/api/cards/{card['id']}/statements")[0],404)
        self.assertEqual(self.request(self.a,"/api/expenses","POST",{"amount":"12","spent_on":date.today().isoformat(),"description":"Card expense","card_id":card["id"]},self.csrf_a)[0],200)
        plan_code,_=self.request(self.a,"/api/expenses","POST",{"amount":"10","spent_on":date.today().isoformat(),"description":"Card installments","card_id":card["id"],"installments_count":3,"first_installment_on":date.today().isoformat()},self.csrf_a)
        self.assertEqual(plan_code,200)
        self.assertEqual(len(self.request(self.a,"/api/installments")[1]),3)
        statements=self.request(self.a,f"/api/cards/{card['id']}/statements")[1]
        self.assertEqual(len(statements),3)
        self.assertEqual(self.request(self.b,f"/api/cards/{card['id']}/repayments","POST",{"amount":"1"},self.csrf_b)[0],404)
        self.assertEqual(self.request(self.a,f"/api/cards/{card['id']}/repayments","POST",{"amount":"2","paid_on":date.today().isoformat()},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.a,"/api/register","POST",[])[0],400)

    def test_login_rate_limit_is_per_source_and_successful_login_remains_available(self):
        for attempt in range(app.AUTH_FAILURE_LIMITS["login"]):
            code,_=self.request(self.a,"/api/login","POST",{"email":"unknown@example.invalid","password":"wrong"})
            self.assertEqual(code,401,attempt)
        code,unknown=self.request(self.a,"/api/login","POST",{"email":"unknown@example.invalid","password":"wrong"})
        self.assertEqual(code,429)
        self.assertIn("bir süre",unknown["error"])
        code,known_wrong=self.request(self.a,"/api/login","POST",{"email":"api-a@example.invalid","password":"wrong"})
        self.assertEqual(code,401)
        self.assertEqual(known_wrong["error"],"E-posta veya parola hatalı.")
        self.assertEqual(unknown["error"],"Çok sayıda deneme yapıldı. Lütfen bir süre sonra tekrar deneyin.")
        normal_login=self.request(self.a,"/api/login","POST",{"email":"api-a@example.invalid","password":"local-api-test-pass"})
        self.assertEqual(normal_login[0],200)
        other_client=self.flask_app.test_client()
        login=other_client.post("/api/login",json={"email":"api-a@example.invalid","password":"local-api-test-pass"},environ_base={"REMOTE_ADDR":"198.51.100.10"})
        self.assertEqual(login.status_code,200)
        self.assertTrue(login.get_json()["csrf"])

    def test_login_errors_do_not_disclose_whether_email_exists(self):
        unknown=self.request(self.a,"/api/login","POST",{"email":"missing@example.invalid","password":"wrong-password"})
        known=self.request(self.a,"/api/login","POST",{"email":"api-a@example.invalid","password":"wrong-password"})
        self.assertEqual(unknown[0],401)
        self.assertEqual(known[0],401)
        self.assertEqual(unknown[1]["error"],known[1]["error"])

    def test_registration_rate_limit_has_a_per_source_cap(self):
        for index in range(app.AUTH_SOURCE_LIMITS["register"]):
            code,_=self.request(self.a,"/api/register","POST",{"email":f"invalid-{index}","password":"wrong"})
            self.assertEqual(code,400,index)
        code,error=self.request(self.a,"/api/register","POST",{"email":"another-invalid","password":"wrong"})
        self.assertEqual(code,429)
        self.assertIn("bir süre",error["error"])

    def test_account_deletion_password_failures_are_rate_limited(self):
        for attempt in range(app.AUTH_FAILURE_LIMITS["account-delete"]):
            code,_=self.request(self.a,"/api/account/delete","POST",{"password":"wrong-password","confirmation":"SİL"},self.csrf_a)
            self.assertEqual(code,400,attempt)
        code,error=self.request(self.a,"/api/account/delete","POST",{"password":"wrong-password","confirmation":"SİL"},self.csrf_a)
        self.assertEqual(code,429)
        self.assertIn("bir süre",error["error"])

    def test_logout_revokes_only_current_session(self):
        second=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        _,login=self.request(second,"/api/login","POST",{"email":"api-a@example.invalid","password":"local-api-test-pass"})
        self.assertEqual(self.request(self.a,"/api/logout","POST",{},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.a,"/api/options")[0],401)
        self.assertEqual(self.request(second,"/api/options")[0],200)
        self.assertTrue(login["csrf"])

    def test_production_login_cookie_has_secure_attribute(self):
        request=Request(self.base+"/api/login",data=json.dumps({"email":"api-a@example.invalid","password":"local-api-test-pass"}).encode(),headers={"Content-Type":"application/json"},method="POST")
        with patch.dict(os.environ,{"APP_ENV":"production"}):
            with self.a.open(request) as response:
                cookie=response.headers.get("Set-Cookie","")
        self.assertIn("HttpOnly",cookie)
        self.assertIn("SameSite=Strict",cookie)
        self.assertIn("Secure",cookie)

    def test_health_check_is_non_sensitive_and_reports_database_unavailable(self):
        self.assertEqual(self.flask_app.test_client().get("/api/db-status").status_code, 404)
        code,data=self.request(self.a,"/healthz")
        self.assertEqual(code,200)
        self.assertEqual(data,{"status":"ok"})
        with patch.object(webapp.core,"connect",side_effect=sqlite3.DatabaseError("internal database detail")):
            unavailable=self.flask_app.test_client().get("/healthz")
        self.assertEqual(unavailable.status_code,503)
        self.assertNotIn("internal database detail",unavailable.get_data(as_text=True))
        missing=Path(self.temp.name)/"missing-health.sqlite3"
        with patch.object(webapp.core,"DB_PATH",missing):
            unavailable=self.flask_app.test_client().get("/healthz")
        self.assertEqual(unavailable.status_code,503)
        self.assertFalse(missing.exists())

    def test_internal_errors_are_generic_to_clients_and_sanitized_in_logs(self):
        with self.assertLogs(self.flask_app.logger, level="ERROR") as captured:
            response=self.flask_app.test_client().get("/test-internal-error")
        self.assertEqual(response.status_code,500)
        self.assertNotIn("private expense payload",response.get_data(as_text=True))
        self.assertIn("RuntimeError","\n".join(captured.output))
        self.assertNotIn("private expense payload","\n".join(captured.output))

    def test_render_proxy_rate_limit_uses_cf_client_ip_not_forwarded_for(self):
        with patch.dict(os.environ,{"TRUST_PROXY":"1"}):
            proxy_app=webapp.create_app({"TESTING":True})
        client=proxy_app.test_client()
        with patch.object(app,"reserve_auth_attempt",return_value=True) as reserve:
            response=client.post("/api/login",json={"email":"unknown@example.invalid","password":"wrong"},headers={"CF-Connecting-IP":"198.51.100.12","X-Forwarded-For":"203.0.113.8"},environ_base={"REMOTE_ADDR":"127.0.0.1"})
        self.assertEqual(response.status_code,429)
        self.assertEqual(reserve.call_args.args[0],"198.51.100.12")
        with patch.object(app,"reserve_auth_attempt",return_value=True) as reserve:
            client.post("/api/login",json={"email":"unknown@example.invalid","password":"wrong"},headers={"X-Forwarded-For":"203.0.113.8"},environ_base={"REMOTE_ADDR":"127.0.0.1"})
        self.assertEqual(reserve.call_args.args[0],"127.0.0.1")

    def test_csv_api_preview_import_export_and_user_isolation(self):
        _,options_a=self.request(self.a,"/api/options")
        category=options_a["categories"][0]["name"]
        csv_text=f"Tarih;Tutar;Açıklama;Kategori\n01.02.2024;2,50;CSV test;{category}\n01.02.2024;2,50;CSV test;{category}\n"
        code,preview=self.request(self.a,"/api/import/preview","POST",{"csv":csv_text},self.csrf_a)
        self.assertEqual(code,200)
        self.assertFalse(preview["rows"][0]["duplicate"])
        self.assertTrue(preview["rows"][1]["duplicate"])
        code,result=self.request(self.a,"/api/import","POST",{"csv":csv_text,"selected_rows":[2]},self.csrf_a)
        self.assertEqual(code,200)
        self.assertEqual(result["imported"],1)
        self.assertEqual(result["duplicate_rows"],[3])
        month="2024-02"
        self.assertEqual(len(self.request(self.a,f"/api/expenses?month={month}")[1]),1)
        self.assertEqual(self.request(self.b,f"/api/expenses?month={month}")[1],[])
        with self.a.open(self.base+"/api/expenses.csv") as response:
            own_export=response.read().decode("utf-8")
        with self.b.open(self.base+"/api/expenses.csv") as response:
            other_export=response.read().decode("utf-8")
        self.assertIn("CSV test",own_export)
        self.assertNotIn("CSV test",other_export)
        self.assertEqual(self.request(self.a,"/api/import/preview","POST",{"csv":csv_text})[0],403)

    def test_recurring_api_enforces_ownership_and_csrf_and_occurrence_is_idempotent(self):
        today=date.today().isoformat()
        payload={"description":"API recurring","amount":"30.00","frequency":"monthly","start_on":today}
        self.assertEqual(self.request(self.a,"/api/recurring","POST",payload)[0],403)
        code,created=self.request(self.a,"/api/recurring","POST",payload,self.csrf_a)
        self.assertEqual(code,200)
        recurring_id=created["id"]
        own=self.request(self.a,"/api/recurring")[1]
        self.assertEqual(len(own["rules"]),1)
        self.assertEqual(self.request(self.b,"/api/recurring")[1]["rules"],[])
        self.assertEqual(self.request(self.b,f"/api/recurring/{recurring_id}/status","POST",{"status":"cancelled"},self.csrf_b)[0],404)
        occurrence=f"/api/recurring/{recurring_id}/occurrences/{today}/complete"
        self.assertEqual(self.request(self.b,occurrence,"POST",{},self.csrf_b)[0],404)
        first_code,first=self.request(self.a,occurrence,"POST",{},self.csrf_a)
        second_code,second=self.request(self.a,occurrence,"POST",{},self.csrf_a)
        self.assertEqual((first_code,second_code),(200,200))
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["expense_id"],second["expense_id"])
        self.assertEqual(self.request(self.b,"/api/expenses?month="+today[:7])[1],[])

    def test_income_api_requires_csrf_and_isolates_records_rules_and_actions(self):
        today=date.today(); month=today.strftime("%Y-%m")
        payload={"description":"Synthetic refund","income_type":"refund","amount":"99,95","expected_on":(today+timedelta(days=2)).isoformat()}
        self.assertEqual(self.request(self.a,"/api/income","POST",payload)[0],403)
        status,created=self.request(self.a,"/api/income","POST",payload,self.csrf_a)
        self.assertEqual(status,200)
        income_id=created["id"]
        self.assertEqual(len(self.request(self.a,f"/api/income?month={month}")[1]["records"]),1)
        self.assertEqual(self.request(self.b,f"/api/income?month={month}")[1]["records"],[])
        self.assertEqual(self.request(self.b,f"/api/income/{income_id}/status","POST",{"status":"received"},self.csrf_b)[0],404)
        rule_payload={"description":"Synthetic salary","income_type":"salary","amount":"50000","frequency":"monthly","start_on":today.isoformat()}
        status,rule=self.request(self.a,"/api/income","POST",rule_payload,self.csrf_a)
        self.assertEqual(status,200)
        daily_payload={"description":"Daily test stipend","income_type":"additional","amount":"12.50","frequency":"daily","start_on":today.isoformat(),"end_on":today.isoformat()}
        daily_status,daily_rule=self.request(self.a,"/api/income","POST",daily_payload,self.csrf_a)
        self.assertEqual(daily_status,200)
        own=self.request(self.a,f"/api/income?month={month}")[1]
        self.assertTrue(any(row["rule_id"]==rule["id"] for row in own["records"]))
        self.assertTrue(any(row["rule_id"]==daily_rule["id"] for row in own["records"]))
        self.assertFalse(any(row["rule_id"]==daily_rule["id"] for row in self.request(self.b,f"/api/income?month={month}")[1]["records"]))
        self.assertEqual(self.request(self.b,f"/api/income-rules/{rule['id']}/status","POST",{"status":"cancelled"},self.csrf_b)[0],404)
        self.assertEqual(self.request(self.a,f"/api/income/{income_id}/status","POST",{"status":"received"},self.csrf_a)[0],200)
        summary=self.request(self.a,f"/api/summary?month={month}")[1]
        self.assertGreaterEqual(summary["income_received_minor"],9995)
        self.assertEqual(summary["spent_minor"],0)
        other={"description":"Other user's income","income_type":"other","amount":"4","expected_on":today.isoformat()}
        self.assertEqual(self.request(self.b,"/api/income","POST",other,self.csrf_b)[0],200)
        with self.a.open(self.base+"/api/income.csv") as response:
            own_csv=response.read().decode("utf-8")
        with self.b.open(self.base+"/api/income.csv") as response:
            other_csv=response.read().decode("utf-8")
        self.assertIn("Synthetic refund",own_csv)
        self.assertNotIn("Other user's income",own_csv)
        self.assertIn("Other user's income",other_csv)

    def test_cash_planning_api_requires_csrf_and_isolates_calendar_and_adjustments(self):
        today=date.today(); tomorrow=(today+timedelta(days=1)).isoformat()
        self.assertEqual(self.request(self.a,"/api/cash-position")[1]["configured"],False)
        self.assertEqual(self.request(self.a,"/api/cash-position","PUT",{"amount":"500","effective_on":today.isoformat()})[0],403)
        self.assertEqual(self.request(self.a,"/api/cash-position","PUT",{"amount":"500","effective_on":today.isoformat()},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,"/api/cash-position")[1]["configured"],False)
        category_a=self.request(self.a,"/api/options")[1]["categories"][0]["id"]
        category_b=self.request(self.b,"/api/options")[1]["categories"][0]["id"]
        self.assertEqual(self.request(self.a,"/api/expenses","POST",{"amount":"30","spent_on":today.isoformat(),"description":"Only account A","category_id":category_a},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,"/api/expenses","POST",{"amount":"40","spent_on":today.isoformat(),"description":"Only account B","category_id":category_b},self.csrf_b)[0],200)
        self.assertEqual(self.request(self.a,"/api/cash-adjustments","POST",{"amount":"10","adjustment_on":today.isoformat(),"description":"Correction"})[0],403)
        self.assertEqual(self.request(self.a,"/api/cash-adjustments","POST",{"amount":"10","adjustment_on":today.isoformat(),"description":"Correction"},self.csrf_a)[0],200)
        calendar_a=self.request(self.a,f"/api/calendar?start={today.isoformat()}&end={tomorrow}")[1]
        calendar_b=self.request(self.b,f"/api/calendar?start={today.isoformat()}&end={tomorrow}")[1]
        self.assertIn("Only account A",[row["description"] for row in calendar_a])
        self.assertNotIn("Only account B",[row["description"] for row in calendar_a])
        self.assertIn("Only account B",[row["description"] for row in calendar_b])
        self.assertEqual(self.request(self.a,f"/api/calendar?start={today.isoformat()}&end={tomorrow}&type=expense")[1][0]["event_type"],"expense")
        self.assertEqual(self.request(self.a,"/api/calendar?start=invalid&end=invalid")[0],400)
        self.assertEqual(self.request(self.a,"/api/cash-flow")[0],200)

    def test_account_deletion_requires_password_and_confirmation_and_cascades_only_owner(self):
        today=date.today().isoformat()
        user_a=self.request(self.a,"/api/session")[1]["user_id"]
        options_a=self.request(self.a,"/api/options")[1]
        options_b=self.request(self.b,"/api/options")[1]
        category_a=options_a["categories"][0]["id"]
        category_b=options_b["categories"][0]["id"]
        self.assertEqual(self.request(self.a,"/api/expenses","POST",{"amount":"20","spent_on":today,"description":"A private record","category_id":category_a},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,"/api/expenses","POST",{"amount":"30","spent_on":today,"description":"B private record","category_id":category_b},self.csrf_b)[0],200)
        card_a=self.request(self.a,"/api/cards","POST",{"bank_name":"Synthetic A","display_name":"A card","closing_day":10,"due_day":15},self.csrf_a)[1]["id"]
        card_b=self.request(self.b,"/api/cards","POST",{"bank_name":"Synthetic B","display_name":"B card","closing_day":10,"due_day":15},self.csrf_b)[1]["id"]
        self.assertEqual(self.request(self.a,"/api/expenses","POST",{"amount":"20","spent_on":today,"description":"A card purchase","card_id":card_a},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,"/api/expenses","POST",{"amount":"30","spent_on":today,"description":"B card purchase","card_id":card_b},self.csrf_b)[0],200)
        self.assertEqual(self.request(self.a,"/api/budgets","POST",{"month":today[:7],"amount":"500","category_id":category_a},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,"/api/budgets","POST",{"month":today[:7],"amount":"700","category_id":category_b},self.csrf_b)[0],200)
        rule_a=self.request(self.a,"/api/recurring","POST",{"description":"A recurring","amount":"5","frequency":"monthly","start_on":today,"category_id":category_a},self.csrf_a)[1]["id"]
        rule_b=self.request(self.b,"/api/recurring","POST",{"description":"B recurring","amount":"6","frequency":"monthly","start_on":today,"category_id":category_b},self.csrf_b)[1]["id"]
        income_a=self.request(self.a,"/api/income","POST",{"description":"A income","income_type":"salary","amount":"500","frequency":"monthly","start_on":today},self.csrf_a)[1]["id"]
        income_b=self.request(self.b,"/api/income","POST",{"description":"B income","income_type":"salary","amount":"600","frequency":"monthly","start_on":today},self.csrf_b)[1]["id"]
        self.request(self.a,"/api/income?month="+today[:7])
        self.request(self.b,"/api/income?month="+today[:7])
        self.assertEqual(self.request(self.a,f"/api/recurring/{rule_a}/occurrences/{today}/complete","POST",{},self.csrf_a)[0],200)
        self.assertEqual(self.request(self.b,f"/api/recurring/{rule_b}/occurrences/{today}/complete","POST",{},self.csrf_b)[0],200)
        second_a=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        _,second_login=self.request(second_a,"/api/login","POST",{"email":"api-a@example.invalid","password":"local-api-test-pass"})
        self.assertTrue(second_login["csrf"])
        delete_path="/api/account/delete"
        self.assertEqual(self.request(self.a,delete_path,"POST",{"password":"local-api-test-pass","confirmation":"SİL"})[0],403)
        self.assertEqual(self.request(self.a,delete_path,"POST",{"password":"wrong-password","confirmation":"SİL"},self.csrf_a)[0],400)
        self.assertEqual(self.request(self.a,delete_path,"POST",{"password":"local-api-test-pass","confirmation":"sil"},self.csrf_a)[0],400)
        self.assertEqual(self.request(self.a,"/api/options")[0],200)
        status,deleted=self.request(self.a,delete_path,"POST",{"password":"local-api-test-pass","confirmation":"SİL"},self.csrf_a)
        self.assertEqual((status,deleted),(200,{"ok":True}))
        self.assertEqual(self.request(self.a,"/api/options")[0],401)
        self.assertEqual(self.request(second_a,"/api/options")[0],401)
        self.assertEqual(self.request(self.b,"/api/options")[0],200)
        b_expenses=self.request(self.b,"/api/expenses?month="+today[:7])[1]
        self.assertIn("B private record",[row["description"] for row in b_expenses])
        self.assertEqual(self.request(self.b,"/api/recurring")[1]["rules"][0]["id"],rule_b)
        self.assertEqual(self.request(self.b,"/api/income")[1]["rules"][0]["id"],income_b)
        for route in ("/api/backup","/api/backups","/backups/private.sqlite3","/static/backups/private.sqlite3"):
            self.assertEqual(self.request(self.b,route)[0],404)
        with app.connect() as db:
            for table in ("categories","payment_methods","expenses","budgets","sessions","recurring_expenses","recurring_occurrences","income_records","income_rules","income_rule_amounts","credit_cards","card_statements","card_repayments","installment_plans","installment_occurrences"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?",(user_a,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM income_rules WHERE id=? AND user_id=?",(income_b,user_a)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM income_rules WHERE id=? AND user_id=?",(income_b,self.request(self.b,"/api/session")[1]["user_id"])).fetchone()[0],1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])


if __name__=="__main__":unittest.main()
