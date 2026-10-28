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
        for change in ({'start_on':'2024-02-06'},):
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
        base={'description':'Alışveriş','amount':'100','entry_kind':'expense','card_ref':card,'purchased_on':'2024-01-20'}
        first=finance.save_payment_plan(self.user,{**base,'request_key':'due-change-key-0001'})['id']
        second=finance.save_payment_plan(self.user,{**base,'request_key':'due-change-key-0002'})['id']
        finance.record_payment(self.user,first,{'scheduled_on':'2024-02-05','paid_on':'2024-02-05','amount':'100','request_key':'due-paid-key-0001'})
        old=profile_settings.get_profile(self.user)['cards'][0]
        profile_settings.save_card(self.user,{**old,'payment_due_day':10})
        rules={r['id']:r for r in finance.overview(self.user,'2024-01-01','2024-03-31')['rules']}
        self.assertEqual(rules[first]['start_on'],'2024-02-05')
        self.assertEqual(rules[second]['display_start_on'],'2024-02-10')
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
        dataset['recurring']=[{'id':5,'entry_kind':'regular','amount_minor':50000,'status':'active','frequency':'monthly','start_on':'2024-01-09','end_on':'2024-01-09'}]
        budget=finance.extra_budget(dataset,view,'2024-01-01','2024-01-31')
        self.assertEqual(budget['allowance_minor'],'20000')
        self.assertEqual(budget['remaining_minor'],'19899')
        self.assertEqual(budget['excess_minor'],'0')

    def test_monthly_total_minus_card_fixed_payments_isolated_and_recalculates(self):
        card=self.card(payment_due_on='2024-01-09')
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
        self.assertEqual(view['totals']['payments_planned_minor'],'35001')
        self.assertEqual(sum(int(e['amount_minor']) for e in view['events'] if e['kind']=='payment'),35001)
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

    def test_authoritative_card_total_includes_installments_and_partial_payments_once(self):
        from unittest.mock import patch
        from datetime import date
        card=self.card(payment_due_on='2024-01-20')
        self.plan(entry_kind='installment',card_ref=card,amount='100',start_on='2024-01-20',end_on='2024-03-20')
        finance.save_payment_plan(self.user,{'description':'Market','amount':'20','entry_kind':'expense','card_ref':card,'purchased_on':'2024-01-01','request_key':'monthly-core-extra-01'})
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-01','amount':'300','version':0})
        before=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(before['totals']['payments_planned_minor'],'30000')
        self.assertEqual(before['rules'][0]['scheduled_total_minor'],'10000')
        self.assertEqual(len([e for e in before['events'] if e['kind']=='payment']),1)
        finance.save_opening(self.user,{'amount':'1000','effective_on':'2024-01-01'})
        payload={'card_ref':card,'month':'2024-01','amount':'75','paid_on':'2024-01-15','request_key':'monthly-card-paid-01'}
        self.assertTrue(finance.record_card_monthly_payment(self.user,payload)['created'])
        self.assertFalse(finance.record_card_monthly_payment(self.user,payload)['created'])
        after=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(after['totals']['payments_paid_minor'],'7500')
        self.assertEqual(after['totals']['payments_planned_minor'],'22500')
        self.assertEqual(after['net_minor'],'-30000')
        self.assertEqual(after['cash_position']['balance_minor'],'92500')
        with self.assertRaises(ValueError):finance.record_card_monthly_payment(self.user,{**payload,'amount':'226','request_key':'monthly-card-paid-02'})
        with self.assertRaises(ValueError):finance.record_card_monthly_payment(self.other,payload)
        with self.assertRaises(ValueError):finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-01','amount':'74','version':1})
        with self.assertRaises(finance.Conflict):finance.record_card_monthly_payment(self.user,{**payload,'amount':'70'})

    def test_authoritative_card_total_preserves_existing_payment_and_month_boundary(self):
        card=self.card(payment_due_on='2024-02-29')
        rule=self.plan(entry_kind='installment',card_ref=card,amount='100',start_on='2024-02-29',end_on='2024-03-29')
        finance.record_payment(self.user,rule,{'scheduled_on':'2024-02-29','paid_on':'2024-01-31','amount':'25','request_key':'monthly-old-paid-01'})
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-02','amount':'300','version':0})
        feb=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(feb['totals']['payments_planned_minor'],'27500')
        self.assertEqual(feb['events'][0]['date'],'2024-02-29')
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['totals']['payments_paid_minor'],'2500')
        self.assertEqual(finance.overview(self.user,'2024-03-01','2024-03-31')['totals']['payments_planned_minor'],'10000')

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

    def test_exact_card_due_date_is_never_rebased_onto_the_selected_month(self):
        card=self.card(payment_due_on='2026-11-09')
        plan=self.plan(entry_kind='installment',card_ref=card,start_on='2026-11-09',end_on='2026-12-09')
        with self.assertRaises(finance.Conflict):
            finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2026-10','due_on':'2026-11-09','amount':'300','version':0})
        self.assertEqual(finance.read_data(self.user)['card_monthly_totals'],[])
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2026-11','due_on':'2026-11-09','amount':'300','version':0})
        november=finance.overview(self.user,'2026-11-01','2026-11-30')
        row=november['card_monthly_debt']['cards'][0]
        self.assertEqual((row['due_on'],row['installment_minor'],row['extra_minor']),('2026-11-09','10000','20000'))
        self.assertEqual(finance.overview(self.user,'2026-10-01','2026-10-31')['totals']['payments_planned_minor'],'0')
        import profile_settings
        original=profile_settings.get_profile(self.user)['cards'][0]
        profile_settings.save_card(self.user,{**original,'payment_due_on':'2026-12-09'})
        row=finance.overview(self.user,'2026-11-01','2026-11-30')['card_monthly_debt']['cards'][0]
        self.assertEqual(row['due_on'],'2026-11-09');self.assertEqual(row['due_date_explicit'],1)

    def test_correcting_legacy_wrong_invoice_month_preserves_actual_payment_day(self):
        card=self.card(payment_due_on='2026-11-09')
        self.plan(entry_kind='installment',card_ref=card,start_on='2026-11-09',end_on='2026-12-09')
        finance.save_opening(self.user,{'amount':'1000','effective_on':'2024-01-01'})
        with core.connect() as db:
            db.execute('INSERT INTO card_monthly_totals(user_id,card_ref,month,amount_minor,version,due_on) VALUES(?,?,?,?,1,?)',(self.user,card,'2026-10',30000,'2026-10-09'))
            db.execute('INSERT INTO card_monthly_payments(user_id,card_ref,month,amount_minor,paid_on,idempotency_key) VALUES(?,?,?,?,?,?)',(self.user,card,'2026-10',5000,'2024-01-02','period-correction-paid-01'))
        cash=finance.overview(self.user,'2026-10-01','2026-10-31')['cash_position']['balance_minor']
        payload={'card_ref':card,'source_month':'2026-10','month':'2026-11','due_on':'2026-11-09','amount':'300','version':1}
        self.assertEqual(finance.save_card_monthly_total(self.user,payload),{'version':2})
        self.assertEqual(finance.save_card_monthly_total(self.user,payload),{'version':2})
        data=finance.read_data(self.user)
        self.assertEqual([(row['month'],row['due_on']) for row in data['card_monthly_totals']],[('2026-11','2026-11-09')])
        self.assertEqual([(row['month'],row['paid_on']) for row in data['card_monthly_payments']],[('2026-11','2024-01-02')])
        self.assertEqual(finance.overview(self.user,'2026-10-01','2026-10-31')['cash_position']['balance_minor'],cash)
        row=finance.overview(self.user,'2026-11-01','2026-11-30')['card_monthly_debt']['cards'][0]
        self.assertEqual((row['extra_minor'],row['remaining_minor']),('20000','25000'))

    def test_invoice_period_correction_never_overwrites_an_existing_target(self):
        card=self.card(payment_due_on='2026-11-09')
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2026-11','amount':'350','version':0})
        with core.connect() as db:db.execute('INSERT INTO card_monthly_totals(user_id,card_ref,month,amount_minor,version,due_on) VALUES(?,?,?,?,1,?)',(self.user,card,'2026-10',30000,'2026-10-09'))
        with self.assertRaises(finance.Conflict):finance.save_card_monthly_total(self.user,{'card_ref':card,'source_month':'2026-10','month':'2026-11','due_on':'2026-11-09','amount':'300','version':1})
        self.assertEqual(sorted(row['amount_minor'] for row in finance.read_data(self.user)['card_monthly_totals']),[30000,35000])

    def remove(self,kind,record_id):
        data=finance.read_data(self.user)
        return finance.delete_financial_record(self.user,{'record_kind':kind,'id':record_id,'revision':finance.managed_revision(data,kind,record_id),'confirmation':'SİL'})

    def test_income_edit_delete_restore_reconciles_actual_cash_and_keeps_source(self):
        finance.save_opening(self.user,{'amount':'1000','effective_on':'2024-01-01'})
        record=finance.save_income_once(self.user,{'description':'Ek gelir','amount':'100','expected_on':'2024-01-02','request_key':'crud-income-0001'})['id']
        row=finance.overview(self.user,'2024-01-01','2024-01-31')['income_records'][0]
        finance.edit_simple_income(self.user,{'record_kind':'income_record','id':record,'revision':row['revision'],'description':'Düzeltilmiş gelir','amount':'125.25','expected_on':'2024-01-02','status':'received','received_on':'2024-01-03'})
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['cash_position']['balance_minor'],'112525')
        with self.assertRaises(finance.Conflict):finance.edit_simple_income(self.user,{'record_kind':'income_record','id':record,'revision':row['revision'],'description':'Eski ekran','amount':'1','expected_on':'2024-01-02'})
        removed=self.remove('income_record',record)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['cash_position']['balance_minor'],'100000')
        with core.connect() as db:self.assertEqual(db.execute('SELECT amount_minor FROM income_records WHERE id=?',(record,)).fetchone()[0],12525)
        with self.assertRaises(finance.Conflict):finance.restore_financial_record(self.other,removed)
        finance.restore_financial_record(self.user,removed)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['cash_position']['balance_minor'],'112525')
        self.remove('income_record',record)
        with self.assertRaises(finance.Conflict):finance.restore_financial_record(self.user,removed)

    def test_deleted_regular_income_receipt_does_not_regenerate_virtual_income(self):
        rule=finance.save_monthly_income(self.user,{'description':'Maaş','amount':'100','expected_on':'2024-01-31','frequency':'monthly','request_key':'crud-rule-000001'})['id']
        receipt=finance.receive_income(self.user,{'rule_id':rule,'scheduled_on':'2024-01-31','received_on':'2024-01-31'})['id']
        removed=self.remove('income_record',receipt)
        january=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(january['net_minor'],'0');self.assertEqual(january['events'],[])
        self.assertEqual(finance.overview(self.user,'2024-02-01','2024-02-29')['net_minor'],'10000')
        with self.assertRaises(ValueError):finance.receive_income(self.user,{'rule_id':rule,'scheduled_on':'2024-01-31','received_on':'2024-01-31'})
        finance.restore_financial_record(self.user,removed)
        whole=self.remove('income_rule',rule)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-03-31')['net_minor'],'0')
        finance.restore_financial_record(self.user,whole)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-03-31')['net_minor'],'30000')

    def test_income_rule_edit_preserves_receipts_and_changes_future_amount(self):
        from unittest.mock import patch
        rule=finance.save_monthly_income(self.user,{'description':'Maaş','amount':'100','expected_on':'2024-01-01','frequency':'monthly','request_key':'crud-rule-edit-001'})['id']
        finance.receive_income(self.user,{'rule_id':rule,'scheduled_on':'2024-01-01','received_on':'2024-01-01'})
        with core.connect() as db:db.execute("INSERT INTO income_records(user_id,rule_id,income_type,description,amount_minor,expected_on,status) VALUES(?,?,'additional','Maaş',10000,'2024-02-01','expected')",(self.user,rule))
        row=finance.overview(self.user,'2024-01-01','2024-03-31')['income_rules'][0]
        payload={'record_kind':'income_rule','id':rule,'revision':row['revision'],'description':'Güncel maaş','amount':'200','expected_on':'2024-01-01','frequency':'monthly','schedule_mode':'anchored'}
        with patch.object(finance,'today',return_value=date(2024,1,15)):finance.edit_simple_income(self.user,payload)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['net_minor'],'10000')
        self.assertEqual(finance.overview(self.user,'2024-02-01','2024-02-29')['net_minor'],'20000')
        payload['revision']=finance.overview(self.user,'2024-01-01','2024-03-31')['income_rules'][0]['revision']
        with self.assertRaises(ValueError):finance.edit_simple_income(self.user,{**payload,'frequency':'weekly'})
        with self.assertRaises(ValueError):finance.edit_simple_income(self.other,payload)

    def test_regular_income_without_receipts_can_edit_date_frequency_and_last_day(self):
        rule=finance.save_monthly_income(self.user,{'description':'Plan','amount':'100','start_month':'2024-01','schedule_mode':'last_day','frequency':'monthly','request_key':'crud-flex-rule-01'})['id']
        with core.connect() as db:db.execute("INSERT INTO income_records(user_id,rule_id,income_type,description,amount_minor,expected_on,status) VALUES(?,?,'additional','Plan',10000,'2024-01-31','expected')",(self.user,rule))
        row=finance.overview(self.user,'2024-01-01','2024-03-31')['income_rules'][0]
        finance.edit_simple_income(self.user,{'record_kind':'income_rule','id':rule,'revision':row['revision'],'description':'Haftalık plan','amount':'75','frequency':'weekly','schedule_mode':'anchored','expected_on':'2024-01-02'})
        view=finance.overview(self.user,'2024-01-01','2024-01-23')
        self.assertEqual([event['date'] for event in view['events']],['2024-01-02','2024-01-09','2024-01-16','2024-01-23'])
        self.assertEqual(view['net_minor'],'30000')
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['net_minor'],'37500')
        with self.assertRaisesRegex(ValueError,'Bitiş yılını'):
            self.plan(start_on='2026-11-06',end_on='2026-02-04',request_key='crud-wrong-year-01')

    def test_payment_delete_restore_removes_only_owned_plan_cash_effects(self):
        finance.save_opening(self.user,{'amount':'1000','effective_on':'2024-01-01'})
        card=self.card(payment_due_on='2024-01-09')
        plan=self.plan(entry_kind='installment',card_ref=card)
        finance.record_payment(self.user,plan,{'scheduled_on':'2024-01-09','paid_on':'2024-01-09','amount':'40','request_key':'crud-partial-plan-01'})
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-01','amount':'300','version':0})
        finance.record_card_monthly_payment(self.user,{'card_ref':card,'month':'2024-01','amount':'30','paid_on':'2024-01-09','request_key':'crud-invoice-paid-01'})
        before=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(before['cash_position']['balance_minor'],'93000')
        removed=self.remove('payment_plan',plan)
        after=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(after['cash_position']['balance_minor'],'97000')
        self.assertEqual(after['net_minor'],'-30000')
        self.assertEqual(after['card_monthly_debt']['cards'][0]['extra_minor'],'30000')
        with self.assertRaises(ValueError):finance.record_payment(self.user,plan,{'scheduled_on':'2024-01-09','paid_on':'2024-01-09','amount':'1','request_key':'crud-deleted-paid-01'})
        finance.restore_financial_record(self.user,removed)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['cash_position']['balance_minor'],'93000')

    def test_crud_http_csrf_ownership_and_delete_confirmation(self):
        client=webapp.create_app({'TESTING':True,'PRIVATE_BETA':False}).test_client()
        login=client.post('/api/login',json={'email':'cashflow-fixture@example.invalid','password':'synthetic-only-password'});headers={'X-CSRF-Token':login.json['csrf']}
        record=finance.save_income_once(self.user,{'description':'API gelir','amount':'10','expected_on':'2024-01-02','request_key':'crud-http-income-01'})['id']
        data=finance.read_data(self.user);payload={'record_kind':'income_record','id':record,'revision':finance.managed_revision(data,'income_record',record),'confirmation':'SİL'}
        self.assertEqual(client.post('/api/simple-records/delete',json=payload).status_code,403)
        self.assertEqual(client.post('/api/simple-records/delete',json={**payload,'confirmation':''},headers=headers).status_code,400)
        self.assertEqual(client.post('/api/simple-records/delete',json={**payload,'id':99999999},headers=headers).status_code,400)
        removed=client.post('/api/simple-records/delete',json=payload,headers=headers)
        self.assertEqual(removed.status_code,200)
        self.assertEqual(client.post('/api/simple-records/restore',json=removed.json,headers=headers).status_code,200)

    def statement_purchase(self,amount='120',spent='2024-01-05',installments=1):
        card=core.save_credit_card(self.user,{'bank_name':'Sentetik','display_name':'Ekstre kartı','closing_day':22,'due_day':3})
        expense=core.save_expense(self.user,{'amount':amount,'spent_on':spent,'description':'Ekstre alışverişi','card_id':card,'installments_count':installments,'first_installment_on':spent})
        return card,expense

    def test_total_statement_includes_linked_installment_without_double_counting(self):
        card,expense=self.statement_purchase(amount='300')
        ref='legacy:'+str(card)
        self.plan(entry_kind='installment',card_ref=ref,amount='100',start_on='2024-02-03',end_on='2024-03-03')
        statement=finance.overview(self.user,'2024-02-01','2024-02-29')['statements'][0]
        finance.record_statement_payment(self.user,{'statement_id':statement['id'],'amount':'30','paid_on':'2024-02-01','request_key':'total-legacy-paid-01'})
        finance.save_card_monthly_total(self.user,{'card_ref':ref,'month':'2024-02','amount':'300','version':0})
        view=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(view['totals']['payments_paid_minor'],'3000')
        self.assertEqual(view['totals']['payments_planned_minor'],'27000')
        self.assertEqual(view['net_minor'],'-30000')
        self.assertTrue(view['statements'][0]['covered_by_monthly_total'])
        self.assertEqual(view['card_monthly_debt']['cards'][0]['extra_minor'],'20000')
        finance.record_card_monthly_payment(self.user,{'card_ref':ref,'month':'2024-02','amount':'70','paid_on':'2024-02-02','request_key':'total-monthly-paid-01'})
        self.assertEqual(finance.overview(self.user,'2024-02-01','2024-02-29')['net_minor'],'-30000')
        self.assertEqual(len(finance.read_data(self.user)['expenses']),1)

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

    def test_card_day_is_single_source_in_plan_total_forecast_and_profile(self):
        import profile_settings
        card=self.card(payment_due_day=9)
        plan=self.plan(entry_kind='installment',card_ref=card,start_on='2024-01-15',end_on='2024-03-15')
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-02','amount':'300','due_day_offset':1,'version':0})
        view=finance.overview(self.user,'2024-02-01','2024-02-29')
        row=view['card_monthly_debt']['cards'][0]
        self.assertEqual((row['due_on'],row['extra_minor']),('2024-02-10','20000'))
        self.assertEqual([(e['date'],e['amount_minor']) for e in view['events']],[('2024-02-10','30000')])
        finance.record_payment(self.user,plan,{'scheduled_on':'2024-01-15','paid_on':'2024-01-10','amount':'100','request_key':'day-history-key-0001'})
        original=profile_settings.get_profile(self.user)['cards'][0]
        profile_settings.save_card(self.user,{**original,'payment_due_day':12})
        view=finance.overview(self.user,'2024-01-01','2024-03-31')
        self.assertEqual(next(e['date'] for e in view['events'] if e['status']=='paid'),'2024-01-10')
        feb=finance.overview(self.user,'2024-02-01','2024-02-29')
        self.assertEqual(feb['events'][0]['date'],'2024-02-13')
        self.assertEqual(feb['card_monthly_debt']['cards'][0]['due_on'],'2024-02-13')
        self.assertEqual(finance.cash_position(finance.read_data(self.user))['configured'],False)

    def test_card_due_short_month_offsets_and_period_identity(self):
        import card_schedule
        self.assertEqual(card_schedule.due_on({'payment_due_day':31},'2024-02'),'2024-02-29')
        self.assertEqual(card_schedule.due_on({'payment_due_day':31},'2025-02'),'2025-02-28')
        self.assertEqual(card_schedule.due_on({'payment_due_day':1},'2024-01',-5),'2023-12-27')
        self.assertEqual(card_schedule.due_on({'payment_due_day':31},'2024-12',5),'2025-01-05')
        card=self.card(payment_due_day=1)
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-02','amount':'300','due_day_offset':-5,'version':0})
        view=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(view['events'][0]['date'],'2024-01-27')
        self.assertEqual(view['events'][0]['monthly_month'],'2024-02')
        for bad in (-6,6,1.5,True,'1.5','no'):
            with self.assertRaises(ValueError):finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-03','amount':'300','due_day_offset':bad,'version':0})

    def test_opening_edit_is_revisioned_and_never_creates_income(self):
        finance.save_opening(self.user,{'amount':'100','effective_on':'2024-01-01'})
        revision=finance.cash_position(finance.read_data(self.user))['revision']
        finance.save_opening(self.user,{'amount':'200','effective_on':'2024-01-02','revision':revision})
        with self.assertRaises(finance.Conflict):finance.save_opening(self.user,{'amount':'300','effective_on':'2024-01-01','revision':revision})
        view=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(view['cash_position']['balance_minor'],'20000')
        self.assertEqual(view['totals']['income_received_minor'],'0')

    def test_bank_cash_reclassification_preserves_balance_and_is_recoverable(self):
        finance.save_opening(self.user,{'amount':'0','effective_on':'2024-01-02'})
        record=finance.save_income_once(self.user,{'description':'Sentetik banka nakdi','amount':'123.45','expected_on':'2024-01-02','status':'received','request_key':'cash-reclassify-0001'})['id']
        before=finance.overview(self.user,'2024-01-01','2024-01-31')
        row=next(r for r in before['income_records'] if r['id']==record)
        payload={'id':record,'revision':row['revision']}
        with self.assertRaises(ValueError):finance.transfer_income_to_opening(self.other,payload)
        change=finance.transfer_income_to_opening(self.user,payload)
        after=finance.overview(self.user,'2024-01-01','2024-01-31')
        self.assertEqual(before['cash_position']['balance_minor'],after['cash_position']['balance_minor'])
        self.assertEqual(after['cash_position']['opening_balance_minor'],'12345')
        self.assertEqual(after['totals']['income_received_minor'],'0')
        finance.restore_financial_record(self.user,change)
        self.assertEqual(finance.overview(self.user,'2024-01-01','2024-01-31')['totals']['income_received_minor'],'12345')

    def test_legacy_profile_due_day_overrides_old_core_card_default(self):
        import profile_settings
        card_id=core.save_credit_card(self.user,{'bank_name':'Eski banka','display_name':'Eski kart','closing_day':22,'due_day':3})
        with core.connect() as db:
            db.execute('INSERT INTO profile_cards(user_id,legacy_card_id,bank_name,display_name,payment_due_on) VALUES(?,?,?,?,?)',(self.user,card_id,'Eski banka','Eski kart','2026-11-09'))
        card=profile_settings.get_profile(self.user)['cards'][0]
        self.assertEqual(card['payment_due_day'],9)
        self.assertEqual(finance.configured_card_due(finance.read_data(self.user),f'legacy:{card_id}','2024-02'),'2024-02-09')

    def test_existing_invoice_date_becomes_bounded_offset_without_rewriting_payments(self):
        card=self.card(payment_due_day=26)
        with core.connect() as db:
            db.execute('INSERT INTO card_monthly_totals(user_id,card_ref,month,amount_minor,due_on) VALUES(?,?,?,?,?)',(self.user,card,'2026-11',30000,'2026-11-27'))
        row=finance.overview(self.user,'2026-11-01','2026-11-30')['card_monthly_debt']['cards'][0]
        self.assertEqual((row['due_on'],row['due_day_offset']),('2026-11-27',1))
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2026-11','amount':'300','due_day_offset':0,'version':1})
        self.assertEqual(finance.overview(self.user,'2026-11-01','2026-11-30')['card_monthly_debt']['cards'][0]['due_on'],'2026-11-26')

    def test_opening_day_correction_includes_existing_payment_without_reposting(self):
        card=self.card(payment_due_day=26)
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-10','amount':'5379.01','version':0})
        finance.record_card_monthly_payment(self.user,{'card_ref':card,'month':'2024-10','amount':'5379.01','paid_on':'2024-10-26','request_key':'cash-boundary-payment01'})
        finance.save_opening(self.user,{'amount':'30620','effective_on':'2024-10-27'})
        before=finance.cash_position(finance.read_data(self.user),date(2024,10,27))
        self.assertEqual(before['balance_minor'],'3062000')
        finance.save_opening(self.user,{'amount':'30620','effective_on':'2024-10-26','revision':before['revision']})
        for _ in range(2):self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,10,27))['balance_minor'],'2524099')
        with core.connect() as db:
            rows=db.execute('SELECT amount_minor,paid_on FROM card_monthly_payments WHERE user_id=?',(self.user,)).fetchall()
            self.assertEqual([(r[0],r[1]) for r in rows],[(537901,'2024-10-26')])

    def test_scheduled_once_income_credits_on_date_and_confirmation_does_not_duplicate(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':True})
        record=finance.save_income_once(self.user,{'description':'Tarihli gelir','amount':'100','expected_on':'2024-01-03','status':'expected','request_key':'auto-income-once-0001'})['id']
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,2))['balance_minor'],'50000')
        for when in (date(2024,1,3),date(2024,1,4)):
            position=finance.cash_position(finance.read_data(self.user),when)
            self.assertEqual((position['balance_minor'],position['automatic_income_minor']),('60000','10000'))
        finance.receive_income(self.user,{'record_id':record,'received_on':'2024-01-03','expected_amount_minor':'10000'})
        finance.receive_income(self.user,{'record_id':record,'received_on':'2024-01-03','expected_amount_minor':'10000'})
        position=finance.cash_position(finance.read_data(self.user),date(2024,1,4))
        self.assertEqual((position['balance_minor'],position['automatic_income_minor']),('60000','0'))

    def test_scheduled_income_monthly_weekly_and_projection_share_recognition(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1})
        monthly=finance.save_monthly_income(self.user,{'description':'Aylık tarihli','amount':'100','frequency':'monthly','schedule_mode':'last_day','start_month':'2024-01','request_key':'auto-monthly-key-0001'})['id']
        finance.save_monthly_income(self.user,{'description':'Haftalık tarihli','amount':'10','frequency':'weekly','expected_on':'2024-01-02','request_key':'auto-weekly-key-0001'})
        data=finance.read_data(self.user)
        self.assertEqual(finance.cash_position(data,date(2024,1,31))['balance_minor'],'65000')
        self.assertEqual(finance.cash_position(data,date(2024,2,29))['balance_minor'],'79000')
        self.assertEqual(finance.projected_balance(data,'2024-02-29',date(2024,1,31)),79000)
        finance.receive_income(self.user,{'rule_id':monthly,'scheduled_on':'2024-01-31','received_on':'2024-01-31','expected_amount_minor':'10000'})
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,2,29))['balance_minor'],'79000')

    def test_actual_receipt_date_overrides_scheduled_date_without_phantom_cash(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1})
        record=finance.save_income_once(self.user,{'description':'Geciken gerçek gelir','amount':'100','expected_on':'2024-01-03','status':'expected','request_key':'auto-late-key-000001'})['id']
        finance.receive_income(self.user,{'record_id':record,'received_on':'2024-01-05','expected_amount_minor':'10000'})
        data=finance.read_data(self.user)
        self.assertEqual(finance.cash_position(data,date(2024,1,4))['balance_minor'],'50000')
        self.assertEqual(finance.cash_position(data,date(2024,1,5))['balance_minor'],'60000')

    def test_deleting_scheduled_receipt_suppresses_virtual_regeneration(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1})
        rule=finance.save_monthly_income(self.user,{'description':'Tekrarlı tarihli','amount':'100','frequency':'monthly','expected_on':'2024-01-02','request_key':'auto-delete-rule-001'})['id']
        receipt=finance.receive_income(self.user,{'rule_id':rule,'scheduled_on':'2024-01-02','received_on':'2024-01-02','expected_amount_minor':'10000'})['id']
        data=finance.read_data(self.user)
        removed=finance.delete_financial_record(self.user,{'record_kind':'income_record','id':receipt,'revision':finance.managed_revision(data,'income_record',receipt),'confirmation':'SİL'})
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,31))['balance_minor'],'50000')
        finance.restore_financial_record(self.user,removed)
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,31))['balance_minor'],'60000')

    def test_paused_materialized_plan_does_not_auto_credit_but_actual_income_stays(self):
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1})
        rule=core.save_income_rule(self.user,{'description':'Duraklatılan','amount':'100','frequency':'monthly','start_on':'2024-01-02','schedule_mode':'anchored'})
        with core.connect() as db:
            record=db.execute("INSERT INTO income_records(user_id,rule_id,income_type,description,amount_minor,expected_on,status) VALUES(?,?,'other','Duraklatılan',10000,'2024-01-02','expected')",(self.user,rule)).lastrowid
        core.set_income_rule_status(self.user,rule,'paused')
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,31))['balance_minor'],'50000')
        with core.connect() as db:db.execute("UPDATE income_records SET status='received',received_on='2024-01-02' WHERE id=? AND user_id=?",(record,self.user))
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,31))['balance_minor'],'60000')

    def test_auto_income_is_opt_in_owned_and_revision_protected(self):
        for user in (self.user,self.other):
            finance.save_opening(user,{'amount':'500','effective_on':'2024-01-01'})
            finance.save_income_once(user,{'description':'Planlı','amount':'100','expected_on':'2024-01-02','status':'expected','request_key':'auto-owned-key-00001'})
        original=finance.cash_position(finance.read_data(self.user),date(2024,1,3))
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1,'revision':original['revision']})
        with self.assertRaises(finance.Conflict):finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':0,'revision':original['revision']})
        self.assertEqual(finance.cash_position(finance.read_data(self.user),date(2024,1,3))['balance_minor'],'60000')
        self.assertEqual(finance.cash_position(finance.read_data(self.other),date(2024,1,3))['balance_minor'],'50000')

    def test_auto_income_before_opening_is_not_added_again(self):
        finance.save_monthly_income(self.user,{'description':'Eski başlangıçlı','amount':'100','frequency':'monthly','expected_on':'2000-01-02','request_key':'auto-long-history-01'})
        finance.save_opening(self.user,{'amount':'500','effective_on':'2024-01-01','auto_income_on_due':1})
        data=finance.read_data(self.user)
        self.assertEqual(finance.cash_position(data,date(2024,1,3))['balance_minor'],'60000')
        self.assertEqual(finance.projected_balance(data,'2024-01-31',date(2024,1,3)),60000)
