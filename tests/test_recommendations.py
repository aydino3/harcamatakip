"""Data-driven recommendations: synthetic ledger, read-only and exact arithmetic."""
import copy
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch
import unittest
import app as core
import simple_cashflow as finance
import recommendations
from tests import test_simple_cashflow as fixture
import webapp


class RecommendationsTest(unittest.TestCase):
    setUp=fixture.SimpleCashflowTest.setUp
    plan=fixture.SimpleCashflowTest.plan
    card=fixture.SimpleCashflowTest.card

    def opening(self,amount='100',automatic=0):
        finance.save_opening(self.user,{'amount':amount,'effective_on':'2024-01-01','auto_income_on_due':automatic})

    def income(self,amount='300',on='2024-01-05'):
        return finance.save_income_once(self.user,{'description':'Sentetik gelir','amount':amount,'expected_on':on,'status':'expected','request_key':'advice-income-key-0001'})['id']

    def analyze(self,month='2024-01',on=date(2024,1,1)):
        return recommendations.analyze(finance.read_data(self.user),month,on)

    def codes(self,result):return {row['code'] for row in result['items']}

    def test_empty_account_is_incomplete_not_a_zero_cash_success(self):
        r=self.analyze();self.assertFalse(r['cash']['available']);self.assertIsNone(r['cash']['balance_minor'])
        self.assertEqual(self.codes(r),{'data_quality'});self.assertIsNone(r['spending']['income_minor'])
        self.assertTrue(all(row['income_minor'] is None and row['capacity_minor'] is None for row in r['future_months']))

    def test_midperiod_deficit_is_found_even_when_month_ends_positive(self):
        self.opening();self.income();self.plan(entry_kind='regular',amount='200',start_on='2024-01-03',end_on='2024-01-03')
        r=self.analyze();self.assertEqual(r['cash']['ending_minor'],'20000');self.assertEqual(r['cash']['minimum_minor'],'-10000')
        self.assertEqual(r['cash']['first_gap'],{'date':'2024-01-03','amount_minor':'10000'});self.assertIn('cash_gap',self.codes(r))

    def test_same_day_is_netted_but_dependency_on_future_income_stays_visible(self):
        self.opening();self.income(on='2024-01-03');self.plan(entry_kind='regular',amount='200',start_on='2024-01-03',end_on='2024-01-03')
        r=self.analyze();self.assertNotIn('cash_gap',self.codes(r));self.assertIn('income_dependency',self.codes(r));self.assertEqual(r['cash']['minimum_minor'],'10000')
        self.assertEqual(r['cash']['without_income_first_gap']['amount_minor'],'10000')

    def test_overdue_expected_income_is_not_invented_as_available_today(self):
        self.opening();self.income(amount='500',on='2024-01-03');self.plan(entry_kind='regular',amount='200',start_on='2024-01-12',end_on='2024-01-12')
        r=self.analyze(on=date(2024,1,10));self.assertEqual(r['cash']['expected_income_minor'],'0');self.assertIn('cash_gap',self.codes(r))
        issue=next(row for row in r['issues'] if row['code']=='overdue_income');self.assertEqual((issue['count'],issue['amount_minor']),(1,'50000'))

    def test_settled_card_total_and_installments_do_not_reduce_cash_twice(self):
        self.opening('1000');card=self.card(payment_due_day=6,statement_closing_day=26)
        self.plan(entry_kind='installment',card_ref=card,start_on='2024-01-06',end_on='2024-01-06')
        finance.save_card_monthly_total(self.user,{'card_ref':card,'month':'2024-01','amount':'300','version':0})
        finance.record_card_monthly_payment(self.user,{'card_ref':card,'month':'2024-01','amount':'300','paid_on':'2024-01-05','request_key':'advice-paid-card-001'})
        r=self.analyze(on=date(2024,1,10));self.assertEqual(r['cash']['balance_minor'],'70000');self.assertEqual(r['cash']['pending_payments_minor'],'0')
        self.assertNotIn('cash_gap',self.codes(r))

    def test_auto_income_is_identified_and_not_added_a_second_time(self):
        self.opening(automatic=1);self.income(amount='100',on='2024-01-02')
        r=self.analyze(on=date(2024,1,3));self.assertEqual(r['cash']['balance_minor'],'20000');self.assertEqual(r['cash']['expected_income_minor'],'0')
        self.assertEqual(next(i for i in r['issues'] if i['code']=='automatic_income')['amount_minor'],'10000')
        self.assertEqual(finance.read_data(self.user)['income_records'][0]['status'],'expected')

    def test_fixed_cost_gap_is_not_presented_as_optional_spending_reduction(self):
        self.opening('1000');self.income(amount='100');self.plan(entry_kind='regular',amount='150',start_on='2024-01-15',end_on='2024-01-15')
        r=self.analyze();self.assertIn('fixed_gap',self.codes(r));self.assertNotIn('extra_gap',self.codes(r));self.assertNotIn('spending_capacity',self.codes(r))
        self.assertEqual(next(i for i in r['items'] if i['code']=='fixed_gap')['facts']['amount_minor'],'5000')

    def test_missing_income_is_unknown_not_zero_salary(self):
        self.opening('1000');self.plan(entry_kind='regular',amount='150',start_on='2024-01-15',end_on='2024-01-15')
        r=self.analyze();self.assertIn('income_missing',{i['code'] for i in r['issues']});self.assertNotIn('fixed_gap',self.codes(r))
        self.assertIsNone(r['spending']['income_minor']);self.assertIsNone(r['spending']['capacity_minor'])

    def test_missing_closing_day_and_future_total_do_not_create_a_safe_spending_limit(self):
        self.opening('1000');self.income(amount='1000');self.card(payment_due_day=6)
        r=self.analyze();self.assertIsNone(r['spending']['capacity_minor']);self.assertFalse(r['coverage']['payment_complete'])
        self.assertIn('closing_missing',{i['code'] for i in r['issues']});self.assertNotIn('spending_capacity',self.codes(r))

    def test_unassigned_installment_blocks_cash_recommendations_that_may_double_count(self):
        self.opening();core.save_recurring(self.user,{'description':'Kartı bilinmeyen','amount':'200','frequency':'monthly','start_on':'2024-01-03','end_on':'2024-01-03'})
        r=self.analyze();self.assertTrue(r['cash']['blocked']);self.assertNotIn('cash_gap',self.codes(r));self.assertIn('data_quality',self.codes(r))

    def test_future_income_is_not_copied_without_a_recurring_plan_and_pressure_is_dated(self):
        self.opening('1000');core.save_income_rule(self.user,{'description':'Aylık','amount':'100','frequency':'monthly','start_on':'2024-01-10','schedule_mode':'anchored'})
        self.plan(entry_kind='regular',amount='150',start_on='2024-02-15',end_on='2024-02-15')
        r=self.analyze();p=next(i for i in r['items'] if i['code']=='future_fixed_gap');self.assertEqual(p['facts']['month'],'2024-02');self.assertEqual(p['facts']['amount_minor'],'5000')
        self.assertEqual(r['future_months'][0]['income_minor'],'10000')

    def test_installment_end_is_payment_month_relief_not_cash_added_today(self):
        self.opening('1000');card=self.card(payment_due_day=6,statement_closing_day=26)
        self.plan(entry_kind='installment',card_ref=card,start_on='2024-01-06',end_on='2024-02-06')
        r=self.analyze();end=next(i for i in r['items'] if i['code']=='installment_end')
        self.assertEqual((end['facts']['date'],end['facts']['month'],end['facts']['amount_minor']),('2024-02-06','2024-03','10000'))
        self.assertEqual(r['cash']['balance_minor'],'100000')
        data=finance.read_data(self.user);data['recurring'][0]['status']='paused';self.assertNotIn('installment_end',self.codes(recommendations.analyze(data,'2024-01',date(2024,1,1))))

    def test_trend_requires_three_closed_complete_statement_months(self):
        self.opening('1000');card=self.card(payment_due_day=20,statement_closing_day=10)
        for month,amount in [('2023-11','100'),('2023-12','200'),('2024-01','300')]:finance.save_card_monthly_total(self.user,{'card_ref':card,'month':month,'amount':amount,'version':0})
        data=finance.read_data(self.user);r=recommendations.analyze(data,'2024-01',date(2024,1,31));trend=next(i for i in r['items'] if i['code']=='spending_comparison')
        self.assertEqual(trend['facts']['average_minor'],'15000');self.assertEqual(trend['facts']['amount_minor'],'15000')
        data['card_monthly_totals']=[r for r in data['card_monthly_totals'] if r['month']!='2023-12'];self.assertNotIn('spending_comparison',self.codes(recommendations.analyze(data,'2024-01',date(2024,1,31))))

    def test_bigint_precision_and_pure_analysis_preserve_the_source(self):
        data=finance.read_data(self.user);data['opening']={'opening_balance_minor':900719925474099312,'effective_on':'2024-01-01','auto_income_on_due':0};before=copy.deepcopy(data)
        r=recommendations.analyze(data,'2024-01',date(2024,1,1));self.assertEqual(r['cash']['balance_minor'],'900719925474099312');self.assertEqual(data,before)
        self.assertEqual(r,recommendations.analyze(data,'2024-01',date(2024,1,1)))

    def test_authenticated_endpoint_is_owned_validated_and_database_read_only(self):
        self.opening('1000');client=webapp.create_app({'TESTING':True,'PRIVATE_BETA':False}).test_client()
        self.assertEqual(client.get('/api/recommendations').status_code,401)
        client.post('/api/login',json={'email':'cashflow-fixture@example.invalid','password':'synthetic-only-password'})
        with core.connect() as db:db.execute("INSERT INTO cash_settings(user_id,opening_balance_minor,effective_on) VALUES(?,777777,'2024-01-01')",(self.other,))
        original=core.connect
        @contextmanager
        def readonly(*args,**kwargs):
            with original(*args,**kwargs) as db:
                db.execute('SET TRANSACTION READ ONLY' if hasattr(db,'_conn') else 'PRAGMA query_only=ON')
                yield db
        with patch.object(core,'connect',readonly):
            response=client.get('/api/recommendations?month=2024-01&user_id='+str(self.other))
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['cash']['balance_minor'],'100000');self.assertTrue(response.json['read_only'])
        self.assertEqual(client.get('/api/recommendations?month=2024-13').status_code,400)
        self.assertEqual(client.post('/api/recommendations',json={}).status_code,405)
