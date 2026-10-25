## Purpose
Build a multi-user expense tracking and budgeting app inspired by the user's Excel workbook. Priorities: correct expense records, useful budget analysis, privacy, user-level data isolation, and ease of use.

## Persistent rules
- Inspect relevant code and architecture before each task; do not change the stack without justification.
- Never modify `17 Ekim Güncel EXCEL.xlsx`.
- Focus on Harcama Takibi (Expense Tracking), Aylık Bütçe (Monthly Budget), Ayarlar (Settings), and Ana Sayfa (Dashboard).
- Keep Pluxee, personal assets/investments, IPO, and interest modules out of the MVP.
- Never hardcode personal workbook amounts, dates, or budgets as defaults for all users.
- Enforce per-user data isolation in the data layer; hiding records in the UI is not enough.
- If backend and server-side authorization are absent, do not describe the app as a secure production multi-user product.
- Test expense create/edit/delete, categories, budget totals, and dashboard calculations.
- Separate planned and completed recurring expenses.
- Use decimal arithmetic or integer minor units for money; avoid floating-point totals.
- Test monthly date boundaries.
- Handle duplicate records, invalid rows, date formats, and decimal separators in CSV/Excel import.
- Dashboard and reports must use consistent business rules.
- The initial user interface language must be Turkish, with natural Turkish user-facing copy.
- Keep code identifiers and developer-facing documentation in English.
- Centralize user-facing strings so an English locale can be added later without rewriting components. Do not add a nonfunctional language selector or an unnecessarily complex i18n system.
- Do not send user data to external services without a clear need and authorization. Never commit secrets or log sensitive information.
- Avoid unnecessary dependencies, over-abstraction, and scope expansion.
- Run tests for real; do not claim unrun tests passed.
- Keep the README up to date with setup, run, test, and data-management instructions.

## Priorities
- P0: privacy, user isolation, data integrity, and preventing data loss.
- P1: expense entry, budgets, and dashboard correctness.
- P2: recurring expenses, reports, import/export.
- P3: visual polish and convenience improvements.

## Definition of done
Run relevant tests and the build when possible. Report completed work, test results, known limitations, and the next step. Document material assumptions and protect user data.
