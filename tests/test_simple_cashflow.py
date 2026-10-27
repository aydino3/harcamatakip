"""Sentetik gelir/ödeme ve salt okunur takvim regresyonları."""
from pathlib import Path
from datetime import date
import tempfile
import unittest

import app as core
import simple_cashflow as finance
import webapp


class SimpleCashflowTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.previous=core.DB_PATH
        core.DB_PATH=Path(self.temp.name)/"synthetic.sqlite3"
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(setattr,core,"DB_PATH",self.previous)
        core.init_db();finance.initialize()
        self.user=core.create_user("cashflow-fixture@example.invalid","synthetic-only-password")
        self.other=core.create_user("other-cashflow@example.invalid","synthetic-only-password")

    def plan(self,**changes):
        return finance.save_payment_plan(self.user,{"description":"Sentetik taksit","amount":"100.00","start_on":"2024-01-09","end_on":"2024-03-08","frequency":"monthly","request_key":"plan-create-key-0001",**changes})["id"]

    def card(self,owner=None,**changes):
        import profile_settings
        owner=owner or self.user
        payload={'bank_name':'Sentetik banka','display_name':'Test kart','payment_due_on':'2024-02-05',**changes}
        return profile_settings.save_card(owner,payload)['id']

    def test_weekly_income_and_last_day_month_without_specific_date(self):
        from unittest.mock import patch
        weekly={'description':'Haftalık','amount':'100','expected_on':'2024-01-02','frequency':'weekly','schedule_mode':'anchored','request_key':'weekly-income-key-01'}
        finance.save_monthly_income(self.user,weekly)
        self.assertEqual([r['date'] for r in finance.overview(self.user,'2024-01-01','2024-01-23')['events']],['2024-01-02','2024-01-09','2024-01-16','2024-01-23'])
        payload={'description':'Ay sonu','amount':'200','start_month':'2024-01','frequency':'monthly','schedule_mode':'last_day','request_key':'lastday-income-key-01'}
        self.assertEqual(finance.save_monthly_income(self.user,payload),finance.save_monthly_income(self.user,payload))
        rows=[r['date'] for r in finance.overview(self.user,'2024-01-01','2024-03-31')['events'] if r['description']=='Ay sonu']
        self.assertEqual(rows,['2024-01-31','2024-02-29','2024-03-31'])
        with self.assertRaises(ValueError):finance.save_monthly_income(self.user,{**weekly,'schedule_mode':'last_day'})

    def test_card_oneoff_uses_owned_due_date_and_does_not_repeat_or_double_count(self):
        card=self.card()
        payload={'description':'Tek alışveriş','amount':'120','entry_kind':'expense','card_ref':card,'purchased_on':'2024-01-20','request_key':'oneoff-purchase-key-01'}
        result=finance.save_payment_plan(self.user,payload)
        self.assertEqual(finance.save_payment_plan(self.user,payload),result)
        data=finance.overview(self.user,'2024-01-01','2024-06-30')
        self.assertEqual([(r['date'],r['amount_minor']) for r in data['events']],[('2024-02-05','12000')])
        self.assertEqual(data['rules'][0]['entry_kind'],'expense')
        self.assertEqual(data['rules'][0]['card_ref'],card)
        finance.record_payment(self.user,result['id'],{'scheduled_on':'2024-02-05','paid_on':'2024-02-05','amount':'30','request_key':'oneoff-pay-key-0001'})
        after=finance.overview(self.user,'2024-01-01','2024-06-30')
        self.assertEqual(after['totals']['payments_paid_minor'],'3000')
        self.assertEqual(after['totals']['payments_planned_minor'],'9000')
        self.assertEqual(after['net_minor'],'-12000')
        with self.assertRaises(ValueError):finance.record_payment(self.user,result['id'],{'scheduled_on':'2024-03-05','paid_on':'2024-03-05','amount':'30','request_key':'oneoff-pay-key-0002'})

    def test_card_ownership_missing_stale_due_and_inactive_cards_are_rejected(self):
        import profile_settings
        reference=self.card(self.other)
        base={'description':'Yetki kontrolü','amount':'10','entry_kind':'expense','purchased_on':'2024-01-02','request_key':'card-validation-0001'}
        with self.assertRaises(ValueError):finance.save_payment_plan(self.user,{**base,'card_ref':reference})
        own=self.card()
        for change in ({'start_on':'2024-02-06'},{'purchased_on':'2024-03-01'}):
            with self.assertRaises(ValueError):finance.save_payment_plan(self.user,{**base,'card_ref':own,**change})
        card=profile_settings.get_profile(self.user)['cards'][0]
        profile_settings.save_card(self.user,{**card,'active':0})
        with self.assertRaises(ValueError):finance.save_payment_plan(self.user,{**base,'card_ref':own})

    def test_installment_card_link_end_date_and_paid_history_are_preserved(self):
        card=self.card()
        rule=self.plan(entry_kind='installment',card_ref=card)
        item=finance.overview(self.user,'2024-01-01','2024-03-31')['rules'][0]
        self.assertEqual(item['card_ref'],card)
        self.assertEqual(item['end_on'],'2024-03-08')
        with self.assertRaises(ValueError):self.plan(entry_kind='installment',end_on='',request_key='end-required-key-0001')
        finance.record_payment(self.user,rule,{'scheduled_on':'2024-01-09','paid_on':'2024-01-09','amount':'10','request_key':'history-paid-key-01'})
        with self.assertRaises(ValueError):finance.save_payment_plan(self.user,{**item,'amount':'200','revision':item['revision']})

    def test_card_due_change_moves_only_unpaid_oneoff_and_keeps_paid_history(self):
        import profile_settings
        card=self.card()
        base={'description':'Alışveriş','amount':'100','entry_kind':'expense','card_ref':card,'purchased_on':'2024-01-01'}
        first=finance.save_payment_plan(self.user,{**base,'request_key':'due-change-key-0001'})['id']
        second=finance.save_payment_plan(self.user,{**base,'request_key':'due-change-key-0002'})['id']
        finance.record_payment(self.user,first,{'scheduled_on':'2024-02-05','paid_on':'2024-02-05','amount':'100','request_key':'due-paid-key-0001'})
        old=profile_settings.get_profile(self.user)['cards'][0]
        profile_settings.save_card(self.user,{**old,'payment_due_on':'2024-02-10'})
        rules={r['id']:r for r in finance.overview(self.user,'2024-01-01','2024-03-31')['rules']}
        self.assertEqual(rules[first]['start_on'],'2024-02-05')
        self.assertEqual(rules[second]['start_on'],'2024-02-10')
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-03-31')['net_minor'],'-20000')

    def test_bank_regular_payment_weekly_monthly_end_and_no_card(self):
        base={'description':'Banka ödemesi','amount':'75','entry_kind':'regular','card_ref':'','frequency':'weekly','start_on':'2024-01-02','end_on':'2024-01-23','request_key':'bank-weekly-key-0001'}
        result=finance.save_payment_plan(self.user,base)
        self.assertEqual(finance.save_payment_plan(self.user,base),result)
        rows=finance.overview(self.user,'2024-01-01','2024-03-31')
        self.assertEqual([r['date'] for r in rows['events']],['2024-01-02','2024-01-09','2024-01-16','2024-01-23'])
        self.assertEqual(rows['net_minor'],'-30000')
        self.assertEqual(rows['rules'][0]['entry_kind'],'regular')
        self.assertEqual(rows['rules'][0]['card_ref'],'')
        monthly={**base,'frequency':'monthly','start_on':'2024-01-31','end_on':'2024-03-29','request_key':'bank-monthly-key-01'}
        finance.save_payment_plan(self.user,monthly)
        for change in ({'end_on':''},{'card_ref':self.card(self.other)},{'frequency':'yearly'}):
            with self.assertRaises(ValueError):finance.save_payment_plan(self.user,{**base,**change,'request_key':'invalid-bank-key-01'})

    def test_regular_payment_card_is_optional_owned_and_not_an_extra(self):
        card=self.card()
        rule=self.plan(entry_kind='regular',card_ref=card,frequency='weekly',end_on='2024-01-23')
        view=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(view['rules'][0]['card_ref'],card)
        self.assertEqual(view['card_spending']['total_minor'],'0')
        self.assertEqual(view['extra_budget']['fixed_minor'],'30000')
        self.assertEqual(view['extra_budget']['fixed_gap_minor'],'30000')
        self.assertEqual(finance.overview(self.other,'2024-01-01','2024-01-31')['card_spending']['count'],0)

    def test_extra_spending_purchase_month_per_card_gross_survives_repayment(self):
        card=self.card()
        rule=finance.save_payment_plan(self.user,{'description':'Market','amount':'120.01','entry_kind':'expense','card_ref':card,'purchased_on':'2024-01-20','request_key':'extra-month-key-0001'})['id']
        self.plan(entry_kind='installment',card_ref=card)
        january=finance.overview(self.user,'2024-01-01','2024-01-31')
        february=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(january['card_spending']['total_minor'],'12001')
        self.assertEqual(january['card_spending']['count'],1)
        self.assertEqual(january['card_spending']['cards'][0]['entries'][0]['payment_due_on'],'2024-02-05')
        self.assertEqual(february['card_spending']['total_minor'],'0')
        finance.record_payment(self.user,rule,{'scheduled_on':'2024-02-05','paid_on':'2024-02-05','amount':'30','request_key':'extra-paid-key-0001'})
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['card_spending'],january['card_spending'])
        self.assertEqual(january['extra_budget']['fixed_minor'],'10000')
        self.assertEqual(january['extra_budget']['excess_minor'],'12001')

    def test_legacy_extras_exclude_installments_and_materialized_regular_expenses(self):
        dataset={'cards':[{'id':1,'bank_name':'Test','display_name':'Kart','active':1}], 'statements':[{'id':2,'due_on':'2024-02-05'}],
          'expenses':[{'id':i,'card_id':1,'statement_id':2,'spent_on':'2024-01-20','description':'Kayit','amount_minor':101,'installments_count':1} for i in (1,2,3)],
          'installment_plans':[{'id':1,'expense_id':2}], 'occurrences':[{'expense_id':3}], 'recurring':[]}
        extras=finance.card_spending(dataset,'2024-01-01','2024-01-31')
        self.assertEqual((extras['count'],extras['total_minor']),(1,'101'))
        self.assertEqual(finance.card_spending(dataset,'2024-02-01','2024-02-29')['total_minor'],'0')
        view={'events':[{'recurring_id':5,'amount_minor':'50000'}], 'totals':{'income_received_minor':'10000','income_expected_minor':'60000'}}
        dataset['recurring']=[{'id':5,'entry_kind':'regular'}]
        budget=finance.extra_budget(dataset,view,'2024-01-01','2024-01-31')
        self.assertEqual(budget['allowance_minor'],'20000')
        self.assertEqual(budget['remaining_minor'],'19899')
        self.assertEqual(budget['excess_minor'],'0')

    def test_monthly_total_minus_card_fixed_payments_isolated_and_recalculates(self):
        card=self.card()
        other_card=self.card(display_name='İkinci kart')
        rule=self.plan(entry_kind='installment',card_ref=card)
        self.plan(entry_kind='regular',card_ref=card,amount='20',request_key='monthly-regular-001')
        self.plan(entry_kind='installment',card_ref=other_card,amount='50',request_key='monthly-other-0001')
        before=finance.overview(self.user,'2024-01-01','2024-01-31')
        payload={'card_ref':card,'month':'2024-01','amount':'300.01','version':0}
        self.assertEqual(finance.save_card_monthly_total(self.user,payload),{'version':1})
        self.assertEqual(finance.save_card_monthly_total(self.user,payload),{'version':1})
        view=finance.overview(self.user,'2024-01-01','2024-01-31')
        row=next(row for row in view['card_monthly_debt']['cards'] if row['card_ref']==card)
        self.assertEqual((row['total_minor'],row['installment_minor'],row['regular_minor'],row['extra_minor']),('30001','10000','2000','18001'))
        self.assertEqual(view['totals'],before['totals'])
        self.assertEqual(view['cash_position'],before['cash_position'])
        with self.assertRaises(finance.Conflict):finance.save_card_monthly_total(self.user,{**payload,'amount':'310'})
        self.assertEqual(finance.save_card_monthly_total(self.user,{**payload,'amount':'310','version':1}),{'version':2})
        self.assertNotEqual(finance.overview(self.user,'2024-02-01','2024-02-29')['card_monthly_debt']['cards'][0]['source'],'entered')
        with self.assertRaises(ValueError):finance.save_card_monthly_total(self.other,payload)
        for patch in ({'amount':'-1'},{'month':'2024-13'},{'month':'2024-1'},{'version':True}):
            with self.assertRaises(ValueError):finance.save_card_monthly_total(self.user,{**payload,**patch})
        finance.save_card_monthly_total(self.user,{**payload,'amount':'10','version':2})
        row=next(row for row in finance.overview(self.user,'2024-01-01','2024-01-31')['card_monthly_debt']['cards'] if row['card_ref']==card)
        self.assertTrue(row['inconsistent']);self.assertIsNone(row['extra_minor'])

    def test_monthly_total_missing_card_relationship_is_explicit(self):
        self.card()
        self.plan()
        view=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(len(view['card_monthly_debt']['unassigned_installments']),1)
        self.assertFalse(view['card_monthly_debt']['complete'])
        self.assertIsNone(view['card_monthly_debt']['cards'][0]['total_minor'])

    def test_income_api_accepts_weekly_and_month_end_and_rejects_unknown_frequency(self):
        client=webapp.create_app({'TESTING':True,'PRIVATE_BETA':False}).test_client()
        login=client.post('/api/login',json={'email':'cashflow-fixture@example.invalid','password':'synthetic-only-password'})
        headers={'X-CSRF-Token':login.json['csrf']}
        base={'description':'API gelir','amount':'10','expected_on':'2024-01-02','request_key':'api-weekly-key-0001','frequency':'weekly'}
        self.assertEqual(client.post('/api/simple-income',json=base,headers=headers).status_code,200)
        payload={**base,'frequency':'monthly','schedule_mode':'last_day','start_month':'2024-02','request_key':'api-lastday-key-01'}
        del payload['expected_on']
        self.assertEqual(client.post('/api/simple-income',json=payload,headers=headers).status_code,200)
        self.assertEqual(client.post('/api/simple-income',json={**base,'frequency':'yearly'},headers=headers).status_code,400)

    def statement_purchase(self,amount='120',spent='2024-01-05',installments=1):
        card=core.save_credit_card(self.user,{'bank_name':'Sentetik','display_name':'Ekstre kartı','closing_day':22,'due_day':3})
        expense=core.save_expense(self.user,{'amount':amount,'spent_on':spent,'description':'Ekstre alışverişi','card_id':card,'installments_count':installments,'first_installment_on':spent})
        return card,expense

    def test_existing_card_purchase_forecasts_on_statement_due_not_purchase_day(self):
        card,expense=self.statement_purchase()
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['net_minor'],'0')
        result=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(result['net_minor'],'-12000')
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['events'][0]['date'],'2024-02-03')
        self.assertEqual(result['events'][0]['card_id'],card)

    def test_statement_partial_payment_is_idempotent_and_does_not_double_count(self):
        card,expense=self.statement_purchase()
        statement=finance.overview(self.user,'2024-01-01','2024-03-31')['statements'][0]
        payload={'statement_id':statement['id'],'amount':'30','paid_on':'2024-02-03','request_key':'statement-payment-0001'}
        first=finance.record_statement_payment(self.user,payload)
        self.assertFalse(finance.record_statement_payment(self.user,payload)['created'])
        result=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(result['totals']['payments_paid_minor'],'3000')
        self.assertEqual(result['totals']['payments_planned_minor'],'9000')
        self.assertEqual(result['net_minor'],'-12000')
        with self.assertRaises(ValueError):finance.record_statement_payment(self.user,{**payload,'amount':'100','request_key':'statement-payment-0002'})
        with self.assertRaises(ValueError):finance.record_statement_payment(self.other,{**payload,'request_key':'statement-payment-0003'})

    def test_general_card_payment_reduces_oldest_statement_once(self):
        card,_=self.statement_purchase()
        core.record_card_repayment(self.user,card,{'amount':'40','paid_on':'2024-02-02'})
        result=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(result['totals']['payments_paid_minor'],'4000')
        self.assertEqual(result['totals']['payments_planned_minor'],'8000')
        self.assertEqual(result['net_minor'],'-12000')

    def test_actual_installment_purchase_uses_installment_shares_not_full_purchase(self):
        self.statement_purchase(amount='120',installments=3)
        result=finance.overview(self.user,'2024-01-01','2024-06-30')
        events=[e for e in result['events'] if e.get('statement_id')]
        self.assertEqual([e['amount_minor'] for e in events],['4000','4000','4000'])
        self.assertEqual(result['net_minor'],'-12000')

    def test_opening_is_separate_from_income_and_actual_payment_updates_cash(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01'})
        self.assertEqual(finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01'}),{'ok':True})
        with self.assertRaises(finance.Conflict):finance.save_opening(self.user,{'amount':'600','effective_on':'2024-01-01'})
        plan=self.plan()
        finance.save_income_once(self.user,{'description':'Alınan gelir','amount':'100','expected_on':'2024-01-02','status':'received','request_key':'opening-income-key-01'})
        finance.record_payment(self.user,plan,{'scheduled_on':'2024-01-09','paid_on':'2024-01-09','amount':'25','request_key':'opening-paid-key-0001'})
        result=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(result['totals']['income_received_minor'],'10000')
        self.assertEqual(result['cash_position']['balance_minor'],'57500')
        self.assertEqual(result['net_minor'],'0')

    def test_cash_balance_excludes_future_income_and_unpaid_card_purchases(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01'})
        self.statement_purchase()
        core.save_income(self.user,{'description':'Beklenen','amount':'200','expected_on':'2024-02-03'})
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,31))['balance_minor'],'50000')
        with self.assertRaises(ValueError):finance.save_opening(self.user,{'amount':'-1','effective_on':'2024-01-01'})

    def test_projected_balance_adds_opening_once_and_includes_unpaid_overdue(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-10'})
        self.plan(start_on='2024-01-09',end_on='2024-01-09')
        finance.save_income_once(self.user,{'description':'Gelecek gelir','amount':'200','expected_on':'2024-01-31','status':'expected','request_key':'projection-income-001'})
        data=finance.read_data(self.user)
        self.assertEqual(finance.projected_balance(data,'2024-01-31',date(2024,1,10)),60000)
        self.assertEqual(finance.cash_position(data,date(2024,1,10))['balance_minor'],'50000')
        finance.record_payment(self.user,data['recurring'][0]['id'],{'amount':'25','scheduled_on':'2024-01-09','paid_on':'2024-01-10','request_key':'projection-paid-0001'})
        self.assertEqual(finance.projected_balance(finance.read_data(self.user),'2024-01-31',date(2024,1,10)),60000)
        self.assertIsNone(finance.projected_balance(data,'2024-01-09',date(2024,1,10)))

    def test_explicit_final_payment_is_included_even_if_day_is_earlier_than_first(self):
        self.plan(start_on="2024-12-09",end_on="2025-01-08")
        result=finance.overview(self.user,"2024-12-01","2025-01-31")
        self.assertEqual([event["date"] for event in result["events"]],["2024-12-09","2025-01-08"])
        self.assertEqual(result["net_minor"],"-20000")

    def test_clamped_months_and_explicit_last_date_are_preserved(self):
        self.plan(start_on="2024-01-31",end_on="2024-03-29")
        self.assertEqual([row["date"] for row in finance.overview(self.user,"2024-01-01","2024-03-31")["events"]],["2024-01-31","2024-02-29","2024-03-29"])

    def test_forecast_gets_do_not_materialize_income_or_payments(self):
        core.save_income_rule(self.user,{"description":"Sentetik gelir","amount":"1000","income_type":"additional","frequency":"monthly","start_on":"2024-01-31","schedule_mode":"last_day"})
        self.plan()
        with core.connect() as db:
            before={table:db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("income_records","expenses","recurring_occurrences","payment_events")}
        for _ in range(2):finance.overview(self.user,"2024-01-01","2024-04-30")
        with core.connect() as db:
            self.assertEqual(before,{table:db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before})

    def test_received_income_replaces_virtual_expected_once_on_actual_date(self):
        rule=core.save_income_rule(self.user,{"description":"Maaş fikstürü","amount":"1000","income_type":"additional","frequency":"monthly","start_on":"2024-01-31","schedule_mode":"last_day"})
        core.income_overview(self.user,"2024-01")  # Controlled old materialization.
        payload={"rule_id":rule,"scheduled_on":"2024-01-31","received_on":"2024-02-02","expected_amount_minor":"100000"}
        first=finance.receive_income(self.user,payload)
        self.assertEqual(finance.receive_income(self.user,payload),first)
        result=finance.overview(self.user,"2024-01-01","2024-02-28")
        self.assertEqual(len(result["events"]),1)
        self.assertEqual(result["events"][0]["date"],"2024-02-02")
        self.assertEqual(result["totals"]["income_received_minor"],"100000")

    def test_partial_payment_and_retry_have_one_effect_without_purchase_expense(self):
        rule=self.plan()
        payload={"scheduled_on":"2024-01-09","paid_on":"2024-01-08","amount":"25","request_key":"payment-request-0001"}
        first=finance.record_payment(self.user,rule,payload)
        self.assertFalse(finance.record_payment(self.user,rule,payload)["created"])
        result=finance.overview(self.user,"2024-01-01","2024-01-31")
        self.assertEqual(result["totals"]["payments_paid_minor"],"2500")
        self.assertEqual(result["totals"]["payments_planned_minor"],"7500")
        self.assertEqual(result["net_minor"],"-10000")
        with core.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0],1)
        with self.assertRaises(ValueError):finance.record_payment(self.user,rule,{**payload,"amount":"80","request_key":"payment-request-0002"})

    def test_card_purchase_is_not_a_second_cash_outflow(self):
        card=core.save_credit_card(self.user,{"bank_name":"Sentetik","display_name":"Kart","closing_day":22,"due_day":3})
        core.save_expense(self.user,{"amount":"500","spent_on":"2024-01-01","description":"Sentetik alışveriş","card_id":card})
        self.plan()
        result=finance.overview(self.user,"2024-01-01","2024-01-31")
        self.assertEqual(result["net_minor"],"-10000")
        self.assertEqual(len(result["events"]),1)

    def test_repeat_creation_is_idempotent_and_conflicting_request_is_rejected(self):
        first=self.plan()
        self.assertEqual(self.plan(),first)
        with self.assertRaises(finance.Conflict):self.plan(amount="200")
        with core.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM recurring_expenses").fetchone()[0],1)

    def test_receipt_creation_is_atomic_and_repeat_does_not_duplicate_income(self):
        payload={"description":"Sentetik tahsilat","amount":"15.25","expected_on":"2024-01-01","status":"received","request_key":"income-request-0001"}
        self.assertEqual(finance.save_income_once(self.user,payload),finance.save_income_once(self.user,payload))
        result=finance.overview(self.user,"2024-01-01","2024-01-31")
        self.assertEqual(result["net_minor"],"1525")
        self.assertEqual(result["events"][0]["status"],"received")

    def test_other_users_plan_and_income_cannot_be_accessed(self):
        rule=self.plan()
        with self.assertRaises(ValueError):finance.record_payment(self.other,rule,{"scheduled_on":"2024-01-09","paid_on":"2024-01-09","amount":"10","request_key":"forbidden-request-0001"})
        self.assertEqual(finance.overview(self.other,"2024-01-01","2024-03-31")["events"],[])

    def test_old_reading_does_not_claim_a_bank_balance_when_opening_is_missing(self):
        self.plan()
        result=finance.overview(self.user,"2024-01-01","2024-01-31")
        self.assertIsNone(result["opening"])
        self.assertFalse(result["balance_is_confirmed"])

    def test_stale_edit_does_not_overwrite_newer_plan(self):
        rule=self.plan()
        old=finance.overview(self.user,"2024-01-01","2024-03-31")["rules"][0]
        payload={**old,"amount":"120","revision":old["revision"]}
        finance.save_payment_plan(self.user,payload)
        with self.assertRaises(finance.Conflict):finance.save_payment_plan(self.user,{**payload,"amount":"150"})

    def test_api_auth_csrf_and_exact_money_contract(self):
        self.plan()
        client=webapp.create_app({"TESTING":True,"PRIVATE_BETA":False}).test_client()
        self.assertEqual(client.get("/api/simple-cashflow").status_code,401)
        login=client.post("/api/login",json={"email":"cashflow-fixture@example.invalid","password":"synthetic-only-password"})
        response=client.get("/api/simple-cashflow?start=2024-01-01&end=2024-01-31")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["net_minor"],"-10000")
        self.assertEqual(client.post("/api/simple-income",json={}).status_code,403)

    def test_monthly_income_creation_retries_do_not_duplicate_rules_or_materialize_money(self):
        payload={"description":"Sentetik aylık gelir","amount":"1000","expected_on":"2024-01-29","frequency":"monthly","schedule_mode":"anchored","request_key":"monthly-income-0001"}
        first=finance.save_monthly_income(self.user,payload)
        self.assertEqual(finance.save_monthly_income(self.user,payload),first)
        result=finance.overview(self.user,"2024-01-01","2024-02-29")
        self.assertEqual([row["date"] for row in result["events"]],["2024-01-29","2024-02-29"])
        with core.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM income_rules").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM income_records").fetchone()[0],0)

    def test_received_virtual_income_stale_amount_is_rejected_before_any_insert(self):
        rule=core.save_income_rule(self.user,{"description":"Sentetik gelir","amount":"1000","frequency":"monthly","start_on":"2024-01-31","schedule_mode":"last_day"})
        with self.assertRaises(finance.Conflict):
            finance.receive_income(self.user,{"rule_id":rule,"scheduled_on":"2024-01-31","received_on":"2024-01-31","expected_amount_minor":"999"})
        with core.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM income_records").fetchone()[0],0)
