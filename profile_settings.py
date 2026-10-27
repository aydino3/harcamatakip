"""Hesaba ait profil ve sade kart tanımları; eski kart kayıtları korunur."""
from datetime import date
from simple_cashflow import today
import re
import app as core


def initialize():
    with core.connect() as db:
        pg=hasattr(db,'_conn')
        if pg:
            db.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT')
        elif 'display_name' not in {row[1] for row in db.execute('PRAGMA table_info(users)')}:
            db.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
        identity='BIGSERIAL' if pg else 'INTEGER'
        integer='BIGINT' if pg else 'INTEGER'
        db.execute(f"""CREATE TABLE IF NOT EXISTS profile_cards (
            id {identity} PRIMARY KEY,user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            legacy_card_id {integer},bank_name TEXT NOT NULL,display_name TEXT NOT NULL,last_four TEXT NOT NULL DEFAULT '',
            payment_due_on TEXT,active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
            UNIQUE(id,user_id),UNIQUE(user_id,legacy_card_id),UNIQUE(user_id,bank_name,display_name,last_four),
            FOREIGN KEY(legacy_card_id,user_id) REFERENCES credit_cards(id,user_id))""")
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS profile_cards_id_owner ON profile_cards(id,user_id)')
        if pg:
            db.execute('ALTER TABLE profile_cards ENABLE ROW LEVEL SECURITY')
            db.execute('REVOKE ALL ON profile_cards FROM PUBLIC')
            db.execute('REVOKE ALL ON SEQUENCE profile_cards_id_seq FROM PUBLIC')
            for role in ('anon','authenticated'):
                if db.execute('SELECT 1 FROM pg_roles WHERE rolname=?',(role,)).fetchone():
                    db.execute(f'REVOKE ALL ON profile_cards FROM "{role}"')
                    db.execute(f'REVOKE ALL ON SEQUENCE profile_cards_id_seq FROM "{role}"')


def get_profile(user_id):
    with core.connect() as db:
        user=db.execute('SELECT id,email,display_name FROM users WHERE id=?',(user_id,)).fetchone()
        legacy=[dict(row) for row in db.execute('SELECT c.id,c.bank_name,c.display_name,c.last_four,c.active,c.due_day,COALESCE((SELECT MIN(s.due_on) FROM card_statements s WHERE s.card_id=c.id AND s.user_id=c.user_id AND s.due_on>=?),(SELECT MAX(s.due_on) FROM card_statements s WHERE s.card_id=c.id AND s.user_id=c.user_id)) known_payment_due_on FROM credit_cards c WHERE c.user_id=? ORDER BY c.active DESC,c.display_name',(today().isoformat(),user_id)).fetchall()]
        settings=[dict(row) for row in db.execute('SELECT * FROM profile_cards WHERE user_id=? ORDER BY active DESC,display_name',(user_id,)).fetchall()]
    if not user:
        raise ValueError('Profil bulunamadı.')
    cards=[]
    overlays={row['legacy_card_id']:row for row in settings if row['legacy_card_id'] is not None}
    for old in legacy:
        extra=overlays.get(old['id'])
        cards.append({'id':f"legacy:{old['id']}",'bank_name':old['bank_name'],'display_name':old['display_name'],'last_four':old['last_four'],'active':old['active'],'payment_due_on':extra['payment_due_on'] if extra else old['known_payment_due_on'],'version':extra['version'] if extra else 0,'monthly_due_day':old['due_day']})
    for row in settings:
        if row['legacy_card_id'] is None:
            cards.append({'id':f"card:{row['id']}",**{key:row[key] for key in ('bank_name','display_name','last_four','active','payment_due_on','version')},'monthly_due_day':None})
    return {'id':user['id'],'email':user['email'],'display_name':user['display_name'] or user['email'].split('@')[0],'cards':cards}


def save_name(user_id,payload):
    name=payload.get('display_name','')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:
        raise ValueError('Profil adı 1–80 karakter olmalı.')
    with core.connect() as db:
        db.execute('UPDATE users SET display_name=? WHERE id=?',(name.strip(),user_id))
    return get_profile(user_id)


def save_card(user_id,payload):
    bank,name,last=payload.get('bank_name',''),payload.get('display_name',''),payload.get('last_four','')
    if not isinstance(bank,str) or not isinstance(name,str) or not isinstance(last,str) or not 1<=len(bank.strip())<=80 or not 1<=len(name.strip())<=80 or last and (len(last)!=4 or not last.isascii() or not last.isdigit()):
        raise ValueError('Banka, kart adı ve varsa son dört haneyi kontrol edin.')
    due=payload.get('payment_due_on') or None
    if not due:raise ValueError('Kartın son ödeme tarihini girin.')
    if due:
        try:
            if date.fromisoformat(due).isoformat()!=due:raise ValueError
        except (ValueError,TypeError):raise ValueError('Son ödeme tarihi geçerli bir gün olmalı.') from None
    active=payload.get('active',1)
    if not isinstance(active,(int,bool)) or active not in (0,1,False,True):raise ValueError('Kart durumunu kontrol edin.')
    reference=payload.get('id') or None
    kind=record_id=None
    if reference:
        match=re.fullmatch(r'(legacy|card):([0-9]+)',str(reference))
        if not match:raise ValueError('Kart bulunamadı.')
        kind,raw=match.groups();record_id=core.record_id(raw)
    with core.LOCK,core.connect() as db:
        if hasattr(db,'_conn'):
            db.execute('SELECT id FROM users WHERE id=? FOR UPDATE',(user_id,)).fetchone()
        else:db.execute('BEGIN IMMEDIATE')
        old=None
        if kind=='legacy':
            legacy=db.execute('SELECT id,bank_name,display_name,last_four,active FROM credit_cards WHERE id=? AND user_id=?',(record_id,user_id)).fetchone()
            if not legacy:raise ValueError('Kart bulunamadı.')
            old=db.execute('SELECT * FROM profile_cards WHERE legacy_card_id=? AND user_id=?',(record_id,user_id)).fetchone()
        elif kind=='card':
            old=db.execute('SELECT * FROM profile_cards WHERE id=? AND user_id=? AND legacy_card_id IS NULL',(record_id,user_id)).fetchone()
            if not old:raise ValueError('Kart bulunamadı.')
        values=(bank.strip(),name.strip(),last,due,int(active))
        collision=db.execute('SELECT id FROM profile_cards WHERE user_id=? AND bank_name=? AND display_name=? AND last_four=?',(user_id,bank.strip(),name.strip(),last)).fetchone()
        if reference and collision and (not old or collision['id']!=old['id']):
            raise ValueError('Bu isim ve son dört haneyle başka bir kart zaten tanımlı.')
        legacy_collision=db.execute('SELECT id FROM credit_cards WHERE user_id=? AND bank_name=? AND display_name=? AND last_four=?',(user_id,bank.strip(),name.strip(),last)).fetchone()
        if legacy_collision and (kind!='legacy' or legacy_collision['id']!=record_id):
            raise ValueError('Bu kart mevcut kartlarınızda tanımlı. Son ödeme tarihini kartın Düzenle düğmesinden girin.')
        version=old['version'] if old else 0
        if old:
            before=(legacy['bank_name'],legacy['display_name'],legacy['last_four'],old['payment_due_on'],legacy['active']) if kind=='legacy' else tuple(old[field] for field in ('bank_name','display_name','last_four','payment_due_on','active'))
            if before==values:return {'id':reference}
        if reference and (isinstance(payload.get('version'),bool) or not isinstance(payload.get('version'),int) or payload.get('version')!=version):
            raise ValueError('Kart bilgisi değişti. Güncel bilgileri yükleyip yeniden düzenleyin.')
        if reference:
            _move_unpaid_expenses(db,user_id,kind,record_id,due)
        if old:
            db.execute('UPDATE profile_cards SET bank_name=?,display_name=?,last_four=?,payment_due_on=?,active=?,version=version+1 WHERE id=? AND user_id=?',(*values,old['id'],user_id))
        else:
            prior=db.execute('SELECT * FROM profile_cards WHERE user_id=? AND bank_name=? AND display_name=? AND last_four=?',(user_id,bank.strip(),name.strip(),last)).fetchone()
            if prior:
                if (prior['payment_due_on'],prior['active'])!=(due,int(active)):
                    raise ValueError('Bu kart zaten tanımlı. Değiştirmek için kartı düzenleyin.')
                return {'id':f"card:{prior['id']}"}
            new=db.execute('INSERT INTO profile_cards(user_id,legacy_card_id,bank_name,display_name,last_four,payment_due_on,active) VALUES(?,?,?,?,?,?,?)',(user_id,record_id if kind=='legacy' else None,*values)).lastrowid
            if not reference:reference=f'card:{new}'
        if kind=='legacy':
            db.execute('UPDATE credit_cards SET bank_name=?,display_name=?,last_four=?,active=? WHERE id=? AND user_id=?',(bank.strip(),name.strip(),last,int(active),record_id,user_id))
        return {'id':reference}


def resolve_card(db,user_id,reference):
    match=re.fullmatch(r'(legacy|card):([0-9]+)',str(reference))
    if not match:raise ValueError('Kart bulunamadı.')
    kind,raw=match.groups();record_id=core.record_id(raw)
    if kind=='legacy':
        row=db.execute('SELECT c.*,p.payment_due_on FROM credit_cards c LEFT JOIN profile_cards p ON p.legacy_card_id=c.id AND p.user_id=c.user_id WHERE c.id=? AND c.user_id=?',(record_id,user_id)).fetchone()
        if row:
            result=dict(row)
            if not result['payment_due_on']:
                known=db.execute('SELECT COALESCE(MIN(CASE WHEN due_on>=? THEN due_on END),MAX(due_on)) FROM card_statements WHERE card_id=? AND user_id=?',(today().isoformat(),record_id,user_id)).fetchone()
                result['payment_due_on']=known[0]
    else:
        row=db.execute('SELECT * FROM profile_cards WHERE id=? AND user_id=? AND legacy_card_id IS NULL',(record_id,user_id)).fetchone()
        result=dict(row) if row else None
    if not row:raise ValueError('Kart hesabınıza ait değil veya bulunamadı.')
    return {**result,'kind':kind,'record_id':record_id}


def _move_unpaid_expenses(db,user_id,kind,record_id,due):
    field='legacy_card_id' if kind=='legacy' else 'profile_card_id'
    rows=db.execute(f'''SELECT r.id,d.purchased_on FROM recurring_expenses r JOIN payment_plan_details d ON d.recurring_id=r.id AND d.user_id=r.user_id
        WHERE r.user_id=? AND d.{field}=? AND d.entry_kind='expense'
        AND NOT EXISTS(SELECT 1 FROM payment_events p WHERE p.recurring_id=r.id AND p.user_id=r.user_id)
        AND NOT EXISTS(SELECT 1 FROM recurring_occurrences o JOIN expenses e ON e.id=o.expense_id AND e.user_id=o.user_id WHERE o.recurring_id=r.id AND o.user_id=r.user_id)''',(user_id,record_id)).fetchall()
    if any(row['purchased_on'] and row['purchased_on']>due for row in rows):
        raise ValueError('Yeni vade, bu karta kayıtlı bir alışverişten önce olamaz.')
    for row in rows:
        db.execute('UPDATE recurring_expenses SET start_on=?,end_on=? WHERE id=? AND user_id=?',(due,due,row['id'],user_id))
