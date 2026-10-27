"""Gelir ve kullanıcı tarafından girilen ödeme günlerinden salt okunur nakit planı."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import calendar

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


def read_data(user_id):
    # No initialization, materialization, UPDATE or INSERT in this read path.
    with core.connect() as db:
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
        ):
            data[key]=[dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY {order}",(user_id,)).fetchall()]
        details={row['recurring_id']:dict(row) for row in db.execute('SELECT * FROM payment_plan_details WHERE user_id=?',(user_id,))}
        for rule in data['recurring']:
            extra=details.get(rule['id'],{})
            rule.update(entry_kind=extra.get('entry_kind','installment'),purchased_on=extra.get('purchased_on'),card_ref=('legacy:'+str(extra['legacy_card_id'])) if extra.get('legacy_card_id') else ('card:'+str(extra['profile_card_id'])) if extra.get('profile_card_id') else '')
        opening=db.execute("SELECT opening_balance_minor,effective_on FROM cash_settings WHERE user_id=?",(user_id,)).fetchone()
        data["opening"]=dict(opening) if opening else None
    return data


def calculate(data,start_value,end_value,as_of=None):
    as_of=as_of or today()
    start,end=date.fromisoformat(start_value),date.fromisoformat(end_value)
    if start.isoformat()!=start_value or end.isoformat()!=end_value or end<start or (end-start).days>3660:
        raise ValueError("Tarih aralığını kontrol edin; en fazla 10 yıl seçilebilir.")
    events=[]
    def add(key,when,amount,description,kind,status,**extra):
        if start_value<=when<=end_value:
            events.append({"id":key,"date":when,"amount_minor":int(amount),"description":description,"kind":kind,"status":status,**extra})
    actual_income_keys=set()
    rules={row["id"]:row for row in data["income_rules"]}
    for row in data["income_records"]:
        if row["status"]=="received":
            if row["rule_id"] is not None:
                actual_income_keys.add((row["rule_id"],row["expected_on"]))
            add(f"income:{row['id']}",row["received_on"],row["amount_minor"],row["description"],"income","received",record_id=row["id"],expected_on=row["expected_on"])
        elif row["rule_id"] is None and row["status"]=="expected":
            add(f"income:{row['id']}",row["expected_on"],row["amount_minor"],row["description"],"income","overdue" if row["expected_on"]<as_of.isoformat() else "expected",record_id=row["id"])
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
                add(f"income-rule:{rule['id']}:{due}",due.isoformat(),amount,rule["description"],"income","overdue" if due<as_of else "expected",rule_id=rule["id"],scheduled_on=due.isoformat())
            index+=1
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
        for due in payment_dates(rule,start,end):
            if (rule["id"],due.isoformat()) in completed:
                continue
            remaining=int(rule["amount_minor"])-partial.get((rule["id"],due.isoformat()),0)
            if remaining<=0:
                continue
            add(f"payment:{rule['id']}:{due}",due.isoformat(),remaining,rule["description"],"payment","overdue" if due<as_of else "planned",recurring_id=rule["id"],scheduled_on=due.isoformat())
    for row in data["expenses"]:
        if row["id"] not in linked_expenses and row["card_id"] is None:
            add(f"expense:{row['id']}",row["spent_on"],row["amount_minor"],row["description"],"payment","paid")
    for row in data["repayments"]:
        add(f"repayment:{row['id']}",row["paid_on"],row["amount_minor"],"Kaydedilmiş kart ödemesi","payment","paid")
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
    result["rules"]=[{**rule,"revision":rule_revision(rule)} for rule in data["recurring"]]
    result["income_rules"]=data["income_rules"]
    result["income_records"]=data["income_records"]
    result["income_rule_amounts"]=data["income_rule_amounts"]
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
        identity="BIGSERIAL" if pg else "INTEGER"
        integer="BIGINT" if pg else "INTEGER"
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
        db.execute('CREATE INDEX IF NOT EXISTS payment_details_user_card ON payment_plan_details(user_id,profile_card_id,legacy_card_id)')
        db.execute("CREATE INDEX IF NOT EXISTS payment_events_user_date ON payment_events(user_id,paid_on)")
        db.execute("CREATE INDEX IF NOT EXISTS payment_events_occurrence ON payment_events(user_id,recurring_id,scheduled_on)")
        for table in ("recurring_expenses","income_records","income_rules"):
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
        old_detail=db.execute('SELECT * FROM payment_plan_details WHERE recurring_id=? AND user_id=?',(record_id,user_id)).fetchone() if old else None
        old_meta=_detail_fields(old_detail)
        kind=payload.get('entry_kind',old_meta['entry_kind'])
        if kind not in ('expense','installment','regular'):raise ValueError('Ödeme türünü kontrol edin.')
        reference=payload.get('card_ref',old_meta['card_ref']) or ''
        if kind=='regular' and reference:raise ValueError('Banka / nakit ödemesi kredi kartına bağlanamaz.')
        card=profile_settings.resolve_card(db,user_id,reference) if reference else None
        if card and not card['active'] and reference!=old_meta['card_ref']:raise ValueError('Pasif kartla yeni işlem eklenemez.')
        if not old and card and not card['active']:raise ValueError('Pasif kartla yeni işlem eklenemez.')
        purchase=None
        if kind=='expense':
            if not card:raise ValueError('Harcama için bir kredi kartı seçin.')
            purchase=date.fromisoformat(payload.get('purchased_on',''))
            if purchase.isoformat()!=payload.get('purchased_on') or purchase>today():raise ValueError('Alışveriş tarihi gelecekte olamaz.')
            if not card['payment_due_on']:raise ValueError('Önce Profil ve Ayarlar bölümünde kartın son ödeme tarihini girin.')
            first=date.fromisoformat(card['payment_due_on'])
            if first<purchase:raise ValueError('Kartın son ödeme tarihi alışverişten önce. Kart ayarındaki vadeyi güncelleyin.')
            if payload.get('start_on') and payload['start_on']!=first.isoformat():raise Conflict('Kartın son ödeme tarihi değişmiş. Kartı yeniden seçin.')
            last=first;frequency='monthly'
        else:
            first=date.fromisoformat(payload.get('start_on',''))
            last=date.fromisoformat(payload['end_on']) if payload.get('end_on') else None
            if payload.get('entry_kind') in ('installment','regular') and not last:raise ValueError('Ödemenin bitiş tarihini girin.')
            frequency=payload.get('frequency','monthly')
            if kind=='regular' and frequency not in ('monthly','weekly'):raise ValueError('Banka / nakit ödemesi aylık veya haftalık olabilir.')
        if first.isoformat()!=(payload.get('start_on') or first.isoformat()) or last and last<first:raise ValueError('İlk ve son ödeme tarihlerini kontrol edin.')
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


def _detail_fields(row):
    return {'entry_kind':row['entry_kind'] if row else 'installment','card_ref':('legacy:'+str(row['legacy_card_id'])) if row and row['legacy_card_id'] else ('card:'+str(row['profile_card_id'])) if row and row['profile_card_id'] else '', 'purchased_on':row['purchased_on'] if row else None}


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
            row=db.execute("SELECT * FROM income_records WHERE id=? AND user_id=?",(record_id,user_id)).fetchone()
            if not row or row["status"]=="cancelled":
                raise ValueError("Gelir kaydı bulunamadı.")
            amount=row["amount_minor"]
        else:
            scheduled=date.fromisoformat(payload.get("scheduled_on",""))
            rule=db.execute("SELECT * FROM income_rules WHERE id=? AND user_id=?",(rule_id,user_id)).fetchone()
            if not rule:
                raise ValueError("Gelir kuralı bulunamadı.")
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
