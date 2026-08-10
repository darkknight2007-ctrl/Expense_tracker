import sqlite3
import os
import csv
import io
from flask import (
    Flask, render_template, request, redirect,
    url_for, g, flash, Response
)

app = Flask(__name__)
# SQLite database lives in Flask's instance/ folder.
# On Render's free tier the filesystem is ephemeral; seed data re-fills it on restart.
DATABASE = os.path.join(app.instance_path, 'expenses.db')

CATEGORIES = [
    'Food', 'Transport', 'Housing', 'Health',
    'Entertainment', 'Shopping', 'Education', 'Other'
]


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        # Ensure the instance directory exists.
        os.makedirs(app.instance_path, exist_ok=True)
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Create schema and seed sample data when the DB is brand new."""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS expenses (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            amount   REAL    NOT NULL,
            category TEXT    NOT NULL,
            date     TEXT    NOT NULL,
            note     TEXT
        );

        CREATE TABLE IF NOT EXISTS budgets (
            category      TEXT PRIMARY KEY,
            monthly_limit REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_date     ON expenses(date);
        CREATE INDEX IF NOT EXISTS idx_category ON expenses(category);
    """)
    db.commit()
    _seed_db(db)


def _seed_db(db):
    """Insert sample expenses only when the table is completely empty.

    On Render's free tier the filesystem resets on every restart, so this
    ensures the app always has something to display rather than an empty list.
    Real user data entered during a session will be visible until the next
    restart — this is a known limitation of the free tier without a disk.
    """
    count = db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    if count > 0:
        return  # Already has data — don't overwrite.

    import datetime
    today = datetime.date.today()
    # Spread samples across the last two months so charts look interesting.
    def days_ago(n):
        return (today - datetime.timedelta(days=n)).isoformat()

    sample_expenses = [
        (12.50,  'Food',          days_ago(1),  'Lunch at cafe'),
        (45.00,  'Transport',     days_ago(3),  'Monthly bus pass top-up'),
        (8.99,   'Entertainment', days_ago(5),  'Netflix'),
        (120.00, 'Housing',       days_ago(7),  'Electricity bill'),
        (23.40,  'Food',          days_ago(10), 'Weekly groceries'),
        (15.00,  'Health',        days_ago(14), 'Pharmacy'),
        (60.00,  'Shopping',      days_ago(18), 'New headphones'),
        (9.99,   'Education',     days_ago(20), 'Coursera subscription'),
        (34.00,  'Food',          days_ago(25), 'Dinner with friends'),
        (200.00, 'Housing',       days_ago(32), 'Internet bill'),
        (18.75,  'Transport',     days_ago(35), 'Cab to airport'),
        (55.00,  'Health',        days_ago(40), 'Gym membership'),
    ]
    db.executemany(
        "INSERT INTO expenses (amount, category, date, note) VALUES (?, ?, ?, ?)",
        sample_expenses
    )
    # Sample budget limits so the over-budget warning is visible.
    db.executemany(
        "INSERT OR IGNORE INTO budgets (category, monthly_limit) VALUES (?, ?)",
        [('Food', 50.00), ('Housing', 150.00)]
    )
    db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_filter_query(category_filter, month_filter):
    """Return (where_clause, params) tuple for optional filters."""
    conditions, params = [], []
    if category_filter:
        conditions.append("category = ?")
        params.append(category_filter)
    if month_filter:
        conditions.append("strftime('%Y-%m', date) = ?")
        params.append(month_filter)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    category_filter = request.args.get('category', '').strip()
    month_filter    = request.args.get('month', '').strip()

    where, params = build_filter_query(category_filter, month_filter)

    expenses = db.execute(
        f"SELECT * FROM expenses {where} ORDER BY date DESC, id DESC",
        params
    ).fetchall()

    total_row = db.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM expenses {where}",
        params
    ).fetchone()
    total = total_row[0]

    # Budget-limit detection: fetch monthly spend per category for current month
    current_month = month_filter or __import__('datetime').date.today().strftime('%Y-%m')
    budget_rows = db.execute("""
        SELECT b.category, b.monthly_limit,
               COALESCE(SUM(e.amount), 0) AS spent
        FROM budgets b
        LEFT JOIN expenses e
            ON e.category = b.category
           AND strftime('%Y-%m', e.date) = ?
        GROUP BY b.category
    """, [current_month]).fetchall()
    over_budget = {
        r['category']
        for r in budget_rows
        if r['spent'] > r['monthly_limit']
    }

    # Distinct categories and months for filter dropdowns
    all_categories = [r[0] for r in db.execute(
        "SELECT DISTINCT category FROM expenses ORDER BY category"
    ).fetchall()]
    all_months = [r[0] for r in db.execute(
        "SELECT DISTINCT strftime('%Y-%m', date) AS m FROM expenses ORDER BY m DESC"
    ).fetchall()]

    return render_template(
        'index.html',
        expenses=expenses,
        total=total,
        over_budget=over_budget,
        categories=CATEGORIES,
        all_categories=all_categories,
        all_months=all_months,
        category_filter=category_filter,
        month_filter=month_filter,
    )


@app.route('/add', methods=['POST'])
def add_expense():
    amount   = request.form.get('amount', '').strip()
    category = request.form.get('category', '').strip()
    date     = request.form.get('date', '').strip()
    note     = request.form.get('note', '').strip()

    if not amount or not category or not date:
        flash('Amount, category, and date are required.', 'error')
        return redirect(url_for('index'))

    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash('Amount must be a positive number.', 'error')
        return redirect(url_for('index'))

    db = get_db()
    db.execute(
        "INSERT INTO expenses (amount, category, date, note) VALUES (?, ?, ?, ?)",
        [amount, category, date, note or None]
    )
    db.commit()
    flash('Expense added.', 'success')
    return redirect(url_for('index'))


@app.route('/delete/<int:expense_id>', methods=['POST'])
def delete_expense(expense_id):
    db = get_db()
    db.execute("DELETE FROM expenses WHERE id = ?", [expense_id])
    db.commit()
    flash('Expense deleted.', 'success')
    return redirect(url_for('index'))


@app.route('/edit/<int:expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    db = get_db()

    if request.method == 'GET':
        expense = db.execute(
            "SELECT * FROM expenses WHERE id = ?", [expense_id]
        ).fetchone()
        if expense is None:
            flash('Expense not found.', 'error')
            return redirect(url_for('index'))
        return render_template('edit.html', expense=expense, categories=CATEGORIES)

    # POST – update
    amount   = request.form.get('amount', '').strip()
    category = request.form.get('category', '').strip()
    date     = request.form.get('date', '').strip()
    note     = request.form.get('note', '').strip()

    if not amount or not category or not date:
        flash('Amount, category, and date are required.', 'error')
        return redirect(url_for('edit_expense', expense_id=expense_id))

    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash('Amount must be a positive number.', 'error')
        return redirect(url_for('edit_expense', expense_id=expense_id))

    db.execute(
        "UPDATE expenses SET amount=?, category=?, date=?, note=? WHERE id=?",
        [amount, category, date, note or None, expense_id]
    )
    db.commit()
    flash('Expense updated.', 'success')
    return redirect(url_for('index'))


@app.route('/export')
def export_csv():
    db = get_db()
    category_filter = request.args.get('category', '').strip()
    month_filter    = request.args.get('month', '').strip()

    where, params = build_filter_query(category_filter, month_filter)

    expenses = db.execute(
        f"SELECT id, date, category, amount, note FROM expenses {where} ORDER BY date DESC",
        params
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Date', 'Category', 'Amount', 'Note'])
    for e in expenses:
        writer.writerow([e['id'], e['date'], e['category'], e['amount'], e['note'] or ''])

    filename = 'expenses'
    if category_filter:
        filename += f'_{category_filter}'
    if month_filter:
        filename += f'_{month_filter}'
    filename += '.csv'

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/charts')
def charts():
    db = get_db()

    monthly = db.execute("""
        SELECT strftime('%Y-%m', date) AS month, SUM(amount) AS total
        FROM expenses
        GROUP BY month
        ORDER BY month
    """).fetchall()

    by_category = db.execute("""
        SELECT category, SUM(amount) AS total
        FROM expenses
        GROUP BY category
        ORDER BY SUM(amount) DESC
    """).fetchall()

    monthly_labels = [r['month'] for r in monthly]
    monthly_values = [round(r['total'], 2) for r in monthly]

    cat_labels = [r['category'] for r in by_category]
    cat_values = [round(r['total'], 2) for r in by_category]

    return render_template(
        'charts.html',
        monthly_labels=monthly_labels,
        monthly_values=monthly_values,
        cat_labels=cat_labels,
        cat_values=cat_values,
    )


# ── Bootstrap ─────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

if __name__ == '__main__':
    # Local development only.
    # On Render, gunicorn is the entry point: `gunicorn app:app`
    # Render injects $PORT automatically; gunicorn reads it — no hardcoding needed.
    app.run(debug=True)
