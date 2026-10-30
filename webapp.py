"""Flask HTTP adapter for the existing expense-tracker business logic."""
from __future__ import annotations

import csv
import hmac
import ipaddress
import os
import sqlite3
import traceback
from datetime import date
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import app as core
import privacy_notice as privacy


def create_app(test_config: dict | None = None) -> Flask:
    web = Flask(__name__, static_folder=None)
    web.config.update(MAX_CONTENT_LENGTH=core.MAX_CSV_BYTES + 64_000, TESTING=False)
    web.config["TRUST_PROXY"] = os.environ.get("TRUST_PROXY", "0") == "1"
    production = os.environ.get("APP_ENV", "development").strip().lower() == "production"
    web.config["PRIVATE_BETA"] = core.private_beta_enabled(strict=production)
    if test_config:
        web.config.update(test_config)

    if not web.config.get("TESTING"):
        core.validate_runtime_configuration()
        try:
            core.init_db()
            from simple_cashflow import initialize
            initialize()
            import profile_settings
            profile_settings.initialize()
        except Exception as _db_err:  # noqa: BLE001
            # Log but don't crash: DB may be temporarily unreachable on cold start.
            # All request handlers that need the DB will return 503 naturally.
            import traceback as _tb
            print(f"[startup] init_db failed: {_db_err}", flush=True)
            _tb.print_exc()

    # Disabled until the operator provides a complete, reviewed information notice.
    notice_path = os.environ.get('PRIVACY_NOTICE_PATH')
    notice = privacy.load_notice(web.config['PRIVACY_NOTICE'], allow_draft=bool(web.config.get('PRIVACY_NOTICE_ALLOW_DRAFT')) and not production) if 'PRIVACY_NOTICE' in web.config else privacy.from_file(notice_path) if notice_path else None
    if notice:
        privacy.initialize(notice)
    web.extensions['privacy_notice'] = notice

    def json_error(message: str, status: int):
        return jsonify(error=message), status

    def request_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None
        return data

    def auth(csrf: bool = False):
        token = request.cookies.get(core.COOKIE)
        session = core.session_user(token)
        if not session:
            return None, json_error("Oturum açmanız gerekiyor.", 401)
        if csrf and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf_token"]):
            return None, json_error("Güvenlik doğrulaması başarısız.", 403)
        return session, None

    def client_key() -> str:
        # Render's Cloudflare edge overwrites CF-Connecting-IP. Ignore X-Forwarded-For,
        # which can contain caller-supplied values. Never enable this outside that edge.
        if web.config.get("TRUST_PROXY"):
            forwarded = request.headers.get("CF-Connecting-IP", "").strip()
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return request.remote_addr or "unknown"

    @web.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @web.errorhandler(HTTPException)
    def http_error(error):
        message = "İstek tamamlanamadı."
        if error.code == 404:
            message = "Bulunamadı."
        elif error.code == 413:
            message = "İstek boyutu sınırı aşıldı."
        elif error.code == 405:
            message = "Bu işlem desteklenmiyor."
        return json_error(message, error.code or 400)

    @web.errorhandler(Exception)
    def internal_error(error):
        # Keep request data and SQL values out of logs, but include safe PostgreSQL
        # schema diagnostics so production column mismatches can be repaired.
        stack = "".join(traceback.format_tb(error.__traceback__))
        diag = getattr(error, "diag", None)
        table_name = getattr(diag, "table_name", None)
        column_name = getattr(diag, "column_name", None)
        sqlstate = getattr(error, "pgcode", None)
        web.logger.error(
            "Unhandled request error (%s; table=%r; column=%r; sqlstate=%r)\n%s",
            type(error).__name__, table_name, column_name, sqlstate, stack
        )
        # Avoid returning exception details (SQL, filesystem paths, request data) to clients.
        return json_error("Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.", 500)

    @web.get("/healthz")
    def health_check():
        try:
            with core.connect() as db:
                db.execute("SELECT 1").fetchone()
            return jsonify(status="ok")
        except Exception:
            return json_error("Hizmet geçici olarak kullanılamıyor.", 503)

    @web.get("/api/session")
    def session_route():
        session = core.session_user(request.cookies.get(core.COOKIE))
        payload = {"user_id": session["user_id"], "csrf": session["csrf_token"]} if session else {"user_id": None}
        return jsonify({**payload, "private_beta": web.config["PRIVATE_BETA"], "privacy_notice": privacy.public_notice(notice)})

    @web.get('/api/privacy-notice')
    def privacy_notice_route():
        return jsonify(privacy.public_notice(notice))

    @web.get('/api/privacy-acknowledgments')
    def privacy_acknowledgments_route():
        session, error = auth()
        if error:
            return error
        return jsonify(acknowledgments=privacy.acknowledgments_for_user(session['user_id']) if notice else [])

    @web.get("/api/options")
    def options_route():
        session, error = auth()
        return error or jsonify(core.user_options(session["user_id"]))

    @web.post("/api/cards")
    def save_card_route():
        session, error = auth(csrf=True)
        if error: return error
        data = request_body()
        if data is None: return json_error("Geçersiz istek.", 400)
        try:
            saved = core.save_credit_card(session["user_id"], data)
            return json_error("Kart bulunamadı.", 404) if not saved else jsonify(id=saved)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Kart bilgilerini kontrol edin.", 400)

    @web.get("/api/cards/<card_id>/statements")
    def card_statements_route(card_id):
        session, error = auth()
        if error: return error
        try: card_id = core.record_id(card_id)
        except ValueError: return json_error("Kart bulunamadı.", 404)
        result = core.list_card_statements(session["user_id"], card_id)
        return json_error("Kart bulunamadı.", 404) if result is None else jsonify(result)

    @web.post("/api/cards/<card_id>/repayments")
    def card_repayment_route(card_id):
        session, error = auth(csrf=True)
        if error: return error
        data = request_body()
        if data is None: return json_error("Geçersiz istek.", 400)
        try:
            saved = core.record_card_repayment(session["user_id"], core.record_id(card_id), data)
            return json_error("Kart bulunamadı.", 404) if not saved else jsonify(id=saved)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Kart ödeme bilgilerini ve tutarı kontrol edin.", 400)

    @web.post("/api/statements/<statement_id>/dates")
    def statement_dates_route(statement_id):
        session, error = auth(csrf=True)
        if error: return error
        data = request_body()
        if data is None: return json_error("Geçersiz istek.", 400)
        try:
            changed = core.edit_statement_dates(session["user_id"], core.record_id(statement_id), data.get("closing_on", ""), data.get("due_on", ""))
            return jsonify(ok=True) if changed else json_error("Ekstre bulunamadı.", 404)
        except (ValueError, TypeError): return json_error("Ekstre tarihlerini kontrol edin.", 400)

    @web.get("/api/installments")
    def installments_route():
        session, error = auth()
        return error or jsonify(core.list_installments(session["user_id"]))

    @web.post("/api/installments/<occurrence_id>/status")
    def installment_status_route(occurrence_id):
        session, error = auth(csrf=True)
        if error: return error
        data = request_body()
        if data is None: return json_error("Geçersiz istek.", 400)
        try:
            changed = core.set_installment_status(session["user_id"], core.record_id(occurrence_id), data.get("status", ""), data.get("paid_on"))
            return jsonify(ok=True) if changed else json_error("Taksit bulunamadı.", 404)
        except (ValueError, TypeError): return json_error("Taksit durumunu kontrol edin.", 400)

    def simple_write(operation):
        session,error=auth(csrf=True)
        if error:
            return error
        data=request_body()
        if data is None:
            return json_error("Geçersiz kayıt isteği.",400)
        from simple_cashflow import Conflict
        try:
            return jsonify(operation(session["user_id"],data))
        except Conflict as error:
            return json_error(str(error),409)
        except (ValueError,TypeError,OverflowError) as error:
            message=str(error) if isinstance(error,ValueError) else ""
            if not message or message.startswith(("invalid","Invalid","amount","date","end","Only")):
                message="Tarih, tutar ve açıklama bilgilerini kontrol edin."
            return json_error(message,400)

    @web.get("/api/profile")
    def profile_route():
        session,error=auth()
        if error:
            return error
        import profile_settings
        return jsonify(profile_settings.get_profile(session["user_id"]))

    @web.put("/api/profile")
    def profile_save_route():
        import profile_settings
        return simple_write(profile_settings.save_name)

    @web.post("/api/profile/cards")
    def profile_card_save_route():
        import profile_settings
        return simple_write(profile_settings.save_card)

    @web.post('/api/simple-card-monthly-payments')
    def simple_card_monthly_payment_route():
        from simple_cashflow import record_card_monthly_payment
        return simple_write(record_card_monthly_payment)

    @web.put('/api/simple-card-monthly-total')
    def simple_card_monthly_total_route():
        from simple_cashflow import save_card_monthly_total
        return simple_write(save_card_monthly_total)

    @web.put('/api/simple-opening')
    def simple_opening_route():
        from simple_cashflow import save_opening
        return simple_write(save_opening)

    @web.post('/api/simple-statement-payments')
    def simple_statement_payment_route():
        from simple_cashflow import record_statement_payment
        return simple_write(record_statement_payment)

    @web.post("/api/simple-payment-plans")
    def simple_plan_save_route():
        from simple_cashflow import save_payment_plan
        return simple_write(save_payment_plan)

    @web.put('/api/simple-income')
    def simple_income_edit_route():
        from simple_cashflow import edit_simple_income
        return simple_write(edit_simple_income)

    @web.post('/api/simple-records/delete')
    def simple_record_delete_route():
        from simple_cashflow import delete_financial_record
        return simple_write(delete_financial_record)

    @web.post('/api/simple-records/restore')
    def simple_record_restore_route():
        from simple_cashflow import restore_financial_record
        return simple_write(restore_financial_record)

    @web.post("/api/simple-income")
    def simple_income_save_route():
        from simple_cashflow import save_income_once,save_monthly_income
        def save(user_id,data):
            if data.get('frequency','once') not in ('once','monthly','weekly'):
                raise ValueError('Gelir sıklığı tek seferlik, aylık veya haftalık olmalı.')
            if data.get("frequency") in ("monthly","weekly"):
                return save_monthly_income(user_id,data)
            return save_income_once(user_id,data)
        return simple_write(save)

    @web.post("/api/simple-income/receive")
    def simple_income_receive_route():
        from simple_cashflow import receive_income
        return simple_write(receive_income)

    @web.post("/api/simple-payments/<rule_id>/complete")
    def simple_payment_complete_route(rule_id):
        session,error=auth(csrf=True)
        if error:
            return error
        data=request_body()
        if data is None:
            return json_error("Geçersiz ödeme isteği.",400)
        try:
            from simple_cashflow import record_payment
            return jsonify(record_payment(session["user_id"],core.record_id(rule_id),data))
        except (ValueError,TypeError,OverflowError):
            return json_error("Ödeme bilgilerini kontrol edin. Taksit zaten ödenmiş olabilir; tutar kalan borcu aşmamalı, gerçek ödeme tarihi gelecekte olmamalı.",400)

    @web.get("/api/recommendations")
    def recommendations_route():
        session,error=auth()
        if error:return error
        import recommendations
        from simple_cashflow import today
        try:return jsonify(recommendations.for_user(session['user_id'],request.args.get('month',today().strftime('%Y-%m'))))
        except (ValueError,TypeError,OverflowError):return json_error('Tavsiye ayını kontrol edin.',400)

    @web.get("/api/simple-cashflow")
    def simple_cashflow_route():
        session,error=auth()
        if error:
            return error
        from simple_cashflow import overview,today
        start=request.args.get("start",today().isoformat())
        end=request.args.get("end",(today().replace(day=1)+core.timedelta(days=120)).isoformat())
        try:
            return jsonify(overview(session["user_id"],start,end))
        except (ValueError,TypeError,OverflowError):
            return json_error("Ödeme planının tarih aralığını kontrol edin.",400)

    @web.get("/api/summary")
    def summary_route():
        session, error = auth()
        if error:
            return error
        month = request.args.get("month", date.today().strftime("%Y-%m"))
        try:
            return jsonify(core.summary(session["user_id"], month))
        except ValueError:
            return json_error("Geçersiz ay.", 400)

    @web.get("/api/cash-position")
    def cash_position_route():
        session, error = auth()
        return error or jsonify(core.get_cash_position(session["user_id"]))

    @web.put("/api/cash-position")
    def save_cash_position_route():
        session, error = auth(csrf=True)
        if error: return error
        data=request_body()
        if data is None: return json_error("Geçersiz istek.",400)
        try: return jsonify(core.save_opening_cash(session["user_id"],data.get("amount"),data.get("effective_on","")))
        except (ValueError,TypeError,OverflowError): return json_error("Başlangıç bakiyesini ve geçerli tarihini kontrol edin.",400)

    @web.post("/api/cash-adjustments")
    def cash_adjustment_route():
        session, error = auth(csrf=True)
        if error: return error
        data=request_body()
        if data is None: return json_error("Geçersiz istek.",400)
        try:
            adjustment_id=core.add_cash_adjustment(session["user_id"],data.get("amount"),data.get("adjustment_on",""),data.get("description",""))
            return jsonify(id=adjustment_id)
        except (ValueError,TypeError,OverflowError): return json_error("Bakiye düzeltmesini, tutarı ve başlangıç tarihinden sonraki tarihi kontrol edin.",400)

    @web.get("/api/calendar")
    def payment_calendar_route():
        session,error=auth()
        if error:return error
        start=request.args.get("start",date.today().isoformat())
        end=request.args.get("end",(date.today()+core.timedelta(days=60)).isoformat())
        try: rows=core.payment_calendar(session["user_id"],start,end)
        except (ValueError,TypeError,OverflowError):return json_error("Takvim tarih aralığını kontrol edin (en fazla 367 gün).",400)
        event_type=request.args.get("type")
        status=request.args.get("status")
        allowed_types={"expense","income","recurring_expense","card_statement","card_repayment","installment","cash_adjustment"}
        allowed_statuses={"completed","received","expected","overdue","cancelled","planned","paused","paid","unpaid","partial"}
        if event_type and event_type not in allowed_types:return json_error("Takvim türü geçersiz.",400)
        if status and status not in allowed_statuses:return json_error("Takvim durumu geçersiz.",400)
        if event_type:rows=[row for row in rows if row["event_type"]==event_type]
        if status:rows=[row for row in rows if row["status"]==status]
        return jsonify(rows)

    @web.get("/api/cash-flow")
    def cash_flow_route():
        session,error=auth()
        if error:return error
        try:return jsonify(core.cash_flow_forecast(session["user_id"],request.args.get("start"),request.args.get("end")))
        except (ValueError,TypeError,OverflowError):return json_error("Nakit akışı tarih aralığını kontrol edin (en fazla 367 gün).",400)

    @web.get("/api/recurring")
    def recurring_route():
        session, error = auth()
        return error or jsonify(core.recurring_overview(session["user_id"]))

    @web.get("/api/income")
    def income_route():
        session, error = auth()
        if error:
            return error
        month = request.args.get("month", date.today().strftime("%Y-%m"))
        try:
            return jsonify(core.income_overview(session["user_id"], month))
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Gelir bilgileri yüklenemedi. Seçili ayı kontrol edin.", 400)

    @web.get("/api/income.csv")
    def income_export_route():
        session, error = auth()
        if error:
            return error
        body = core.export_income_csv(session["user_id"]).encode("utf-8")
        return Response(body, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=gelirler.csv"})

    @web.post("/api/income")
    def save_income_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            if data.get("frequency") in ("daily", "weekly", "monthly", "yearly"):
                saved = core.save_income_rule(session["user_id"], data)
                return json_error("Gelir kuralı bulunamadı veya iptal edilmiş.", 404) if not saved else jsonify(id=saved, kind="rule")
            saved = core.save_income(session["user_id"], data)
            return json_error("Gelir kaydı bulunamadı.", 404) if not saved else jsonify(id=saved, kind="record")
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Gelir bilgilerini kontrol edin.", 400)

    @web.post("/api/income/<income_id>/status")
    def income_status_route(income_id):
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            changed = core.set_income_status(session["user_id"], core.record_id(income_id), data.get("status", ""), data.get("received_on"))
        except (ValueError, TypeError):
            return json_error("Gelir durumu veya tahsilat tarihini kontrol edin.", 400)
        return jsonify(ok=True) if changed else json_error("Gelir bulunamadı veya durumu değiştirilemiyor.", 404)

    @web.delete("/api/income/<income_id>")
    def delete_income_route(income_id):
        session, error = auth(csrf=True)
        if error:
            return error
        try:
            deleted = core.delete_income(session["user_id"], core.record_id(income_id))
        except (ValueError, TypeError):
            return json_error("Gelir kaydı bulunamadı.", 404)
        return jsonify(ok=True) if deleted else json_error("Yalnızca beklenen tek seferlik gelir silinebilir.", 404)

    @web.post("/api/income-rules/<rule_id>/status")
    def income_rule_status_route(rule_id):
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None or data.get("status") not in ("active", "paused", "cancelled"):
            return json_error("Durum bilgisini kontrol edin.", 400)
        try:
            changed = core.set_income_rule_status(session["user_id"], core.record_id(rule_id), data["status"])
        except (ValueError, TypeError):
            return json_error("Durum bilgisini kontrol edin.", 400)
        return jsonify(ok=True) if changed else json_error("Gelir kuralı bulunamadı.", 404)

    @web.post("/api/income-rules/<rule_id>/amount")
    def income_rule_amount_route(rule_id):
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            changed = core.set_income_rule_amount(session["user_id"], core.record_id(rule_id), data.get("effective_on", ""), data.get("amount"))
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Yeni gelir tutarı ve yürürlük tarihini kontrol edin.", 400)
        return jsonify(ok=True) if changed else json_error("Aktif gelir kuralı bulunamadı.", 404)

    @web.get("/api/expenses")
    def expenses_route():
        session, error = auth()
        if error:
            return error
        month = request.args.get("month", date.today().strftime("%Y-%m"))
        try:
            start, end = core.month_bounds(month)
        except ValueError:
            return json_error("Geçersiz ay.", 400)
        with core.connect() as db:
            rows = db.execute("""SELECT e.id,e.amount_minor,e.currency,e.spent_on,e.description,e.note,e.category_id,e.payment_method_id,e.card_id,e.statement_id,e.installments_count,
                c.name category,p.name payment_method FROM expenses e
                LEFT JOIN categories c ON c.id=e.category_id AND c.user_id=e.user_id
                LEFT JOIN payment_methods p ON p.id=e.payment_method_id AND p.user_id=e.user_id
                WHERE e.user_id=? AND e.currency='TRY' AND e.spent_on>=? AND e.spent_on<? ORDER BY e.spent_on DESC,e.id DESC""",
                (session["user_id"], start, end)).fetchall()
        return jsonify([dict(row) for row in rows])

    @web.get("/api/expenses.csv")
    def export_route():
        session, error = auth()
        if error:
            return error
        body = core.export_csv(session["user_id"]).encode("utf-8")
        return Response(body, content_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=harcamalar.csv"})

    @web.get("/")
    def index_route():
        return send_from_directory(core.ROOT / "static", "index.html")

    @web.get("/static/<path:filename>")
    def static_route(filename):
        response = send_from_directory(core.ROOT / "static", filename)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @web.post("/api/register")
    @web.post("/api/login")
    def auth_route():
        path = request.path
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        scope = "register" if path.endswith("register") else "login"
        address = client_key()
        identity = data.get("email", "") if scope == "register" else data.get("identifier", data.get("email", data.get("username", "")))
        if core.reserve_auth_attempt(address, scope) or core.reserve_auth_attempt(address, scope, identity):
            return json_error("Çok sayıda deneme yapıldı. Lütfen bir süre sonra tekrar deneyin.", 429)
        try:
            acknowledgment = privacy.prepare_acknowledgment(notice, data.get('privacy_acknowledgment'), scope)
        except privacy.NoticeError as error:
            return jsonify(error=str(error), code=error.code), error.status
        if scope == "register":
            if web.config["PRIVATE_BETA"] and not isinstance(data.get("invitation_token"), str):
                return json_error("Özel beta kaydı için geçerli bir davet kodu gerekir.", 403)
            try:
                # Missing usernames are rejected at the public registration boundary.
                username = core.normalize_username(data.get("username"))
                user_id = core.create_user(data.get("email", ""), data.get("password", ""), data.get("invitation_token"), web.config["PRIVATE_BETA"], username=username, privacy_ack=acknowledgment)
            except core.UsernameUnavailable as error:
                return json_error(str(error),409)
            except core.InvalidUsername as error:
                return json_error(str(error),400)
            except core.InvalidInvitation:
                return json_error("Davet kodu geçersiz, süresi dolmuş veya kullanılmış.", 403)
            except (*core._DB_INTEGRITY_ERRORS,):
                return json_error("Kayıt tamamlanamadı. Bilgileri kontrol edin.", 400)
            except (ValueError, AttributeError):
                return json_error("E-posta veya parola geçersiz. Parola en az 10 karakter olmalı.", 400)
            core.clear_auth_failures(address, scope)
            core.clear_auth_failures(address, scope, identity)
        else:
            user_id = core.authenticate(identity, data.get("password", ""))
            if not user_id:
                return json_error("Kullanıcı adı, e-posta veya parola hatalı.", 401)
            core.clear_auth_failures(address, scope)
            core.clear_auth_failures(address, scope, identity)
        token, csrf_token = core.create_session(user_id, privacy_ack=acknowledgment if scope == "login" else None)
        response = jsonify(user_id=user_id, csrf=csrf_token)
        response.set_cookie(core.COOKIE, token, max_age=604800, httponly=True, samesite="Strict", secure=core.secure_cookie_enabled(), path="/")
        return response

    @web.post("/api/logout")
    def logout_route():
        session, error = auth(csrf=True)
        if error:
            return error
        core.revoke_session(request.cookies.get(core.COOKIE))
        response = Response(status=200)
        response.delete_cookie(core.COOKIE, path="/", httponly=True, samesite="Strict", secure=core.secure_cookie_enabled())
        return response

    @web.post("/api/account/delete")
    def delete_account_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        address = client_key()
        identity = str(session["user_id"])
        if core.reserve_auth_attempt(address, "account-delete") or core.reserve_auth_attempt(address, "account-delete", identity):
            return json_error("Çok sayıda deneme yapıldı. Lütfen bir süre sonra tekrar deneyin.", 429)
        try:
            deleted = core.delete_account(session["user_id"], data.get("password"), data.get("confirmation"))
        except (TypeError, *core._DB_INTEGRITY_ERRORS):
            deleted = False
        if not deleted:
            return json_error("Parolanızı ve hesap silme onayını kontrol edin.", 400)
        core.clear_auth_failures(address, "account-delete")
        core.clear_auth_failures(address, "account-delete", identity)
        response = jsonify(ok=True)
        response.delete_cookie(core.COOKIE, path="/", httponly=True, samesite="Strict", secure=core.secure_cookie_enabled())
        return response

    @web.post("/api/expenses")
    def save_expense_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            saved = core.save_expense(session["user_id"], data)
            return json_error("Harcama bulunamadı.", 404) if not saved else jsonify(id=saved)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Harcama bilgilerini kontrol edin.", 400)

    @web.post("/api/import/preview")
    def import_preview_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            return jsonify(rows=core.parse_expense_csv(session["user_id"], data.get("csv", "")))
        except (ValueError, TypeError, csv.Error) as exc:
            return json_error(str(exc) or "CSV dosyasını kontrol edin.", 400)

    @web.post("/api/import")
    def import_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            selected = data.get("selected_rows", [])
            if not isinstance(selected, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in selected):
                raise ValueError
            return jsonify(core.import_csv_rows(session["user_id"], data.get("csv", ""), selected))
        except (ValueError, TypeError, csv.Error, *core._DB_INTEGRITY_ERRORS) as exc:
            return json_error(str(exc) or "CSV içe aktarılamadı. Dosyayı yeniden önizleyip kontrol edin.", 400)

    @web.post("/api/recurring")
    def save_recurring_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            saved = core.save_recurring(session["user_id"], data)
            return json_error("Düzenli harcama bulunamadı veya iptal edilmiş.", 404) if not saved else jsonify(id=saved)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Düzenli harcama bilgilerini kontrol edin.", 400)

    @web.post("/api/recurring/<recurring_id>/status")
    def recurring_status_route(recurring_id):
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            changed = core.set_recurring_status(session["user_id"], core.record_id(recurring_id), data.get("status", ""))
        except (ValueError, TypeError):
            return json_error("Durum bilgisini kontrol edin.", 400)
        return jsonify(ok=True) if changed else json_error("Düzenli harcama bulunamadı.", 404)

    @web.post("/api/recurring/<recurring_id>/occurrences/<scheduled_on>/complete")
    def complete_occurrence_route(recurring_id, scheduled_on):
        session, error = auth(csrf=True)
        if error:
            return error
        try:
            result = core.complete_recurring_occurrence(session["user_id"], core.record_id(recurring_id), scheduled_on)
            return json_error("Düzenli harcama bulunamadı.", 404) if result is None else jsonify(result)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Bu düzenli harcama gerçekleşmesi kaydedilemedi.", 400)

    @web.post("/api/budgets")
    def save_budget_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        try:
            core.save_budget(session["user_id"], data.get("month", ""), data.get("amount"), data.get("category_id"))
            return jsonify(ok=True)
        except (ValueError, TypeError, *core._DB_INTEGRITY_ERRORS):
            return json_error("Bütçe bilgilerini kontrol edin.", 400)

    @web.post("/api/categories")
    def create_category_route():
        session, error = auth(csrf=True)
        if error:
            return error
        data = request_body()
        if data is None:
            return json_error("Geçersiz istek.", 400)
        name_value = data.get("name", "")
        if not isinstance(name_value, str):
            return json_error("Kategori adını kontrol edin.", 400)
        name = name_value.strip()
        if not name or len(name) > 50:
            return json_error("Kategori adını kontrol edin.", 400)
        try:
            with core.connect() as db:
                db.execute("INSERT INTO categories(user_id,name) VALUES(?,?)", (session["user_id"], name))
            return jsonify(ok=True), 201
        except (*core._DB_INTEGRITY_ERRORS,):
            return json_error("Bu kategori zaten var.", 409)

    @web.delete("/api/expenses/<expense_id>")
    def delete_expense_route(expense_id):
        session, error = auth(csrf=True)
        if error:
            return error
        try:
            parsed_id = core.record_id(expense_id)
        except ValueError:
            parsed_id = None
        if not parsed_id:
            return json_error("Bulunamadı.", 404)
        try:
            deleted = core.delete_expense(session["user_id"], parsed_id)
        except ValueError:
            return json_error("Ödeme geçmişi bulunan kart harcamaları silinemez.", 409)
        return jsonify(ok=True) if deleted else json_error("Harcama bulunamadı.", 404)

    return web
