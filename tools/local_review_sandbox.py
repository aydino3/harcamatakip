"""Private, isolated local review copy. Never opens a production database."""
import json,os,secrets,sys,tempfile
from pathlib import Path
from werkzeug.serving import make_server, WSGIRequestHandler


def run():
    snapshot=json.load(sys.stdin)
    for key in ('EXPENSE_DATABASE_URL','POSTGRES_URL','DATABASE_URL','VERCEL'):
        os.environ.pop(key,None)
    os.environ['APP_ENV']='development';os.environ['PRIVATE_BETA']='0'
    os.umask(0o077)
    folder=Path(tempfile.mkdtemp(prefix='finance-private-review-'))
    os.chmod(folder,0o700)
    os.environ['EXPENSE_DB_PATH']=str(folder/'review.sqlite3')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import app as core
    import simple_cashflow as finance
    import profile_settings as profiles
    from webapp import create_app
    web=create_app()
    user=core.create_user(snapshot['profile']['email'],secrets.token_urlsafe(48))
    with core.connect() as db:
        # Remove only defaults just created in this new, empty local test account.
        db.execute('DELETE FROM categories WHERE user_id=?',(user,))
        db.execute('DELETE FROM payment_methods WHERE user_id=?',(user,))
        db.execute('UPDATE users SET display_name=? WHERE id=?',(snapshot['profile']['display_name'],user))
        sequence=(('categories','categories'),('payment_methods','payment_methods'),('credit_cards','credit_cards'),('statements','card_statements'),('recurring','recurring_expenses'),('income_rules','income_rules'),('income_rule_amounts','income_rule_amounts'),('income_records','income_records'),('expenses','expenses'),('occurrences','recurring_occurrences'),('repayments','card_repayments'),('adjustments','cash_adjustments'),('opening','cash_settings'))
        for key,table in sequence:
            columns={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            rows=snapshot.get(key,[])
            if isinstance(rows,dict):rows=[rows]
            for source in rows:
                row={name:value for name,value in source.items() if name in columns and name!='user_id'}
                row['user_id']=user
                names=list(row)
                db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES({','.join('?' for _ in names)})",tuple(row[name] for name in names))
        for card in snapshot.get('credit_cards',[]):
            due=snapshot['profile'].get('known_due_dates',{}).get(str(card['id']))
            if due:
                db.execute('INSERT INTO profile_cards(user_id,legacy_card_id,bank_name,display_name,last_four,payment_due_on,active) VALUES(?,?,?,?,?,?,?)',(user,card['id'],card['bank_name'],card['display_name'],card.get('last_four',''),due,card['active']))
        if db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Yerel kopyanın ilişki kontrolü başarısız.')
    # Before enabling edits, verify the seeded financial view equals the source snapshot.
    original={**snapshot,'opening':(snapshot.get('opening') or [None])[0],'payment_events':[]}
    for start,end in (('2026-01-01','2027-12-31'),):
        source=finance.calculate(original,start,end)
        copy=finance.calculate(finance.read_data(user),start,end)
        if source['totals']!=copy['totals'] or source['net_minor']!=copy['net_minor'] or len(source['events'])!=len(copy['events']):raise ValueError('Yerel finansal kopya kaynakla uzlaşmadı.')
    del snapshot
    token,csrf=core.create_session(user)
    @web.after_request
    def mark_review(response):
        if response.is_json and response.status_code==200 and __import__('flask').request.path=='/api/simple-cashflow':
            payload=response.get_json();payload['local_sandbox']=True;response.set_data(json.dumps(payload,ensure_ascii=False));response.content_type='application/json'
        return response
    class Quiet(WSGIRequestHandler):
        def log(self,*args,**kwargs):pass
    server=make_server('127.0.0.1',0,web,threaded=True,request_handler=Quiet)
    # This line is read only by the local proxy, never printed to the tool transcript.
    print(json.dumps({'url':'http://127.0.0.1:'+str(server.server_port),'cookie':core.COOKIE+'='+token,'csrf':csrf}),flush=True)
    server.serve_forever()

if __name__=='__main__':
    try:run()
    except Exception as error:
        import traceback
        frames=traceback.extract_tb(error.__traceback__)
        location=' > '.join(Path(frame.filename).name+':'+str(frame.lineno) for frame in frames)
        print('REVIEW_ERROR '+type(error).__name__+' '+location,file=sys.stderr,flush=True)
        sys.exit(1)

