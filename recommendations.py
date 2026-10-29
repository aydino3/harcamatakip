"""Read-only, explainable budgeting insights over the existing account ledger."""
from collections import defaultdict
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

import card_schedule
import simple_cashflow as finance


def _months(first, last):
    month=first
    while month<=last:
        yield month
        month=card_schedule.shift_month(month,1)


def _strings(value):
    if isinstance(value,dict):return {key:str(item) if key.endswith('_minor') and item is not None else _strings(item) for key,item in value.items()}
    if isinstance(value,list):return [_strings(item) for item in value]
    return value


def analyze(data, month, as_of=None):
    """Pure analysis: no database, external calls, materialization, or assumed salary."""
    if not isinstance(month,str) or not re.fullmatch(r'\d{4}-\d{2}',month):raise ValueError('Tavsiye ayını kontrol edin.')
    date.fromisoformat(month+'-01')
    as_of=as_of or finance.today();end=as_of+timedelta(days=30)
    reports={}
    def report(value):
        if value not in reports:reports[value]=finance.card_statement_spending(data,value)
        return reports[value]
    selected=report(month);position=finance.cash_position(data,as_of)
    issues=[];items=[]
    def issue(code,count=1,amount_minor=None,section='profile'):
        issues.append({'code':code,'count':count,'amount_minor':amount_minor,'section':section})
    def item(code,severity,basis,section='payments',**facts):
        items.append({'code':code,'severity':severity,'basis':basis,'section':section,'facts':facts})
    if position['balance_minor'] is None:issue('opening_missing',section='overview')
    if int(selected['budget']['income_minor'])<=0:issue('income_missing',section='income')
    cuts=sum(row['missing_closing_day'] for row in selected['cards'])
    if cuts:issue('closing_missing',cuts)
    missing=sum(row['total_minor'] is None and not row['missing_closing_day'] for row in selected['cards'])
    if missing:issue('statement_missing',missing,section='payments')
    inconsistent=any(row['inconsistent'] for row in selected['cards'])
    if inconsistent:issue('statement_inconsistent',section='payments')
    unassigned=bool(selected['unassigned_installments'])
    if unassigned:issue('unassigned_installments',len(selected['unassigned_installments']),section='payments')

    # Include overdue obligations, with a bounded, explicit history window.
    earliest=[as_of.replace(day=1).isoformat()]
    earliest.extend(row['start_on'] for row in data.get('recurring',[]) if row['status']=='active')
    earliest.extend(row['due_on'] for row in finance.statement_balances(data) if int(row['remaining_minor'])>0)
    earliest.extend(row['due_on'] for row in finance.monthly_total_balances(data) if int(row['remaining_minor'])>0)
    lower=end-timedelta(days=3660)
    start=max(lower,min(date.fromisoformat(value) for value in earliest))
    history_limited=any(value<lower.isoformat() for value in earliest)
    if history_limited:issue('history_limited',section='payments')
    events=finance.calculate(data,start.isoformat(),end.isoformat(),as_of)['events']
    payments=[row for row in events if row['kind']=='payment' and row['status']!='paid']
    expected=[row for row in events if row['kind']=='income' and row['status']!='received' and row['date']>=as_of.isoformat()]
    overdue=[row for row in payments if row['date']<as_of.isoformat()]
    late_income={row['id']:row for row in events if row['kind']=='income' and row['status']!='received' and row['date']<as_of.isoformat()}
    if not (data.get('opening') or {}).get('auto_income_on_due'):
        for row in data.get('income_records',[]):
            if row['status']=='expected' and row['expected_on']<as_of.isoformat():late_income['record:'+str(row['id'])]={'amount_minor':int(row['amount_minor'])}
        # Materialized events use income:<id>; count the record once.
        actual_ids={row.get('record_id') for row in events if row['kind']=='income' and row['date']<as_of.isoformat()}
        for identity,row in list(late_income.items()):
            if identity.startswith('record:') and int(identity.split(':')[1]) in actual_ids:del late_income[identity]
    if late_income:issue('overdue_income',len(late_income),sum(int(row['amount_minor']) for row in late_income.values()),'income')
    automatic=int(position.get('automatic_income_minor',0))
    if automatic:issue('automatic_income',amount_minor=automatic,section='income')

    future_missing=[]
    for candidate in _months(card_schedule.shift_month(as_of.strftime('%Y-%m'),-1),card_schedule.shift_month(end.strftime('%Y-%m'),1)):
        view=finance.monthly_card_debt(data,candidate)
        for row in view['cards']:
            setting=finance.configured_card(data,row['card_ref'])
            if not setting.get('active',1):continue
            due=row['due_on'] or row['configured_due_on']
            if due and as_of.isoformat()<=due<=end.isoformat():
                if row['source']=='missing':future_missing.append({'card_ref':row['card_ref'],'month':candidate,'due_on':due})
                if row['inconsistent']:inconsistent=True
        if view['unassigned_installments'] and as_of.strftime('%Y-%m')<=candidate<=end.strftime('%Y-%m'):unassigned=True
    if future_missing:issue('future_statements_missing',len(future_missing),section='payments')
    blocked=inconsistent or unassigned
    if blocked and not any(row['code'] in ('statement_inconsistent','unassigned_installments') for row in issues):issue('payment_links_inconsistent',section='payments')

    # Same-day flows are netted. Late expected income is never treated as available today.
    daily=defaultdict(lambda:{'income':0,'payments':0})
    for row in payments:daily[max(row['date'],as_of.isoformat())]['payments']+=int(row['amount_minor'])
    for row in expected:daily[row['date']]['income']+=int(row['amount_minor'])
    cash={'available':position['balance_minor'] is not None,'balance_minor':position['balance_minor'],'automatic_income_minor':automatic,'expected_income_minor':sum(int(row['amount_minor']) for row in expected),'pending_payments_minor':sum(int(row['amount_minor']) for row in payments),'overdue_payments_minor':sum(int(row['amount_minor']) for row in overdue),'reliable':not blocked and not history_limited and not future_missing,'blocked':blocked,'timeline':[],'minimum_minor':None,'without_income_minimum_minor':None,'first_gap':None,'without_income_first_gap':None}
    if cash['available']:
        balance=int(position['balance_minor']);without=balance;minimum=balance;minimum_without=balance
        first_gap={'date':as_of.isoformat(),'amount_minor':-balance} if balance<0 else None
        without_gap=dict(first_gap) if first_gap else None
        for day,amounts in sorted(daily.items()):
            balance+=amounts['income']-amounts['payments'];without-=amounts['payments']
            minimum=min(minimum,balance);minimum_without=min(minimum_without,without)
            if balance<0 and first_gap is None:first_gap={'date':day,'amount_minor':-balance}
            if without<0 and without_gap is None:without_gap={'date':day,'amount_minor':-without}
            cash['timeline'].append({'date':day,'income_minor':amounts['income'],'payments_minor':amounts['payments'],'balance_minor':balance,'without_income_balance_minor':without})
        cash.update(ending_minor=balance,without_income_ending_minor=without,minimum_minor=minimum,without_income_minimum_minor=minimum_without,first_gap=first_gap,without_income_first_gap=without_gap)
        if not blocked:
            if int(position['balance_minor'])<0:item('negative_balance','urgent','cash','overview',amount_minor=-int(position['balance_minor']))
            elif first_gap:item('cash_gap','urgent','cash',date=first_gap['date'],amount_minor=first_gap['amount_minor'],minimum_minor=-minimum,partial=not cash['reliable'])
            elif without_gap:item('income_dependency','warning','cash','income',date=without_gap['date'],amount_minor=without_gap['amount_minor'],income_minor=cash['expected_income_minor'])
    if overdue and not blocked:item('overdue_payments','warning','cash',count=len(overdue),amount_minor=cash['overdue_payments_minor'])

    income=int(selected['budget']['income_minor']);fixed=int(selected['budget']['fixed_minor']);extra=int(selected['extra_minor'])
    if income>0 and selected['budget']['allowance_minor'] is not None:
        if fixed>income:item('fixed_gap','warning','spending','payments',month=month,income_minor=income,fixed_minor=fixed,amount_minor=fixed-income)
        elif selected['complete'] and extra>income-fixed:item('extra_gap','warning','spending','payments',month=month,income_minor=income,fixed_minor=fixed,extra_minor=extra,capacity_minor=income-fixed,amount_minor=extra-(income-fixed),closed=all(row['closing_on'] and row['closing_on']<=as_of.isoformat() for row in selected['cards']))
        elif selected['complete'] and selected['cards']:item('spending_capacity','info','spending','payments',month=month,capacity_minor=income-fixed,extra_minor=extra,remaining_minor=income-fixed-extra)
    known=[row for row in selected['cards'] if row['extra_minor'] is not None and int(row['extra_minor'])>0]
    if known:
        largest=max(known,key=lambda row:(int(row['extra_minor']),row['card_ref']))
        share=(int(largest['extra_minor'])*100+extra//2)//extra
        item('extra_focus','info','spending','payments',month=month,card_name=largest['bank_name']+' · '+largest['display_name'],amount_minor=int(largest['extra_minor']),total_minor=extra,share_percent=share,complete=selected['complete'])

    future=[]
    for offset in (1,2,3):
        future_month=card_schedule.shift_month(as_of.strftime('%Y-%m'),offset);view=report(future_month);b=view['budget'];present=int(b['income_minor'])>0;valid=present and b['allowance_minor'] is not None
        future.append({'month':future_month,'income_minor':int(b['income_minor']) if present else None,'fixed_minor':int(b['fixed_minor']),'capacity_minor':max(0,int(b['income_minor'])-int(b['fixed_minor'])) if valid else None,'gap_minor':max(0,int(b['fixed_minor'])-int(b['income_minor'])) if valid else None,'complete':view['complete'],'fixed_complete':b['allowance_minor'] is not None})
    pressure=next((row for row in future if row['gap_minor'] and row['gap_minor']>0),None)
    if pressure:item('future_fixed_gap','warning','plan','payments',month=pressure['month'],income_minor=pressure['income_minor'],fixed_minor=pressure['fixed_minor'],amount_minor=pressure['gap_minor'])
    endings=[]
    for rule in data.get('recurring',[]):
        if rule['status']!='active' or rule.get('entry_kind')!='installment' or rule['frequency']!='monthly' or not rule.get('end_on'):continue
        final=finance.payment_due(data,rule,rule['end_on'])
        if as_of.isoformat()<=final<=(as_of+timedelta(days=90)).isoformat():endings.append({'description':rule['description'],'due_on':final,'amount_minor':int(rule['amount_minor']),'first_free_month':card_schedule.shift_month(final[:7],1)})
    endings.sort(key=lambda row:(row['due_on'],row['description']))
    if endings:
        first=endings[0];item('installment_end','info','plan','payments',date=first['due_on'],month=first['first_free_month'],amount_minor=first['amount_minor'],description=first['description'])
    older=[report(card_schedule.shift_month(month,-index)) for index in (1,2)]
    complete_history=selected['cards'] and all(view['complete'] and view['cards'] and all(row['closing_on'] and row['closing_on']<=as_of.isoformat() for row in view['cards']) for view in [selected,*older])
    if complete_history:
        average=(sum(int(view['extra_minor']) for view in older)+1)//2
        if extra>average:item('spending_comparison','info','spending','payments',month=month,average_minor=average,extra_minor=extra,amount_minor=extra-average)

    if issues:item('data_quality','warning' if blocked or position['balance_minor'] is None else 'info','data',issues[0]['section'],count=len(issues))
    order={'urgent':0,'warning':1,'info':2};items.sort(key=lambda row:(order[row['severity']],0 if row['code']=='data_quality' else 1,row['code']))
    upcoming=[{key:row[key] for key in ('id','date','description','amount_minor','status','kind','monthly_card_ref','statement_id') if key in row} for row in sorted(payments,key=lambda row:(row['date'],row['id']))[:8]]
    return _strings({'month':month,'as_of':as_of.isoformat(),'horizon_end':end.isoformat(),'read_only':True,'items':items,'issues':issues,'cash':cash,'spending':{'month':month,'complete':selected['complete'],'income_minor':income if income>0 else None,'fixed_minor':fixed,'extra_minor':extra,'capacity_minor':selected['budget']['allowance_minor'] if income>0 else None,'cards':len(selected['cards']),'comparison_available':bool(complete_history)},'future_months':future,'installment_endings':endings[:8],'upcoming_payments':upcoming,'source_counts':{key:len(data.get(key,[])) for key in ('income_records','income_rules','recurring','profile_cards','cards','card_monthly_totals','payment_events','card_monthly_payments')},'coverage':{'payment_integrity_blocked':blocked,'payment_complete':cash['reliable'],'spending_complete':selected['complete'],'history_limited':history_limited}})


def for_user(user_id,month):
    result=analyze(finance.read_data(user_id),month)
    result['generated_at']=datetime.now(ZoneInfo('Europe/Istanbul')).isoformat()
    return result
