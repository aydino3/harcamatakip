"""Gelir ve kullanıcı tarafından girilen ödeme günlerinden salt okunur nakit planı."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import calendar
import card_schedule
from contextlib import nullcontext

import app as core


def today():
    return datetime.now(ZoneInfo("Europe/Istanbul")).date()


def payment_dates(rule, start, end):
    first = date.fromisoformat(rule["start_on"])
    last = date.fromisoformat(rule["end_on"]) if rule["end_on"] else None
    if rule.get("entry_kind")=="expense":
        if start<=first<=end:yield first
        return
    if rule["frequency"] == "monthly":
        month = max(first.year*12+first.month-1,start.year*12+start.month-1)
        stop = min(end,last) if last else end
        while month <= stop.year*12+stop.month-1:
            year,index = divmod(month,12)
            month_start = date(year,index+1,1)
            due = core._month_close(year,index+1,first.day)
            if month == first.year*12+first.month-1:
                due=first
            if last and month == last.year*12+last.month-1:
                due=last  # User's explicit final payment date, including an earlier day.
            if start<=due<=end and due>=first:
                yield due
            month+=1
        return
    index = core.recurrence_index_on_or_after(first,rule["frequency"],start)
    while True:
        due = core.recurrence_date(first,rule["frequency"],index)
        if due>end or last and due>last:
            return
        if due>=start:
            yield due
        index+=1


def read_data(user_id,connection=None):
    # No initialization, materialization, UPDATE or INSERT in this read path.
    with (nullcontext(connection) if connection is not None else core.connect()) as db:
        data={}
        for key,table,order in (
            ("recurring","recurring_expenses","start_on,id"),
            ("income_rules","income_rules","start_on,id"),
            ("income_rule_amounts","income_rule_amounts","rule_id,effective_on"),
            ("income_records","income_records","expected_on,id"),
            ("expenses","expenses","spent_on,id"),
            ("repayments","card_repayments","paid_on,id"),
            ("occurrences","recurring_occurrences","scheduled_on,id"),
            ("adjustments","cash_adjustments","adjustment_on,id"),
            ("payment_events","payment_events","paid_on,id"),
            ("statements","card_statements","due_on,id"),
            ("cards","credit_cards","id"),
            ("profile_cards","profile_cards","id"),
            ("card_monthly_totals","card_monthly_totals","month,card_ref"),
            ("card_monthly_payments","card_monthly_payments","paid_on,id"),
            ("payment_methods","payment_methods","id"),
            ("installment_plans","installment_plans","id"),
            ("installment_occurrences","installment_occurrences","due_on,id"),
        ):
            data[key]=[dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY {order}",(user_id,)).fetchall()]
        details={row['recurring_id']:dict(row) for row in db.execute('SELECT * FROM payment_plan_details WHERE user_id=?',(user_id,))}
        for rule in data['recurring']:
            extra=details.get(rule['id'],{})
            fallback=legacy_entry_kind(rule,data['payment_methods'])
            rule.update(entry_kind=extra.get('entry_kind',fallback),purchased_on=extra.get('purchased_on'),card_ref=('legacy:'+str(extra['legacy_card_id'])) if extra.get('legacy_card_id') else ('card:'+str(extra['profile_card_id'])) if extra.get('profile_card_id') else '')
        opening=db.execute("SELECT opening_balance_minor,effective_on,auto_income_on_due FROM cash_settings WHERE user_id=?",(user_id,)).fetchone()
        data["opening"]=dict(opening) if opening else None
    return _visible_records(data,user_id,connection)


def income_events(data,start_value,end_value,as_of):
    start,end=date.fromisoformat(start_value),date.fromisoformat(end_value)
    events=[]
    def add(key,when,amount,description,kind,status,**extra):
        if start_value<=when<=end_value:
            events.append({"id":key,"date":when,"amount_minor":int(amount),"description":description,"kind":kind,"status":status,**extra})
    automatic=bool((data.get('opening') or {}).get('auto_income_on_due'))
    actual_income_keys=set(tuple(key) for key in data.get('suppressed_income_keys',[]))
    rules={row["id"]:row for row in data["income_rules"]}
    for row in data["income_records"]:
        if row["rule_id"] is not None:
            actual_income_keys.add((row["rule_id"],row["expected_on"]))
        if row["status"]=="received":
            if row["rule_id"] is not None:
                actual_income_keys.add((row["rule_id"],row["expected_on"]))
            add(f"income:{row['id']}",row["received_on"],row["amount_minor"],row["description"],"income","received",record_id=row["id"],expected_on=row["expected_on"])
        elif row["status"]=="expected":
            if row["rule_id"] is not None and rules.get(row["rule_id"],{}).get("status")!="active":continue
            add(f"income:{row['id']}",row["expected_on"],row["amount_minor"],row["description"],"income","received" if automatic and row["expected_on"]<=as_of.isoformat() else "overdue" if row["expected_on"]<as_of.isoformat() else "expected",record_id=row["id"],auto_received=automatic and row["expected_on"]<=as_of.isoformat())
    for rule in rules.values():
        if rule["status"]!="active":
            continue
        first=date.fromisoformat(rule["start_on"])
        last=date.fromisoformat(rule["end_on"]) if rule["end_on"] else None
        index=core.income_recurrence_index(first,rule["frequency"],max(start,first),rule["schedule_mode"])
        prices=[row for row in data["income_rule_amounts"] if row["rule_id"]==rule["id"]]
        while True:
            due=core.income_recurrence_date(first,rule["frequency"],index,rule["schedule_mode"])
            if due>end or last and due>last:
                break
            key=(rule["id"],due.isoformat())
            eligible=[row for row in prices if row["effective_on"]<=due.isoformat()]
            if due>=start and eligible and key not in actual_income_keys:
                amount=max(eligible,key=lambda row:row["effective_on"])["amount_minor"]
                add(f"income-rule:{rule['id']}:{due}",due.isoformat(),amount,rule["description"],"income","received" if automatic and due<=as_of else "overdue" if due<as_of else "expected",rule_id=rule["id"],scheduled_on=due.isoformat(),auto_received=automatic and due<=as_of)
            index+=1
    return events


def calculate(data,start_value,end_value,as_of=None):
    as_of=as_of or today()
    start,end=date.fromisoformat(start_value),date.fromisoformat(end_value)
    if start.isoformat()!=start_value or end.isoformat()!=end_value or end<start or (end-start).days>3660:
        raise ValueError("Tarih aralığını kontrol edin; en fazla 10 yıl seçilebilir.")
    events=[]
    def add(key,when,amount,description,kind,status,**extra):
        if start_value<=when<=end_value:
            events.append({"id":key,"date":when,"amount_minor":int(amount),"description":description,"kind":kind,"status":status,**extra})
    events.extend(income_events(data,start_value,end_value,as_of))
    expenses={row["id"]:row for row in data["expenses"]}
    completed={}
    linked_expenses=set()
    for occurrence in data["occurrences"]:
        expense=expenses.get(occurrence["expense_id"])
        if not expense:
            continue  # Deleted expense is not proof of a real cash payment.
        completed[(occurrence["recurring_id"],occurrence["scheduled_on"])]=expense
        linked_expenses.add(expense["id"])
        add(f"payment-actual:{expense['id']}",occurrence["paid_on"],expense["amount_minor"],expense["description"],"payment","paid",recurring_id=occurrence["recurring_id"],scheduled_on=occurrence["scheduled_on"])
    partial={}
    for row in data.get("payment_events",[]):
        key=(row["recurring_id"],row["scheduled_on"])
        partial[key]=partial.get(key,0)+int(row["amount_minor"])
        rule=next((r for r in data["recurring"] if r["id"]==row["recurring_id"]),None)
        add(f"payment-event:{row['id']}",row["paid_on"],row["amount_minor"],rule["description"] if rule else "Kaydedilmiş ödeme","payment","paid",recurring_id=row["recurring_id"],scheduled_on=row["scheduled_on"])
    for rule in data["recurring"]:
        if rule["status"]!="active":
            continue
        for due in payment_dates(rule,start-timedelta(days=36),end+timedelta(days=36)):
            if (rule["id"],due.isoformat()) in completed:
                continue
            remaining=int(rule["amount_minor"])-partial.get((rule["id"],due.isoformat()),0)
            if remaining<=0:
                continue
            actual_due=payment_due(data,rule,due.isoformat())
            add(f"payment:{rule['id']}:{due}",actual_due,remaining,rule["description"],"payment","overdue" if actual_due<as_of.isoformat() else "planned",recurring_id=rule["id"],scheduled_on=due.isoformat())
    for row in data["expenses"]:
        if row["id"] not in linked_expenses and row["card_id"] is None:
            add(f"expense:{row['id']}",row["spent_on"],row["amount_minor"],row["description"],"payment","paid")
    for row in data["repayments"]:
        add(f"repayment:{row['id']}",row["paid_on"],row["amount_minor"],"Kaydedilmiş kart ödemesi","payment","paid")
    for statement in statement_balances(data):
        if statement['remaining_minor']:
            add(f"statement:{statement['id']}",statement['due_on'],statement['remaining_minor'],statement['description'],'payment','overdue' if statement['due_on']<as_of.isoformat() else 'planned',statement_id=statement['id'],card_id=statement['card_id'],scheduled_on=statement['payment_month']+'-01')
    events=apply_monthly_card_totals(data,events,start_value,end_value,as_of)
    for index,row in enumerate(data["adjustments"]):
        add(f"adjustment:{index}",row["adjustment_on"],row["amount_minor"],row.get("description","Bakiye düzeltmesi"),"adjustment","completed")
    events.sort(key=lambda row:(row["date"],0 if row["kind"]=="income" else 1,row["id"]))
    totals={"income_received_minor":0,"income_expected_minor":0,"payments_paid_minor":0,"payments_planned_minor":0,"adjustments_minor":0}
    running=0
    for event in events:
        if event["kind"]=="income":
            totals["income_received_minor" if event["status"]=="received" else "income_expected_minor"]+=event["amount_minor"]
            signed=event["amount_minor"]
        elif event["kind"]=="adjustment":
            totals["adjustments_minor"]+=event["amount_minor"]
            signed=event["amount_minor"]
        else:
            totals["payments_paid_minor" if event["status"]=="paid" else "payments_planned_minor"]+=event["amount_minor"]
            signed=-event["amount_minor"]
        running+=signed
        event["net_after_minor"]=running
        event["cash_effect_minor"]=signed
    return {"start_on":start_value,"end_on":end_value,"as_of":as_of.isoformat(),"events":events,"totals":totals,"net_minor":running,"opening":data["opening"],"balance_is_confirmed":False,"basis":"entered_payment_dates"}


def overview(user_id,start_value,end_value):
    data=read_data(user_id)
    result=calculate(data,start_value,end_value)
    result["rules"]=[{**rule,"revision":rule_revision(rule),"deletion_revision":managed_revision(data,"payment_plan",rule["id"]),"display_start_on":payment_due(data,rule,rule["start_on"]),"display_end_on":payment_due(data,rule,rule["end_on"]) if rule["end_on"] else None,"has_paid":any(row["recurring_id"]==rule["id"] for row in data["payment_events"]+data["occurrences"]),"scheduled_total_minor":str(sum(int(rule["amount_minor"]) for _ in payment_dates(rule,date.fromisoformat(start_value),date.fromisoformat(end_value))) if rule["status"]=="active" else 0)} for rule in data["recurring"]]
    result["income_rules"]=[{**row,"revision":managed_revision(data,"income_rule",row["id"]),"amount_minor":str(income_rule_price(data,row)),"has_receipts":any(item["rule_id"]==row["id"] and item["status"]=="received" for item in data["income_records"])} for row in data["income_rules"]]
    result["income_records"]=[{**row,"cash_recognized_on":row["expected_on"] if (data.get("opening") or {}).get("auto_income_on_due") and row["status"]=="expected" and row["expected_on"]<=today().isoformat() else None,"revision":managed_revision(data,"income_record",row["id"])} for row in data["income_records"]]
    result["income_rule_amounts"]=data["income_rule_amounts"]
    result['cash_position']=cash_position(data)
    projection=projected_balance(data,end_value)
    result['cash_position']['projected_balance_minor']=str(projection) if projection is not None else None
    result['cash_position']['projected_on']=end_value
    result['card_spending']=card_spending(data,start_value,end_value)
    result['extra_budget']=extra_budget(data,result,start_value,end_value)
    result['card_monthly_debt']=monthly_card_debt(data,start_value[:7])
    covered={(row['card_ref'],row['month']) for row in data.get('card_monthly_totals',[])}
    result['statements']=[{**row,'covered_by_monthly_total':('legacy:'+str(row['card_id']),row['payment_month']) in covered,**{k:str(row[k]) for k in ('total_minor','paid_minor','remaining_minor')}} for row in statement_balances(data)]
    result["net_minor"]=str(result["net_minor"])
    result["totals"]={key:str(value) for key,value in result["totals"].items()}
    for event in result["events"]:
        for key in ("amount_minor","net_after_minor","cash_effect_minor"):
            event[key]=str(event[key])
    return result


def initialize():
    # Referenced card definitions must exist before the additive relationship table.
    import profile_settings
    profile_settings.initialize()
    with core.connect() as db:
        pg=hasattr(db,"_conn")
        if pg:
            db.execute('ALTER TABLE cash_settings ADD COLUMN IF NOT EXISTS auto_income_on_due INTEGER NOT NULL DEFAULT 0 CHECK(auto_income_on_due IN (0,1))')
        elif 'auto_income_on_due' not in {row[1] for row in db.execute('PRAGMA table_info(cash_settings)')}:
            db.execute('ALTER TABLE cash_settings ADD COLUMN auto_income_on_due INTEGER NOT NULL DEFAULT 0 CHECK(auto_income_on_due IN (0,1))')
        identity="BIGSERIAL" if pg else "INTEGER"
        integer="BIGINT" if pg else "INTEGER"
        db.execute(f"""CREATE TABLE IF NOT EXISTS financial_record_deletions (
            user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            record_kind TEXT NOT NULL CHECK(record_kind IN ('income_record','income_rule','payment_plan')),
            record_id {integer} NOT NULL,token TEXT NOT NULL,deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,record_kind,record_id))""")
        if pg:
            db.execute('ALTER TABLE financial_record_deletions ENABLE ROW LEVEL SECURITY')
            for role in ('PUBLIC','anon','authenticated'):
                if role=='PUBLIC' or db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON financial_record_deletions FROM {role}')
        db.execute(f"""CREATE TABLE IF NOT EXISTS payment_events (
            id {identity} PRIMARY KEY,user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recurring_id {integer} NOT NULL,scheduled_on TEXT NOT NULL,paid_on TEXT NOT NULL,
            amount_minor {integer} NOT NULL CHECK(amount_minor>0),idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,idempotency_key),
            FOREIGN KEY(recurring_id,user_id) REFERENCES recurring_expenses(id,user_id) ON DELETE CASCADE)""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS payment_plan_details (
            recurring_id {integer} PRIMARY KEY,user_id {integer} NOT NULL,
            entry_kind TEXT NOT NULL CHECK(entry_kind IN ('installment','expense','regular')),
            legacy_card_id {integer},profile_card_id {integer},purchased_on TEXT,
            CHECK(legacy_card_id IS NULL OR profile_card_id IS NULL),
            FOREIGN KEY(recurring_id,user_id) REFERENCES recurring_expenses(id,user_id) ON DELETE CASCADE,
            FOREIGN KEY(legacy_card_id,user_id) REFERENCES credit_cards(id,user_id),
            FOREIGN KEY(profile_card_id,user_id) REFERENCES profile_cards(id,user_id))""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS card_monthly_totals (
            user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            card_ref TEXT NOT NULL,month TEXT NOT NULL,amount_minor {integer} NOT NULL CHECK(amount_minor>=0),
            version {integer} NOT NULL DEFAULT 1,due_on TEXT,due_date_explicit INTEGER NOT NULL DEFAULT 0 CHECK(due_date_explicit IN (0,1)),PRIMARY KEY(user_id,card_ref,month))""")
        if pg:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN IF NOT EXISTS due_day_offset INTEGER NOT NULL DEFAULT 0 CHECK(due_day_offset BETWEEN -5 AND 5)')
        elif 'due_day_offset' not in {row[1] for row in db.execute('PRAGMA table_info(card_monthly_totals)')}:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN due_day_offset INTEGER NOT NULL DEFAULT 0 CHECK(due_day_offset BETWEEN -5 AND 5)')
        if pg:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN IF NOT EXISTS due_on TEXT')
        elif 'due_on' not in {row[1] for row in db.execute('PRAGMA table_info(card_monthly_totals)')}:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN due_on TEXT')
        if pg:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN IF NOT EXISTS due_date_explicit INTEGER NOT NULL DEFAULT 0')
        elif 'due_date_explicit' not in {row[1] for row in db.execute('PRAGMA table_info(card_monthly_totals)')}:
            db.execute('ALTER TABLE card_monthly_totals ADD COLUMN due_date_explicit INTEGER NOT NULL DEFAULT 0')
        db.execute(f"""CREATE TABLE IF NOT EXISTS card_monthly_payments (
            id {identity} PRIMARY KEY,user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            card_ref TEXT NOT NULL,month TEXT NOT NULL,amount_minor {integer} NOT NULL CHECK(amount_minor>0),
            paid_on TEXT NOT NULL,idempotency_key TEXT NOT NULL,UNIQUE(user_id,idempotency_key),
            FOREIGN KEY(user_id,card_ref,month) REFERENCES card_monthly_totals(user_id,card_ref,month) ON DELETE CASCADE)""")
        db.execute('CREATE INDEX IF NOT EXISTS card_monthly_payments_owner_month ON card_monthly_payments(user_id,card_ref,month)')
        if pg:
            db.execute('ALTER TABLE card_monthly_payments ENABLE ROW LEVEL SECURITY')
            for role in ('PUBLIC','anon','authenticated'):
                if role=='PUBLIC' or db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON card_monthly_payments FROM {role}')
                    db.execute(f'REVOKE ALL ON SEQUENCE card_monthly_payments_id_seq FROM {role}')
            db.execute('ALTER TABLE card_monthly_totals ENABLE ROW LEVEL SECURITY')
            for role in ('PUBLIC','anon','authenticated'):
                if role=='PUBLIC' or db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON card_monthly_totals FROM {role}')
        db.execute('CREATE INDEX IF NOT EXISTS payment_details_user_card ON payment_plan_details(user_id,profile_card_id,legacy_card_id)')
        db.execute("CREATE INDEX IF NOT EXISTS payment_events_user_date ON payment_events(user_id,paid_on)")
        db.execute("CREATE INDEX IF NOT EXISTS payment_events_occurrence ON payment_events(user_id,recurring_id,scheduled_on)")
        for table in ("recurring_expenses","income_records","income_rules","card_repayments"):
            if pg:
                db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS client_request_key TEXT")
            elif "client_request_key" not in {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN client_request_key TEXT")
            db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_client_request ON {table}(user_id,client_request_key) WHERE client_request_key IS NOT NULL")
        if pg:
            db.execute('ALTER TABLE payment_plan_details ENABLE ROW LEVEL SECURITY')
            for role in ('PUBLIC','anon','authenticated'):
                if role=='PUBLIC' or db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON payment_plan_details FROM {role}')
            db.execute("ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY")
            db.execute("REVOKE ALL ON payment_events FROM PUBLIC")
            db.execute("REVOKE ALL ON SEQUENCE payment_events_id_seq FROM PUBLIC")
            for role in ("anon","authenticated"):
                if db.execute("SELECT 1 FROM pg_roles WHERE rolname=?",(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON payment_events FROM "{role}"')
                    db.execute(f'REVOKE ALL ON SEQUENCE payment_events_id_seq FROM "{role}"')


def record_payment(user_id,rule_id,payload):
    import re
    key=payload.get("request_key","")
    if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",key):
        raise ValueError("Ödeme isteği doğrulanamadı; formu yeniden açın.")
    scheduled=date.fromisoformat(payload.get("scheduled_on",""))
    paid=date.fromisoformat(payload.get("paid_on",""))
    if scheduled.isoformat()!=payload.get("scheduled_on") or paid.isoformat()!=payload.get("paid_on") or paid>today():
        raise ValueError("Gerçek ödeme tarihi geçerli olmalı ve gelecekte olamaz.")
    amount=core.money_to_minor(payload.get("amount"))
    if amount<=0:
        raise ValueError("Ödeme tutarı sıfırdan büyük olmalı.")
    with core.LOCK,core.connect() as db:
        _write_lock(db,'recurring_expenses',user_id,rule_id)
        _assert_visible(db,user_id,'payment_plan',rule_id)
        prior=db.execute("SELECT * FROM payment_events WHERE user_id=? AND idempotency_key=?",(user_id,key)).fetchone()
        if prior:
            if (prior["recurring_id"],prior["scheduled_on"],prior["paid_on"],prior["amount_minor"])!=(rule_id,scheduled.isoformat(),paid.isoformat(),amount):
                raise ValueError("Aynı ödeme isteği farklı bilgilerle tekrar kullanılamaz.")
            return {"id":prior["id"],"created":False}
        suffix=" FOR UPDATE" if hasattr(db,"_conn") else ""
        rule=db.execute("SELECT * FROM recurring_expenses WHERE id=? AND user_id=?"+suffix,(rule_id,user_id)).fetchone()
        if not rule or rule["status"]!="active":
            raise ValueError("Aktif ödeme planı bulunamadı.")
        detail=db.execute('SELECT entry_kind FROM payment_plan_details WHERE recurring_id=? AND user_id=?',(rule_id,user_id)).fetchone()
        if scheduled not in payment_dates({**dict(rule),'entry_kind':detail['entry_kind'] if detail else 'installment'},scheduled,scheduled):
            raise ValueError("Tarih bu ödeme planında yer almıyor.")
        legacy=db.execute("SELECT 1 FROM recurring_occurrences o JOIN expenses e ON e.id=o.expense_id AND e.user_id=o.user_id WHERE o.user_id=? AND o.recurring_id=? AND o.scheduled_on=?",(user_id,rule_id,scheduled.isoformat())).fetchone()
        already=int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM payment_events WHERE user_id=? AND recurring_id=? AND scheduled_on=?",(user_id,rule_id,scheduled.isoformat())).fetchone()[0])
        if legacy or already+amount>rule["amount_minor"]:
            raise ValueError("Ödeme, bu taksidin kalan tutarını aşıyor veya taksit zaten ödenmiş.")
        row=db.execute("INSERT INTO payment_events(user_id,recurring_id,scheduled_on,paid_on,amount_minor,idempotency_key) VALUES(?,?,?,?,?,?)",(user_id,rule_id,scheduled.isoformat(),paid.isoformat(),amount,key))
        return {"id":row.lastrowid,"created":True}


class Conflict(ValueError):
    pass


def _key(payload):
    import re
    value=payload.get("request_key","")
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",value):
        raise ValueError("Kayıt isteği doğrulanamadı. Formu yeniden açın.")
    return value


def _write_lock(db,table,user_id,record_id=None):
    if not hasattr(db,"_conn"):
        db.execute("BEGIN IMMEDIATE")
    else:
        # Serialize creations for the same user; row locks also protect edits.
        db.execute("SELECT id FROM users WHERE id=? FOR UPDATE",(user_id,)).fetchone()


def rule_revision(rule):
    import hashlib,json
    fields=[rule.get(key) for key in ("id","description","amount_minor","frequency","start_on","end_on","status","category_id","payment_method_id","entry_kind","card_ref","purchased_on")]
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def save_payment_plan(user_id,payload):
    import profile_settings
    description=payload.get('description','')
    if not isinstance(description,str) or not 1<=len(description.strip())<=160:
        raise ValueError('Açıklama 1–160 karakter olmalı.')
    amount=core.money_to_minor(payload.get('amount'))
    if amount<=0:raise ValueError('Ödeme tutarı sıfırdan büyük olmalı.')
    record_id=core.record_id(payload.get('id'))
    key=None if record_id else _key(payload)
    with core.LOCK,core.connect() as db:
        _write_lock(db,'recurring_expenses',user_id,record_id)
        old=db.execute('SELECT * FROM recurring_expenses WHERE id=? AND user_id=?',(record_id,user_id)).fetchone() if record_id else None
        if record_id and (not old or old['status']=='cancelled'):raise ValueError('Ödeme planı bulunamadı.')
        if record_id:_assert_visible(db,user_id,'payment_plan',record_id)
        old_detail=db.execute('SELECT * FROM payment_plan_details WHERE recurring_id=? AND user_id=?',(record_id,user_id)).fetchone() if old else None
        methods=[dict(row) for row in db.execute('SELECT id,name FROM payment_methods WHERE user_id=?',(user_id,))]
        old_meta=_detail_fields(old_detail,legacy_entry_kind(dict(old),methods) if old else 'installment')
        kind=payload.get('entry_kind',old_meta['entry_kind'])
        if kind not in ('expense','installment','regular'):raise ValueError('Ödeme türünü kontrol edin.')
        reference=payload.get('card_ref',old_meta['card_ref']) or ''
        card=profile_settings.resolve_card(db,user_id,reference) if reference else None
        if card and not card['active'] and reference!=old_meta['card_ref']:raise ValueError('Pasif kartla yeni işlem eklenemez.')
        if not old and card and not card['active']:raise ValueError('Pasif kartla yeni işlem eklenemez.')
        if kind=='installment' and payload.get('entry_kind')=='installment' and not old and not reference:raise ValueError('Aylık taksit için kredi kartını seçin.')
        purchase=None
        if kind=='expense':
            if not card:raise ValueError('Harcama için bir kredi kartı seçin.')
            purchase=date.fromisoformat(payload.get('purchased_on',''))
            if purchase.isoformat()!=payload.get('purchased_on') or purchase>today():raise ValueError('Alışveriş tarihi gelecekte olamaz.')
            first=date.fromisoformat(card_schedule.next_due(card,purchase))
            if first<purchase:raise ValueError('Kartın son ödeme tarihi alışverişten önce. Kart ayarındaki vadeyi güncelleyin.')
            if payload.get('start_on') and payload['start_on']!=first.isoformat():raise Conflict('Kartın son ödeme tarihi değişmiş. Kartı yeniden seçin.')
            last=first;frequency='monthly'
        else:
            first=date.fromisoformat(payload.get('start_on',''))
            last=date.fromisoformat(payload['end_on']) if payload.get('end_on') else None
            if payload.get('entry_kind') in ('installment','regular') and not last:raise ValueError('Ödemenin bitiş tarihini girin.')
            frequency=payload.get('frequency','monthly')
            if kind=='regular' and frequency not in ('monthly','weekly'):raise ValueError('Banka / nakit ödemesi aylık veya haftalık olabilir.')
        if first.isoformat()!=(payload.get('start_on') or first.isoformat()):raise ValueError('İlk ödeme tarihini kontrol edin.')
        if last and last<first:raise ValueError(f'Son ödeme tarihi ({last:%d.%m.%Y}), ilk ödeme tarihinden ({first:%d.%m.%Y}) önce olamaz. Bitiş yılını kontrol edin.')
        if frequency not in ('monthly','weekly','yearly'):raise ValueError('Ödeme sıklığını kontrol edin.')
        category=core.record_id(payload.get('category_id',old['category_id'] if old else None))
        method=core.record_id(payload.get('payment_method_id',old['payment_method_id'] if old else None))
        for table,value in (('categories',category),('payment_methods',method)):
            if value and not db.execute(f'SELECT 1 FROM {table} WHERE id=? AND user_id=?',(value,user_id)).fetchone():raise ValueError('Ödeme bilgileri hesabınıza ait olmalı.')
        values=(description.strip(),amount,frequency,first.isoformat(),last.isoformat() if last else None,category,method)
        meta={'entry_kind':kind,'card_ref':reference,'purchased_on':purchase.isoformat() if purchase else None}
        names=('description','amount_minor','frequency','start_on','end_on','category_id','payment_method_id')
        if old:
            previous=tuple(old[name] for name in names)
            if previous==values and old_meta==meta:return {'id':record_id}
            if payload.get('revision')!=rule_revision({**dict(old),**old_meta}):raise Conflict('Bu plan başka bir işlemde değişti. Güncel bilgileri yükleyip yeniden düzenleyin.')
            has_paid=db.execute('SELECT 1 FROM payment_events WHERE user_id=? AND recurring_id=?',(user_id,record_id)).fetchone() or db.execute('SELECT 1 FROM recurring_occurrences o JOIN expenses e ON e.id=o.expense_id AND e.user_id=o.user_id WHERE o.user_id=? AND o.recurring_id=?',(user_id,record_id)).fetchone()
            if has_paid and (previous[1:5]!=values[1:5] or old_meta!=meta):raise ValueError('Ödemesi kaydedilmiş planın tutarı, tarihi, kartı veya türü değiştirilemez; geçmiş ödemeler korunur.')
            db.execute('UPDATE recurring_expenses SET description=?,amount_minor=?,frequency=?,start_on=?,end_on=?,category_id=?,payment_method_id=? WHERE id=? AND user_id=?',(*values,record_id,user_id))
        else:
            prior=db.execute('SELECT * FROM recurring_expenses WHERE user_id=? AND client_request_key=?',(user_id,key)).fetchone()
            if prior:
                prior_detail=db.execute('SELECT * FROM payment_plan_details WHERE recurring_id=? AND user_id=?',(prior['id'],user_id)).fetchone()
                if tuple(prior[name] for name in names)!=values or _detail_fields(prior_detail)!=meta:raise Conflict('Aynı kayıt isteği farklı bilgilerle kullanılamaz.')
                return {'id':prior['id']}
            record_id=db.execute('INSERT INTO recurring_expenses(user_id,description,amount_minor,frequency,start_on,end_on,category_id,payment_method_id,client_request_key) VALUES(?,?,?,?,?,?,?,?,?)',(user_id,*values,key)).lastrowid
        legacy_id=card['record_id'] if card and card['kind']=='legacy' else None
        profile_id=card['record_id'] if card and card['kind']=='card' else None
        db.execute('INSERT INTO payment_plan_details(recurring_id,user_id,entry_kind,legacy_card_id,profile_card_id,purchased_on) VALUES(?,?,?,?,?,?) ON CONFLICT(recurring_id) DO UPDATE SET entry_kind=excluded.entry_kind,legacy_card_id=excluded.legacy_card_id,profile_card_id=excluded.profile_card_id,purchased_on=excluded.purchased_on',(record_id,user_id,kind,legacy_id,profile_id,meta['purchased_on']))
        return {'id':record_id}


def _detail_fields(row,fallback='installment'):
    return {'entry_kind':row['entry_kind'] if row else fallback,'card_ref':('legacy:'+str(row['legacy_card_id'])) if row and row['legacy_card_id'] else ('card:'+str(row['profile_card_id'])) if row and row['profile_card_id'] else '', 'purchased_on':row['purchased_on'] if row else None}


def save_income_once(user_id,payload):
    key=_key(payload)
    description,income_type,expected,note,amount,category=core._valid_income_fields(payload)
    status=payload.get("status","expected")
    if status not in ("expected","received") or status=="received" and expected>today():
        raise ValueError("Alınmış gelir gelecekte olamaz; tarihi ve durumu kontrol edin.")
    received=expected.isoformat() if status=="received" else None
    with core.LOCK,core.connect() as db:
        _write_lock(db,"income_records",user_id)
        if category and not db.execute("SELECT 1 FROM categories WHERE id=? AND user_id=?",(category,user_id)).fetchone():
            raise ValueError("Kategori hesabınıza ait olmalı.")
        previous=db.execute("SELECT * FROM income_records WHERE user_id=? AND client_request_key=?",(user_id,key)).fetchone()
        fields=(description,income_type,amount,expected.isoformat(),status,received)
        if previous:
            if tuple(previous[name] for name in ("description","income_type","amount_minor","expected_on","status","received_on"))!=fields:
                raise Conflict("Aynı kayıt isteği farklı bilgilerle kullanılamaz.")
            return {"id":previous["id"]}
        row=db.execute("INSERT INTO income_records(user_id,description,income_type,amount_minor,expected_on,status,received_on,category_id,note,client_request_key) VALUES(?,?,?,?,?,?,?,?,?,?)",(user_id,*fields,category,note,key))
        return {"id":row.lastrowid}


def receive_income(user_id,payload):
    received=date.fromisoformat(payload.get("received_on",""))
    if received.isoformat()!=payload.get("received_on") or received>today():
        raise ValueError("Gelirin alınma tarihi gelecekte olamaz.")
    record_id=core.record_id(payload.get("record_id"))
    rule_id=core.record_id(payload.get("rule_id"))
    with core.LOCK,core.connect() as db:
        _write_lock(db,"income_records",user_id)
        if record_id:
            _assert_visible(db,user_id,'income_record',record_id)
            row=db.execute("SELECT * FROM income_records WHERE id=? AND user_id=?",(record_id,user_id)).fetchone()
            if not row or row["status"]=="cancelled":
                raise ValueError("Gelir kaydı bulunamadı.")
            if row["rule_id"] is not None:_assert_visible(db,user_id,"income_rule",row["rule_id"])
            amount=row["amount_minor"]
        else:
            scheduled=date.fromisoformat(payload.get("scheduled_on",""))
            rule=db.execute("SELECT * FROM income_rules WHERE id=? AND user_id=?",(rule_id,user_id)).fetchone()
            if not rule or rule["status"]!="active":
                raise ValueError("Aktif gelir kuralı bulunamadı.")
            _assert_visible(db,user_id,'income_rule',rule_id)
            first=date.fromisoformat(rule["start_on"])
            index=core.income_recurrence_index(first,rule["frequency"],scheduled,rule["schedule_mode"])
            if scheduled<first or core.income_recurrence_date(first,rule["frequency"],index,rule["schedule_mode"])!=scheduled or rule["end_on"] and scheduled.isoformat()>rule["end_on"]:
                raise ValueError("Tarih gelir planına ait değil.")
            price=db.execute("SELECT amount_minor FROM income_rule_amounts WHERE rule_id=? AND user_id=? AND effective_on<=? ORDER BY effective_on DESC LIMIT 1",(rule_id,user_id,scheduled.isoformat())).fetchone()
            if not price:
                raise ValueError("Bu tarih için gelir tutarı bulunamadı.")
            amount=price[0]
            row=db.execute("SELECT * FROM income_records WHERE rule_id=? AND user_id=? AND expected_on=?",(rule_id,user_id,scheduled.isoformat())).fetchone()
            if row:
                _assert_visible(db,user_id,'income_record',row['id'])
                if row['status']=='cancelled':raise ValueError('Gelir kaydı silinmiş veya iptal edilmiş.')
                record_id=row["id"]
            else:
                if str(payload.get("expected_amount_minor",amount))!=str(amount):
                    raise Conflict("Gelir tutarı değişmiş. Güncel kaydı kontrol edin.")
                record_id=db.execute("INSERT INTO income_records(user_id,rule_id,category_id,income_type,description,amount_minor,expected_on,status,received_on) VALUES(?,?,?,?,?,?,?,'received',?)",(user_id,rule_id,rule["category_id"],rule["income_type"],rule["description"],amount,scheduled.isoformat(),received.isoformat())).lastrowid
                return {"id":record_id}
        if str(payload.get("expected_amount_minor",amount))!=str(amount):
            raise Conflict("Gelir tutarı değişmiş. Güncel kaydı kontrol edin.")
        if row["status"]=="received":
            if row["received_on"]!=received.isoformat():
                raise Conflict("Bu gelir farklı bir tarihte alınmış olarak kayıtlı.")
            return {"id":record_id}
        db.execute("UPDATE income_records SET status='received',received_on=?,amount_minor=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(received.isoformat(),amount,record_id,user_id))
        return {"id":record_id}


def save_monthly_income(user_id,payload):
    key=_key(payload)
    frequency=payload.get('frequency','monthly')
    mode=payload.get('schedule_mode','anchored')
    if frequency not in ('monthly','weekly') or frequency=='weekly' and mode!='anchored':raise ValueError('Haftalık gelir için ilk gelir gününü seçin; ay sonu yalnızca aylık gelirde kullanılabilir.')
    payload=dict(payload)
    if mode=='last_day':
        month=payload.get('start_month') or (payload.get('expected_on') or payload.get('start_on') or today().isoformat())[:7]
        try:
            first_month=date.fromisoformat(month+'-01')
            payload['expected_on']=core._month_close(first_month.year,first_month.month,31).isoformat()
        except (ValueError,TypeError):raise ValueError('Başlangıç ayını kontrol edin.') from None
    description,income_type,first,note,amount,category=core._valid_income_fields(payload)
    mode=payload.get("schedule_mode","anchored")
    if mode not in ("anchored","last_day"):
        raise ValueError("Gelirin tekrar gününü kontrol edin.")
    end=date.fromisoformat(payload["end_on"]) if payload.get("end_on") else None
    if end and (end<first or end.isoformat()!=payload["end_on"]):
        raise ValueError("Gelirin son tarihini kontrol edin.")
    fields=(description,income_type,category,frequency,mode,first.isoformat(),end.isoformat() if end else None)
    with core.LOCK,core.connect() as db:
        _write_lock(db,"income_rules",user_id)
        if category and not db.execute("SELECT 1 FROM categories WHERE id=? AND user_id=?",(category,user_id)).fetchone():
            raise ValueError("Kategori hesabınıza ait olmalı.")
        previous=db.execute("SELECT * FROM income_rules WHERE user_id=? AND client_request_key=?",(user_id,key)).fetchone()
        if previous:
            price=db.execute("SELECT amount_minor FROM income_rule_amounts WHERE user_id=? AND rule_id=? AND effective_on=?",(user_id,previous["id"],first.isoformat())).fetchone()
            if tuple(previous[name] for name in ("description","income_type","category_id","frequency","schedule_mode","start_on","end_on"))!=fields or not price or price[0]!=amount:
                raise Conflict("Aynı gelir isteği farklı bilgilerle kullanılamaz.")
            return {"id":previous["id"]}
        rule=db.execute("INSERT INTO income_rules(user_id,description,income_type,category_id,frequency,schedule_mode,start_on,end_on,client_request_key) VALUES(?,?,?,?,?,?,?,?,?)",(user_id,*fields,key)).lastrowid
        db.execute("INSERT INTO income_rule_amounts(user_id,rule_id,effective_on,amount_minor) VALUES(?,?,?,?)",(user_id,rule,first.isoformat(),amount))
        return {"id":rule}


def statement_balances(data):
    """Known statement liabilities, not purchase-date cash outflows. Reads only."""
    cards={row['id']:row for row in data.get('cards',[])}
    installment_expenses={row['expense_id'] for row in data.get('installment_plans',[])}
    linked_expenses={row['expense_id'] for row in data.get('occurrences',[]) if row.get('expense_id') is not None}
    general={}
    for payment in data.get('repayments',[]):
        if payment['statement_id'] is None:general[payment['card_id']]=general.get(payment['card_id'],0)+payment['amount_minor']
    # A general card payment settles known opening debt first, then oldest statement.
    for card_id,card in cards.items():general[card_id]=max(0,general.get(card_id,0)-int(card.get('opening_balance_minor',0)))
    result=[]
    for statement in sorted(data.get('statements',[]),key=lambda row:(row['due_on'],row['closing_on'],row['id'])):
        purchase=sum(row['amount_minor'] for row in data.get('expenses',[]) if row['statement_id']==statement['id'] and row['card_id']==statement['card_id'] and row['id'] not in installment_expenses and row['id'] not in linked_expenses)
        installments=sum(row['amount_minor'] for row in data.get('installment_occurrences',[]) if row['statement_id']==statement['id'] and row['status']!='cancelled')
        total=purchase+installments
        direct=sum(row['amount_minor'] for row in data.get('repayments',[]) if row['statement_id']==statement['id'] and row['card_id']==statement['card_id'])
        remaining=max(0,total-direct)
        allocated=min(remaining,general.get(statement['card_id'],0));general[statement['card_id']]=general.get(statement['card_id'],0)-allocated
        card=cards.get(statement['card_id'],{})
        due=statement['due_on']
        if remaining-allocated:
            ref='legacy:'+str(statement['card_id']);period=due[:7]
            total_row=next((row for row in data.get('card_monthly_totals',[]) if row['card_ref']==ref and row['month']==period),{})
            configured=configured_card_due(data,ref,period)
            if configured:due=card_due_for_month(configured_card(data,ref),period,monthly_due_offset(data,total_row) if total_row else 0)
        result.append({'id':statement['id'],'card_id':statement['card_id'],'due_on':due,'payment_month':statement['due_on'][:7],'total_minor':total,'paid_minor':min(total,direct+allocated),'remaining_minor':remaining-allocated,'description':(card.get('display_name') or 'Kredi kartı')+' · Ekstre ödemesi'})
    return result


def cash_position(data,as_of=None):
    as_of=(as_of or today()).isoformat()
    opening=data.get('opening')
    if not opening or opening['effective_on']>as_of:return {'configured':bool(opening),'balance_minor':None,'as_of':as_of,'effective_on':opening['effective_on'] if opening else None,'opening_balance_minor':str(opening['opening_balance_minor']) if opening else None}
    effective=opening['effective_on']
    in_range=lambda value: bool(value) and effective<=value<=as_of
    recognized=income_events(data,effective,as_of,date.fromisoformat(as_of))
    received=sum(row['amount_minor'] for row in recognized if row['status']=='received')
    automatic=sum(row['amount_minor'] for row in recognized if row.get('auto_received'))
    expenses={row['id']:row for row in data['expenses']}
    linked={row['expense_id'] for row in data['occurrences'] if row['expense_id'] in expenses}
    paid=sum(row['amount_minor'] for row in data['expenses'] if row['id'] not in linked and row['card_id'] is None and in_range(row['spent_on']))
    paid+=sum(expenses[row['expense_id']]['amount_minor'] for row in data['occurrences'] if row['expense_id'] in expenses and in_range(row['paid_on']))
    paid+=sum(row['amount_minor'] for row in data.get('payment_events',[]) if in_range(row['paid_on']))
    paid+=sum(row['amount_minor'] for row in data['repayments'] if in_range(row['paid_on']))
    paid+=sum(row['amount_minor'] for row in data.get('card_monthly_payments',[]) if in_range(row['paid_on']))
    adjustments=sum(row['amount_minor'] for row in data['adjustments'] if in_range(row['adjustment_on']))
    return {'configured':True,'as_of':as_of,'effective_on':effective,'opening_balance_minor':str(opening['opening_balance_minor']),'revision':opening_revision(opening),'balance_minor':str(opening['opening_balance_minor']+received-paid+adjustments),'received_minor':str(received),'automatic_income_minor':str(automatic),'auto_income_on_due':bool(opening.get('auto_income_on_due')),'paid_minor':str(paid)}


def opening_revision(opening):
    import hashlib
    basis=f"{opening['opening_balance_minor']}:{opening['effective_on']}"
    if dict(opening).get('auto_income_on_due'):basis+=':scheduled'
    return hashlib.sha256(basis.encode()).hexdigest()


def save_opening(user_id,payload):
    amount=core.money_to_minor(payload.get('amount'))
    effective=date.fromisoformat(payload.get('effective_on',''))
    if amount<0 or effective>today() or effective.isoformat()!=payload.get('effective_on'):raise ValueError('Başlangıç bakiyesi ve tarihini kontrol edin; gelecek gün seçilemez.')
    with core.LOCK,core.connect() as db:
        _write_lock(db,'cash_settings',user_id)
        old=db.execute('SELECT opening_balance_minor,effective_on,auto_income_on_due FROM cash_settings WHERE user_id=?',(user_id,)).fetchone()
        mode=payload.get('auto_income_on_due',old['auto_income_on_due'] if old else 0)
        if isinstance(mode,bool):mode=int(mode)
        if not isinstance(mode,int) or mode not in (0,1):raise ValueError('Gelirin tarihte otomatik eklenmesi seçimini kontrol edin.')
        if old:
            if (old['opening_balance_minor'],old['effective_on'],old['auto_income_on_due'])!=(amount,effective.isoformat(),mode):
                if payload.get('revision')!=opening_revision(old):raise Conflict('Başlangıç bakiyesi değişmiş. Güncel bilgiyi yükleyip yeniden düzenleyin.')
                db.execute('UPDATE cash_settings SET opening_balance_minor=?,effective_on=?,auto_income_on_due=? WHERE user_id=?',(amount,effective.isoformat(),mode,user_id))
        else:db.execute('INSERT INTO cash_settings(user_id,opening_balance_minor,effective_on,auto_income_on_due) VALUES(?,?,?,?)',(user_id,amount,effective.isoformat(),mode))
    return {'ok':True}


def record_statement_payment(user_id,payload):
    key=_key(payload)
    statement_id=core.record_id(payload.get('statement_id'))
    paid=date.fromisoformat(payload.get('paid_on',''))
    amount=core.money_to_minor(payload.get('amount'))
    if not statement_id or amount<=0 or paid>today() or paid.isoformat()!=payload.get('paid_on'):raise ValueError('Ekstre ödeme tarihi ve tutarını kontrol edin.')
    with core.LOCK,core.connect() as db:
        _write_lock(db,'card_repayments',user_id)
        old=db.execute('SELECT * FROM card_repayments WHERE user_id=? AND client_request_key=?',(user_id,key)).fetchone()
        if old:
            if (old['statement_id'],old['amount_minor'],old['paid_on'])!=(statement_id,amount,paid.isoformat()):raise Conflict('Aynı ödeme isteği farklı bilgilerle kullanılamaz.')
            return {'id':old['id'],'created':False}
        statement=db.execute('SELECT * FROM card_statements WHERE id=? AND user_id=?',(statement_id,user_id)).fetchone()
        if not statement:raise ValueError('Ekstre hesabınıza ait değil veya bulunamadı.')
        # Same user lock serializes other writes; avoid opening a second transaction.
        data={}
        for key_name,table in (('cards','credit_cards'),('statements','card_statements'),('expenses','expenses'),('repayments','card_repayments'),('installment_plans','installment_plans'),('installment_occurrences','installment_occurrences'),('occurrences','recurring_occurrences')):
            data[key_name]=[dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE user_id=?',(user_id,))]
        remaining=next(row['remaining_minor'] for row in statement_balances(data) if row['id']==statement_id)
        if amount>remaining:raise ValueError('Ödeme ekstrenin kalan tutarını aşıyor.')
        result=db.execute('INSERT INTO card_repayments(user_id,card_id,statement_id,amount_minor,paid_on,note,client_request_key) VALUES(?,?,?,?,?,?,?)',(user_id,statement['card_id'],statement_id,amount,paid.isoformat(),'Ekstre ödemesi',key))
        return {'id':result.lastrowid,'created':True}


def projected_balance(data,end_value,as_of=None):
    """Current recorded cash plus still-unsettled inflows/outflows up to the end day."""
    as_of=as_of or today();end=date.fromisoformat(end_value)
    position=cash_position(data,as_of)
    if position['balance_minor'] is None or end<as_of:return None
    candidates=[as_of.isoformat()]
    for key,field in (('recurring','start_on'),('income_rules','start_on'),('income_records','expected_on'),('statements','due_on'),('card_monthly_totals','due_on')):
        if key in ('income_rules','income_records') and (data.get('opening') or {}).get('auto_income_on_due'):
            continue  # Past scheduled income is already recognized by cash_position.
        candidates.extend(row[field] for row in data.get(key,[]) if row.get(field) and row[field]<=end_value)
    first=min(candidates)
    if (end-date.fromisoformat(first)).days>3660:return None
    pending=calculate(data,first,end_value,as_of)
    changes=sum(row['cash_effect_minor'] for row in pending['events'] if row['status'] not in ('received','paid','completed'))
    return int(position['balance_minor'])+changes


def legacy_entry_kind(rule,methods):
    method=next((row['name'].casefold() for row in methods if row['id']==rule.get('payment_method_id')),'')
    return 'regular' if method in ('nakit','banka kartı') else 'installment'


def card_spending(data,start_value,end_value):
    """Purchase-month extras by owned card, independent of settlement cashflow."""
    groups={}
    for card in data.get('cards',[]):
        groups['legacy:'+str(card['id'])]={'card_ref':'legacy:'+str(card['id']),'bank_name':card['bank_name'],'display_name':card['display_name'],'active':card['active'],'entries':[]}
    for card in data.get('profile_cards',[]):
        if card['legacy_card_id'] is None:groups['card:'+str(card['id'])]={'card_ref':'card:'+str(card['id']),'bank_name':card['bank_name'],'display_name':card['display_name'],'active':card['active'],'entries':[]}
    planned_installments={row['expense_id'] for row in data.get('installment_plans',[])}
    recurring_expenses={row['expense_id'] for row in data.get('occurrences',[]) if row.get('expense_id') is not None}
    statements={row['id']:row for row in data.get('statements',[])}
    for expense in data.get('expenses',[]):
        ref='legacy:'+str(expense['card_id'])
        if expense['card_id'] is None or ref not in groups or expense['id'] in planned_installments or expense['id'] in recurring_expenses or expense.get('installments_count',1)>1:continue
        if start_value<=expense['spent_on']<=end_value:
            groups[ref]['entries'].append({'id':'expense:'+str(expense['id']),'description':expense['description'],'purchased_on':expense['spent_on'],'payment_due_on':statements.get(expense['statement_id'],{}).get('due_on'),'amount_minor':str(expense['amount_minor'])})
    for rule in data.get('recurring',[]):
        ref=rule.get('card_ref')
        if rule.get('entry_kind')!='expense' or rule['status']=='cancelled' or ref not in groups or not rule.get('purchased_on'):continue
        if start_value<=rule['purchased_on']<=end_value:
            groups[ref]['entries'].append({'id':'plan:'+str(rule['id']),'description':rule['description'],'purchased_on':rule['purchased_on'],'payment_due_on':rule['start_on'],'amount_minor':str(rule['amount_minor'])})
    cards=[]
    for group in groups.values():
        group['entries'].sort(key=lambda row:(row['purchased_on'],row['id']))
        group['count']=len(group['entries']);group['total_minor']=str(sum(int(row['amount_minor']) for row in group['entries']))
        if group['active'] or group['count']:cards.append(group)
    cards.sort(key=lambda row:(row['bank_name'].casefold(),row['display_name'].casefold()))
    return {'cards':cards,'total_minor':str(sum(int(row['total_minor']) for row in cards)),'count':sum(row['count'] for row in cards),'start_on':start_value,'end_on':end_value}


def extra_budget(data,view,start_value,end_value):
    """Income envelope for variable purchases; never added again to cashflow totals."""
    rules={row['id']:row for row in data.get('recurring',[])}
    first,last=date.fromisoformat(start_value),date.fromisoformat(end_value)
    fixed=sum(int(rule['amount_minor']) for rule in rules.values() if rule['status']=='active' and rule.get('entry_kind','installment')!='expense' for _ in payment_dates(rule,first,last))
    statement_dates={row['id']:row['due_on'] for row in data.get('statements',[])}
    fixed+=sum(row['amount_minor'] for row in data.get('installment_occurrences',[]) if row['status']!='cancelled' and row.get('statement_id') in statement_dates and start_value<=statement_dates[row['statement_id']]<=end_value)
    income=int(view['totals']['income_received_minor'])+int(view['totals']['income_expected_minor'])
    extras=int(card_spending(data,start_value,end_value)['total_minor'])
    allowance=income-fixed
    return {key:str(value) for key,value in {'income_minor':income,'fixed_minor':fixed,'allowance_minor':max(0,allowance),'extra_spent_minor':extras,'remaining_minor':allowance-extras,'excess_minor':max(0,extras-max(0,allowance)),'fixed_gap_minor':max(0,-allowance)}.items()}


def monthly_card_debt(data,month):
    from calendar import monthrange
    first=date.fromisoformat(month+'-01');last=first.replace(day=monthrange(first.year,first.month)[1])
    cards=card_spending(data,first.isoformat(),last.isoformat())['cards']
    saved={row['card_ref']:row for row in data.get('card_monthly_totals',[]) if row['month']==month}
    statements=statement_balances(data)
    unassigned=[]
    fixed={card['card_ref']:{'installment_minor':0,'regular_minor':0} for card in cards}
    for rule in data.get('recurring',[]):
        if rule['status']!='active' or rule.get('entry_kind')=='expense':continue
        amount=sum(int(rule['amount_minor']) for _ in payment_dates(rule,first,last))
        ref=rule.get('card_ref')
        if ref in fixed:fixed[ref]['regular_minor' if rule.get('entry_kind')=='regular' else 'installment_minor']+=amount
        elif amount and rule.get('entry_kind','installment')=='installment':unassigned.append({'id':rule['id'],'description':rule['description'],'amount_minor':str(amount)})
    statements_by_id={row['id']:row for row in data.get('statements',[])}
    for occurrence in data.get('installment_occurrences',[]):
        statement=statements_by_id.get(occurrence.get('statement_id'))
        if statement and occurrence['status']!='cancelled' and statement['due_on'][:7]==month:
            ref='legacy:'+str(statement['card_id'])
            if ref in fixed:fixed[ref]['installment_minor']+=int(occurrence['amount_minor'])
    balances={(row['card_ref'],row['month']):row for row in monthly_total_balances(data)}
    rows=[]
    for card in cards:
        ref=card['card_ref'];manual=saved.get(ref);known=[row for row in statements if 'legacy:'+str(row['card_id'])==ref and row.get('payment_month',row['due_on'][:7])==month]
        planned=sum(int(rule['amount_minor']) for rule in data.get('recurring',[]) if rule['status']=='active' and rule.get('card_ref')==ref for _ in payment_dates(rule,first,last))
        gross=int(manual['amount_minor']) if manual else sum(int(row['total_minor']) for row in known)+planned if known else None
        parts=fixed[ref];extra=gross-sum(parts.values()) if gross is not None else None
        rows.append({'card_ref':ref,'bank_name':card['bank_name'],'display_name':card['display_name'],'version':manual['version'] if manual else 0,'source_month':manual['month'] if manual else month,'configured_due_on':configured_card_due(data,ref,month),'payment_due_day':card_schedule.due_day(configured_card(data,ref)),'due_day_offset':monthly_due_offset(data,manual) if manual else 0,'due_date_explicit':manual.get('due_date_explicit',0) if manual else 0,'due_on':balances.get((ref,month),{}).get('due_on') or configured_card_due(data,ref,month),'remaining_minor':str(balances[(ref,month)]['remaining_minor']) if (ref,month) in balances else None,'source':'entered' if manual else 'statement' if known else 'missing','total_minor':str(gross) if gross is not None else None,**{key:str(value) for key,value in parts.items()},'extra_minor':str(extra) if extra is not None and extra>=0 else None,'inconsistent':extra is not None and extra<0})
    return {'month':month,'cards':rows,'unassigned_installments':unassigned,'extra_minor':str(sum(int(row['extra_minor']) for row in rows if row['extra_minor'] is not None)),'complete':all(row['total_minor'] is not None and not row['inconsistent'] for row in rows) and not unassigned}


def save_card_monthly_total(user_id,payload):
    import profile_settings,re
    month=payload.get('month','');source_month=payload.get('source_month') or month
    for value in (month,source_month):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}',value):raise ValueError('Ödeme ayını seçin.')
        date.fromisoformat(value+'-01')
    amount=core.money_to_minor(payload.get('amount'))
    if amount<0:raise ValueError('Toplam kart borcu negatif olamaz.')
    ref=payload.get('card_ref','');version=payload.get('version',0)
    if isinstance(version,bool) or not isinstance(version,int) or version<0:raise ValueError('Kayıt sürümü geçersiz.')
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        card=profile_settings.resolve_card(db,user_id,ref);ref=card['kind']+':'+str(card['record_id'])
        if not card['active']:raise ValueError('Pasif kartın borcunu güncellemeden önce kartı etkinleştirin.')
        offset=card_schedule.offset_value(payload.get('due_day_offset',0))
        due=card_due_for_month(card,month,offset)
        if payload.get('due_on') and payload['due_on']!=due:raise Conflict('Vade kart ayarından hesaplanır. Kart gününü veya −5/+5 gün düzeltmesini kullanın.')
        old=db.execute('SELECT * FROM card_monthly_totals WHERE user_id=? AND card_ref=? AND month=?',(user_id,ref,source_month)).fetchone()
        target=db.execute('SELECT * FROM card_monthly_totals WHERE user_id=? AND card_ref=? AND month=?',(user_id,ref,month)).fetchone() if source_month!=month else old
        if source_month!=month and not old:
            if target and (target['amount_minor'],target['due_on'],target['version'])==(amount,due,version+1):return {'version':target['version']}
            raise Conflict('Kaydın dönemi değişmiş. Güncel borcu yükleyin.')
        if source_month!=month and target:raise Conflict('Bu kartın hedef ayda ayrı bir borcu var. İki kayıt kendiliğinden birleştirilmedi.')
        data=read_data(user_id,db)
        paid=next((row['paid_minor'] for row in monthly_total_balances(data) if row['card_ref']==ref and row['month']==source_month),0)
        if amount<paid:raise ValueError('Toplam borç, bu ekstre için kaydedilmiş ödemelerden düşük olamaz.')
        if old and source_month==month and old['amount_minor']==amount and old['due_on']==due and old['due_date_explicit'] and old['due_day_offset']==offset:return {'version':old['version']}
        if (old['version'] if old else 0)!=version:raise Conflict('Kart borcu başka bir ekranda değişti. Sayfayı yenileyin.')
        next_version=version+1
        if source_month!=month:
            db.execute('INSERT INTO card_monthly_totals(user_id,card_ref,month,amount_minor,version,due_on,due_date_explicit) VALUES(?,?,?,?,?,?,1)',(user_id,ref,month,amount,next_version,due))
            db.execute('UPDATE card_monthly_payments SET month=? WHERE user_id=? AND card_ref=? AND month=?',(month,user_id,ref,source_month))
            db.execute('DELETE FROM card_monthly_totals WHERE user_id=? AND card_ref=? AND month=?',(user_id,ref,source_month))
        else:
            db.execute('INSERT INTO card_monthly_totals(user_id,card_ref,month,amount_minor,version,due_on,due_date_explicit) VALUES(?,?,?,?,?,?,1) ON CONFLICT(user_id,card_ref,month) DO UPDATE SET amount_minor=excluded.amount_minor,version=excluded.version,due_on=excluded.due_on,due_date_explicit=1',(user_id,ref,month,amount,next_version,due))
        db.execute('UPDATE card_monthly_totals SET due_day_offset=? WHERE user_id=? AND card_ref=? AND month=?',(offset,user_id,ref,month))
        return {'version':next_version}


def card_due_for_month(card,month,offset=0):
    return card_schedule.due_on(card,month,offset)


def configured_card(data,reference):
    kind,_,record_id=reference.partition(':')
    row=next((row for row in data.get('profile_cards',[]) if str(row['legacy_card_id'] if kind=='legacy' else row['id'])==record_id and (kind=='legacy' or row['legacy_card_id'] is None)),None)
    legacy=next((row for row in data.get('cards',[]) if kind=='legacy' and str(row['id'])==record_id),{})
    return {**legacy,**(row or {})}


def configured_card_due(data,reference,month=None):
    card=configured_card(data,reference)
    try:return card_due_for_month(card,month or today().strftime('%Y-%m'))
    except ValueError:return None


def payment_due(data,rule,scheduled):
    if not rule.get('card_ref'):return scheduled
    card=configured_card(data,rule['card_ref'])
    month=scheduled[:7]
    total=next((row for row in data.get('card_monthly_totals',[]) if row['card_ref']==rule['card_ref'] and row['month']==month),{})
    try:return card_due_for_month(card,month,monthly_due_offset(data,total) if total else 0)
    except ValueError:return scheduled


def monthly_due_offset(data,total):
    # Old full-date invoices become bounded monthly offsets on read, without writes.
    offset=total.get('due_day_offset',0)
    if not total.get('due_date_explicit') and total.get('due_on'):
        base=configured_card_due(data,total['card_ref'],total['month'])
        if base:
            difference=(date.fromisoformat(total['due_on'])-date.fromisoformat(base)).days
            if -5<=difference<=5:return difference
    return offset


def monthly_total_balances(data):
    rules={row['id']:row for row in data.get('recurring',[])}
    expenses={row['id']:row for row in data.get('expenses',[])}
    statements=statement_balances(data);result=[]
    for total in data.get('card_monthly_totals',[]):
        ref,month=total['card_ref'],total['month']
        due=configured_card_due(data,ref,month)
        if due:due=card_due_for_month(configured_card(data,ref),month,monthly_due_offset(data,total))
        else:due=total.get('due_on')
        if not due:continue
        def covered(rule_id,scheduled):return rules.get(rule_id,{}).get('card_ref')==ref and scheduled[:7]==month
        paid=sum(int(row['amount_minor']) for row in data.get('payment_events',[]) if covered(row['recurring_id'],row['scheduled_on']))
        paid+=sum(int(expenses[row['expense_id']]['amount_minor']) for row in data.get('occurrences',[]) if row.get('expense_id') in expenses and covered(row['recurring_id'],row['scheduled_on']))
        paid+=sum(int(row['paid_minor']) for row in statements if 'legacy:'+str(row['card_id'])==ref and row.get('payment_month',row['due_on'][:7])==month)
        paid+=sum(int(row['amount_minor']) for row in data.get('card_monthly_payments',[]) if row['card_ref']==ref and row['month']==month)
        gross=int(total['amount_minor'])
        card=next((card for card in data.get('cards' if ref.startswith('legacy:') else 'profile_cards',[]) if str(card['id'])==ref.split(':')[1]),{})
        result.append({**total,'due_on':due,'description':(card.get('display_name') or 'Kart')+' · Aylık kart borcu','paid_minor':paid,'remaining_minor':max(0,gross-paid)})
    return result


def apply_monthly_card_totals(data,events,start,end,as_of):
    totals={(row['card_ref'],row['month']):row for row in monthly_total_balances(data)}
    rules={row['id']:row for row in data.get('recurring',[])}
    kept=[]
    for event in events:
        if event['kind']=='payment' and event['status']!='paid':
            ref=rules.get(event.get('recurring_id'),{}).get('card_ref') or ('legacy:'+str(event['card_id']) if event.get('statement_id') else '')
            if (ref,(event.get('scheduled_on') or event['date'])[:7]) in totals:continue
        kept.append(event)
    for row in totals.values():
        if row['remaining_minor'] and start<=row['due_on']<=end:
            kept.append({'id':'card-month:'+row['card_ref']+':'+row['month'],'date':row['due_on'],'scheduled_on':row['due_on'],'amount_minor':row['remaining_minor'],'description':row['description'],'kind':'payment','status':'overdue' if row['due_on']<as_of.isoformat() else 'planned','monthly_card_ref':row['card_ref'],'monthly_month':row['month']})
    for row in data.get('card_monthly_payments',[]):
        if start<=row['paid_on']<=end:kept.append({'id':'card-month-paid:'+str(row['id']),'date':row['paid_on'],'amount_minor':row['amount_minor'],'description':'Kaydedilmiş aylık kart ödemesi','kind':'payment','status':'paid'})
    return kept


def record_card_monthly_payment(user_id,payload):
    import profile_settings
    key=_key(payload);amount=core.money_to_minor(payload.get('amount'));paid=date.fromisoformat(payload.get('paid_on',''))
    if amount<=0 or paid.isoformat()!=payload.get('paid_on') or paid>today():raise ValueError('Ödeme tutarı ve gerçekleşme tarihini kontrol edin.')
    month=payload.get('month','');ref=payload.get('card_ref','')
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        card=profile_settings.resolve_card(db,user_id,ref);ref=card['kind']+':'+str(card['record_id'])
        old=db.execute('SELECT * FROM card_monthly_payments WHERE user_id=? AND idempotency_key=?',(user_id,key)).fetchone()
        if old:
            if (old['card_ref'],old['month'],old['amount_minor'],old['paid_on'])!=(ref,month,amount,paid.isoformat()):raise Conflict('Aynı ödeme isteği farklı bilgilerle kullanılamaz.')
            return {'id':old['id'],'created':False}
        data=read_data(user_id,db)
        balance=next((row for row in monthly_total_balances(data) if row['card_ref']==ref and row['month']==month),None)
        if not balance or amount>balance['remaining_minor']:raise ValueError('Ödeme, aylık kart borcunun kalanını aşamaz.')
        result=db.execute('INSERT INTO card_monthly_payments(user_id,card_ref,month,amount_minor,paid_on,idempotency_key) VALUES(?,?,?,?,?,?)',(user_id,ref,month,amount,paid.isoformat(),key))
        return {'id':result.lastrowid,'created':True}


def _assert_visible(db,user_id,kind,record_id):
    if db.execute('SELECT 1 FROM financial_record_deletions WHERE user_id=? AND record_kind=? AND record_id=?',(user_id,kind,record_id)).fetchone():
        raise ValueError('Bu kayıt silinmiş. Güncel listeyi yenileyin veya silme işlemini geri alın.')


def _visible_records(data,user_id,connection=None):
    with (nullcontext(connection) if connection is not None else core.connect()) as db:
        deleted=[dict(row) for row in db.execute('SELECT record_kind,record_id FROM financial_record_deletions WHERE user_id=?',(user_id,))]
    ids=lambda kind:{row['record_id'] for row in deleted if row['record_kind']==kind}
    income_rules,records,plans=ids('income_rule'),ids('income_record'),ids('payment_plan')
    data['suppressed_income_keys']=[(row['rule_id'],row['expected_on']) for row in data['income_records'] if row['rule_id'] is not None and row['id'] in records]
    data['income_rules']=[row for row in data['income_rules'] if row['id'] not in income_rules]
    data['income_rule_amounts']=[row for row in data['income_rule_amounts'] if row['rule_id'] not in income_rules]
    data['income_records']=[row for row in data['income_records'] if row['id'] not in records and row['rule_id'] not in income_rules]
    linked={row['expense_id'] for row in data['occurrences'] if row['recurring_id'] in plans and row.get('expense_id') is not None}
    data['recurring']=[row for row in data['recurring'] if row['id'] not in plans]
    data['payment_events']=[row for row in data['payment_events'] if row['recurring_id'] not in plans]
    data['occurrences']=[row for row in data['occurrences'] if row['recurring_id'] not in plans]
    data['expenses']=[row for row in data['expenses'] if row['id'] not in linked]
    removed_plans={row['id'] for row in data['installment_plans'] if row['expense_id'] in linked}
    data['installment_plans']=[row for row in data['installment_plans'] if row['id'] not in removed_plans]
    data['installment_occurrences']=[row for row in data['installment_occurrences'] if row['plan_id'] not in removed_plans]
    return data


def managed_source(data,kind,record_id):
    if kind=='income_record':
        row=next((row for row in data['income_records'] if row['id']==record_id),None);children=[]
    elif kind=='income_rule':
        row=next((row for row in data['income_rules'] if row['id']==record_id),None)
        children=[item for key in ('income_records','income_rule_amounts') for item in data[key] if item['rule_id']==record_id]
    elif kind=='payment_plan':
        row=next((row for row in data['recurring'] if row['id']==record_id),None)
        children=[item for key in ('payment_events','occurrences') for item in data[key] if item['recurring_id']==record_id]
    else:raise ValueError('Kayıt türünü kontrol edin.')
    if not row:raise ValueError('Kayıt hesabınıza ait değil veya bulunamadı.')
    return row,children


def managed_revision(data,kind,record_id):
    import hashlib,json
    return hashlib.sha256(json.dumps(managed_source(data,kind,record_id),sort_keys=True,default=str).encode()).hexdigest()


def income_rule_price(data,rule):
    prices=[row for row in data['income_rule_amounts'] if row['rule_id']==rule['id']]
    eligible=[row for row in prices if row['effective_on']<=today().isoformat()]
    return int(max(eligible,key=lambda row:row['effective_on'])['amount_minor']) if eligible else int(min(prices,key=lambda row:row['effective_on'])['amount_minor']) if prices else 0


def delete_financial_record(user_id,payload):
    import secrets
    if payload.get('confirmation')!='SİL':raise ValueError('Silme işlemini onaylayın.')
    kind=payload.get('record_kind');record_id=core.record_id(payload.get('id'))
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        old=db.execute('SELECT token FROM financial_record_deletions WHERE user_id=? AND record_kind=? AND record_id=?',(user_id,kind,record_id)).fetchone()
        if old:return {'deleted':True,'record_kind':kind,'id':record_id,'token':old['token']}
        data=read_data(user_id,db)
        if payload.get('revision')!=managed_revision(data,kind,record_id):raise Conflict('Kayıt değişmiş. Güncel bilgileri görüp silme işlemini yeniden onaylayın.')
        token=secrets.token_urlsafe(24)
        db.execute('INSERT INTO financial_record_deletions(user_id,record_kind,record_id,token) VALUES(?,?,?,?)',(user_id,kind,record_id,token))
        return {'deleted':True,'record_kind':kind,'id':record_id,'token':token}


def restore_financial_record(user_id,payload):
    record_id=core.record_id(payload.get('id'));kind=payload.get('record_kind');token=payload.get('token','')
    if not isinstance(token,str) or len(token)<16:raise ValueError('Geri alma bilgisi geçersiz.')
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        row=db.execute('SELECT token FROM financial_record_deletions WHERE user_id=? AND record_kind=? AND record_id=?',(user_id,kind,record_id)).fetchone()
        if not row or row['token']!=token:raise Conflict('Bu silme işlemi geri alınmış veya kayıt yeniden silinmiş.')
        if token.startswith('opening-transfer:'):
            import json,base64
            change=json.loads(base64.urlsafe_b64decode(token.split(':',1)[1]))
            opening=db.execute('SELECT opening_balance_minor,effective_on,auto_income_on_due FROM cash_settings WHERE user_id=?',(user_id,)).fetchone()
            if not opening or opening_revision(opening)!=change['after']:raise Conflict('Taşıma sonrasında başlangıç bakiyesi düzenlenmiş; güncel bakiye ezilmedi.')
            if change['previous']:
                previous=change['previous']
                db.execute('UPDATE cash_settings SET opening_balance_minor=?,effective_on=? WHERE user_id=?',(previous['opening_balance_minor'],previous['effective_on'],user_id))
            else:db.execute('DELETE FROM cash_settings WHERE user_id=?',(user_id,))
        db.execute('DELETE FROM financial_record_deletions WHERE user_id=? AND record_kind=? AND record_id=?',(user_id,kind,record_id))
        return {'restored':True}


def edit_simple_income(user_id,payload):
    kind=payload.get('record_kind');record_id=core.record_id(payload.get('id'))
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        data=read_data(user_id,db);row,_=managed_source(data,kind,record_id)
        description,income_type,expected,note,amount,category=core._valid_income_fields({**row,**payload})
        if kind not in ('income_record','income_rule'):raise ValueError('Gelir kaydını seçin.')
        if payload.get('revision')!=managed_revision(data,kind,record_id):raise Conflict('Gelir başka bir ekranda değişti. Listeyi yenileyin.')
        if category and not db.execute('SELECT 1 FROM categories WHERE user_id=? AND id=?',(user_id,category)).fetchone():raise ValueError('Kategori hesabınıza ait olmalı.')
        if kind=='income_record':
            if row['rule_id'] is not None and expected.isoformat()!=row['expected_on']:
                db.execute('UPDATE income_records SET rule_id=NULL WHERE user_id=? AND id=?',(user_id,record_id))
                db.execute("INSERT INTO income_records(user_id,description,income_type,amount_minor,expected_on,status,rule_id) VALUES(?,?,?,?,?,'cancelled',?)",(user_id,row['description'],row['income_type'],row['amount_minor'],row['expected_on'],row['rule_id']))
            status=payload.get('status',row['status'])
            if status not in ('expected','received'):raise ValueError('Gelirin durumunu kontrol edin.')
            received=date.fromisoformat(payload.get('received_on') or expected.isoformat()) if status=='received' else None
            if received and (received>today() or received.isoformat()!=(payload.get('received_on') or expected.isoformat())):raise ValueError('Gerçek alınma tarihi gelecekte olamaz.')
            db.execute('UPDATE income_records SET description=?,income_type=?,amount_minor=?,expected_on=?,received_on=?,status=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',(description,income_type,amount,expected.isoformat(),received.isoformat() if received else None,status,note,record_id,user_id))
        else:
            frequency=payload.get('frequency');mode=payload.get('schedule_mode','anchored')
            if frequency not in ('monthly','weekly') or mode not in ('anchored','last_day') or frequency=='weekly' and mode!='anchored':raise ValueError('Gelirin tekrar sıklığını kontrol edin.')
            first=expected
            if mode=='last_day':
                month=date.fromisoformat(payload.get('start_month','')+'-01');first=core._month_close(month.year,month.month,31)
            has_receipts=any(item['rule_id']==record_id and item['status']=='received' for item in data['income_records'])
            if has_receipts and (frequency,mode,first.isoformat())!=(row['frequency'],row['schedule_mode'],row['start_on']) and first<today():raise ValueError('Alınmış gelirler korunur. Yeni tekrar düzeninin başlangıcını bugün veya sonrası seçin; geçmiş tahsilatı kendi Düzenle düğmesinden düzeltebilirsiniz.')
            if row['end_on'] and first.isoformat()>row['end_on']:raise ValueError('Başlangıç tarihi planın bitişinden sonra olamaz.')
            db.execute('UPDATE income_rules SET description=?,income_type=?,frequency=?,schedule_mode=?,start_on=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',(description,income_type,frequency,mode,first.isoformat(),record_id,user_id))
            if not has_receipts:db.execute('DELETE FROM income_rule_amounts WHERE user_id=? AND rule_id=?',(user_id,record_id))
            effective=max(first,today()).isoformat() if has_receipts else first.isoformat()
            db.execute('INSERT INTO income_rule_amounts(user_id,rule_id,effective_on,amount_minor) VALUES(?,?,?,?) ON CONFLICT(rule_id,effective_on) DO UPDATE SET amount_minor=excluded.amount_minor',(user_id,record_id,effective,amount))
            # Keep previously materialized, unreceived projections aligned with the edited template.
            for projected in data['income_records']:
                if projected['rule_id']!=record_id or projected['status']!='expected':continue
                due=date.fromisoformat(projected['expected_on'])
                index=core.income_recurrence_index(first,frequency,max(first,due),mode)
                belongs=due>=first and core.income_recurrence_date(first,frequency,index,mode)==due and (not row['end_on'] or projected['expected_on']<=row['end_on'])
                if not belongs:db.execute("UPDATE income_records SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND id=?",(user_id,projected['id']))
                elif not has_receipts or projected['expected_on']>=effective:db.execute('UPDATE income_records SET amount_minor=?,description=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND id=?',(amount,description,user_id,projected['id']))
        return {'id':record_id}


def transfer_income_to_opening(user_id,payload):
    """Explicit correction of misclassified bank cash, atomic and recoverable."""
    import secrets,json,base64
    record_id=core.record_id(payload.get('id'))
    with core.LOCK,core.connect() as db:
        _write_lock(db,'users',user_id,user_id)
        data=read_data(user_id,db)
        row,_=managed_source(data,'income_record',record_id)
        if payload.get('revision')!=managed_revision(data,'income_record',record_id):raise Conflict('Gelir kaydı değişmiş. Güncel kaydı kontrol edin.')
        if row['rule_id'] is not None or row['status']!='received':raise ValueError('Yalnızca alınmış tek seferlik banka nakdi başlangıç bakiyesine taşınabilir.')
        old=data.get('opening')
        if old and (old['opening_balance_minor']!=0 or old['effective_on']!=row['received_on']):raise Conflict('Başlangıç bakiyesi sıfır ve gelirle aynı tarih olmalı; mevcut bakiye ezilmedi.')
        before=cash_position(data)['balance_minor']
        db.execute('INSERT INTO cash_settings(user_id,opening_balance_minor,effective_on) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET opening_balance_minor=excluded.opening_balance_minor,effective_on=excluded.effective_on',(user_id,row['amount_minor'],row['received_on']))
        after={'opening_balance_minor':row['amount_minor'],'effective_on':row['received_on']}
        token='opening-transfer:'+base64.urlsafe_b64encode(json.dumps({'previous':old,'after':opening_revision(after),'nonce':secrets.token_urlsafe(24)}).encode()).decode()
        db.execute("INSERT INTO financial_record_deletions(user_id,record_kind,record_id,token) VALUES(?,'income_record',?,?)",(user_id,record_id,token))
        result=cash_position(read_data(user_id,db))
        if old and result['balance_minor']!=before:raise Conflict('Banka bakiyesi uzlaşmadı; taşıma geri alındı.')
        return {'transferred':True,'record_kind':'income_record','id':record_id,'token':token}
