import sqlite3
import os
import csv
import io
from flask import (
    Flask, render_template, request, redirect,
    url_for, g, flash, Response
)

app = Flask(__name__)
# Read secret key from environment; fall back to dev value locally.
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

# On Render the persistent disk is mounted at /var/data (set via RENDER_DATA_DIR).
# Locally we fall back to Flask's instance/ folder so nothing breaks.
_DATA_DIR = os.environ.get('RENDER_DATA_DIR', app.instance_path)
DATABASE  = os.path.join(_DATA_DIR, 'expenses.db')

CATEGORIES = [
    'Food', 'Transport', 'Housing', 'Health',
    'Entertainment', 'Shopping', 'Education', 'Other'
]


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        # Ensure the data directory exists (matters both locally and on Render).
        os.makedirs(_DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
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
