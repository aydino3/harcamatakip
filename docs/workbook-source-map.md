# Workbook source map

Reference: `17 Ekim Güncel EXCEL.xlsx` (read-only). The app does not import its records or modify the workbook.

| Sheet | What it contains | App use |
| --- | --- | --- |
| `Harcama Takibi` | Monthly rows for main card, second card, and cash amounts, with notes. The provided month rows are blank. | Reference for expense records, dates, descriptions, payment methods, and the need to avoid treating a card bill as new consumption. The MVP captures individual transactions instead of a single month-end card total. |
| `Aylık Bütçe` | Monthly spending limits, actual expense groupings, payment obligations, Pluxee spending, cash flow, and min/target/max daily-life budgets. | Reference for monthly and category budget controls and spent/remaining metrics. We keep ordinary spending budgets separate from debt repayment and account balances. |
| `Ayarlar` | Personal salary, Pluxee, cash, gold, reserve, model date, and other scenario inputs. | Only the idea of editable user settings is relevant. Personal values are not seeded into the app. |
| `Ana Sayfa` | A personalized current-state summary and upcoming payment list. | Reference for a readable dashboard with month context, totals, recent activity, and an empty state. |
| `Kullanım Rehberi` | How to operate the workbook, including when to enter completed events. | Helps distinguish actual records from estimates and planned events. |
| `Ödeme Planı`, `Ödeme Takvimi` | Scheduled obligations and future/past cash movements. | Potential future inspiration for recurring costs. Planned future items must remain separate from completed expenses; recurring expenses are not in this phase. |
| `Varlıklar`, `Halka Arz`, `Faiz`, `Pluxee` | Personal assets, investments, interest, and a branded benefit balance. | Explicitly out of scope for this generic expense-tracking MVP. |

## Calculation decisions and ambiguities

- The workbook describes `Aylık Bütçe` min/target/max as everyday spending, separate from debt payments. The app's current dashboard totals recorded purchases, not repayments or cash-account movements.
- Card statement totals and individual purchases can represent the same underlying spending. The app records purchase transactions once; later imports need a preview and deduplication before they can be trusted.
- `Harcama Takibi` contains blank month-level entries in the provided file, so it offers no populated expense rows for validating totals or categories. Workbook amounts are not treated as defaults or acceptance ground truth.
- Planned payment dates, card debt, cash flows, investment proceeds, interest, and Pluxee balances do not belong in the MVP's completed-spending totals.
- Workbook examples are highly personal and contain dated assumptions and updated notes. Those values are not copied to user accounts.

## Phase 1 scope

Accounts, private expenses, user-specific categories and payment methods, monthly overall/category budgets, and a dashboard with month totals, remaining budget, category totals, recent activity, and prior-month comparison. Recurring schedules, reports, imports/exports, payment-plan workflows, and personal finance modules remain later or out of scope.
