# Budget Tracker

A local budget and credit card spending tool. Everything runs on your machine and stores data in a single SQLite file at `data/budget.db` — nothing is sent anywhere.

## Running it

Double-click **`start.bat`**. The first run creates a Python virtual environment and installs dependencies (takes a minute); every run after that starts instantly. Your browser opens automatically to the app. Closing the black console window stops the app.

## Day to day use

### 1. Add an account
On the **Import** tab, click **+ new account**, give it a name (e.g. "Chase Sapphire"), and pick the institution and type. This only needs to be done once per card/account.

### 2. Import a statement
Choose the account, pick the exported file (`.csv`, `.xlsx`, `.ofx`/`.qfx`, or `.pdf`), and click **Preview import**.

- **Chase and Capital One CSV exports** are recognized automatically — no setup needed.
- **Any other CSV/Excel file** (Popular Bank, Synchrony, or anything else) will ask you, once, which column is the date, description, and amount (or separate debit/credit columns). That mapping only needs to be worked out the first time you import that particular export format.
- **OFX/QFX** files parse automatically.
- **PDF statements** are parsed on a best-effort basis by scanning for lines that look like `date, description, amount`. It works well for straightforward statement layouts; if a PDF doesn't parse cleanly, check whether the bank's website offers a CSV export instead — that's always more reliable.

Every import shows a **preview** — imported count, any duplicates that will be skipped, and a category breakdown — before anything is written. Click **Confirm import** to commit it. Re-importing a file (or an overlapping date range) never creates duplicate transactions.

### 3. Review and fix categories
New transactions are auto-categorized using keyword rules (seeded with common merchants, editable on the **Accounts & Rules** tab). Wherever you see a transaction — Month view, Transactions tab — you can change its category from the dropdown. You'll be asked whether to apply that correction to similar transactions too; if you say yes, a new rule is saved so future imports from that merchant categorize correctly automatically.

### 4. Look at spending
- **Month** — pick a year/month to see total spend, a category breakdown chart, and the transaction list for that month.
- **Year** — pick a year to see a month-by-month stacked chart by category and yearly totals.
- **Transactions** — search/filter across every account and date.

### 5. Check imports against your statements
The **Reconcile** tab compares what was imported for each statement against the balances printed on that statement, so you can tell whether anything is missing or counted twice. Nothing runs automatically — click **Run reconciliation** whenever you want a check.

**Getting statements in.** Each statement needs its closing date and new balance (and, optionally, its start date and previous balance):
- **Importing a PDF statement** on the Import tab reads these from the PDF automatically and saves them when you confirm.
- **PDFs you've already imported:** use **Read PDF(s)** on the Reconcile tab (pick the account, select one or several PDFs). It reads the balances and matches the PDF's transaction lines to what's already in the database — it doesn't import anything, and it tells you if any line from the PDF isn't in the database.
- **CSV/Excel exports and anything else:** type the statement in by hand under *Enter a statement by hand*. Enter balances exactly as printed (for a credit card, the amount you owe). Leave the start date and previous balance blank and they continue from the previous statement on file for that account, so after the first one you only type a closing date and a new balance.

**Reading the results.** For each statement you'll see the statement's own change in balance, the change implied by the imported transactions, and the difference. Anything that doesn't match is highlighted, and **Details** explains the likely cause (a possible duplicate, a missing transaction, a payment stored with the wrong sign) and lists exactly which transactions were counted and why.

Which transactions count toward a statement: the ones its PDF listed, plus any other transaction from that account dated inside the statement period. A card statement assigns purchases to a cycle by posting date, which can be a few days after the purchase date the PDF shows, so PDF-listed transactions always count toward their own statement even if their date is just outside the period. A purchase from a CSV import made in the last days of a period may show as a difference until the following statement is read.

### Undoing an import
If an import went in wrong (bad column mapping, wrong file, etc.), go to **Accounts & Rules → Import history** and click **Undo** next to that import — it removes exactly those transactions.

## Sharing with another device on your home network

The app runs on one PC (the "host"); anyone else on your home network uses it through a web browser,
so there's one shared database and nothing to install on their device.

One-time setup on the host PC:

1. Double-click `set_password.bat` and choose a shared password (saved only as a salted hash in `data/access.json`).
2. Double-click `allow_network_access.bat` and approve the Windows prompt. It adds a firewall rule for
   *Private* networks and your local subnet only. (Windows' network profile for your Wi-Fi must be "Private".)
3. Restart the app with `start.bat`. The window now prints the address to use, e.g. `http://YOUR-PC:8420`.

Then on the other device, open that address in a browser and sign in: any username, the shared password.
The host PC itself is never asked for the password. The host PC has to be on with `start.bat` running.

Without `data/access.json` the app only listens on this PC. To turn network access off again, delete that file
and restart. The connection is plain HTTP, which is fine on your own home Wi-Fi but don't expose it to the internet.

## Notes

- Amounts are stored as: negative = money out (a purchase), positive = money in (a payment, refund, or deposit).
- The older *Import history & reconciliation* table on the Accounts & Rules tab still lets you type a total against a single import file. The Reconcile tab supersedes it for statement checks, since an import file often doesn't line up with a statement period.
- The Popular Bank and Synchrony CSV presets aren't built in yet since their exact export formats weren't available — the column-mapping screen covers them in the meantime. The mapping you choose is remembered per account, so you'll only be asked once per account (unless that institution later changes its export's column headers).
