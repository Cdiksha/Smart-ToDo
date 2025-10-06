# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import threading, time, os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret123")

# DB
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tasks.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Mail (optional)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
mail = Mail(app)

# ---------- MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    email = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(200))
    columns = db.relationship('Column', backref='user', lazy=True)
    tasks = db.relationship('Task', backref='user', lazy=True)

class Column(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    position = db.Column(db.Integer, default=0)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    tasks = db.relationship('Task', backref='column', lazy=True)

class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    due_date = db.Column(db.DateTime)
    priority = db.Column(db.String(10), default='Medium')  # Low/Medium/High
    complete = db.Column(db.Boolean, default=False)
    reminder_set = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default="todo")
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    column_id = db.Column(db.Integer, db.ForeignKey('column.id'), nullable=True)

with app.app_context():
    db.create_all()

# ---------- Helpers ----------
def ensure_default_columns(user_id):
    """Create default workflow columns for a user if none exist."""
    cols = Column.query.filter_by(user_id=user_id).order_by(Column.position).all()
    if not cols:
        defaults = ["To Do", "In Progress", "Done"]
        for i, name in enumerate(defaults):
            c = Column(name=name, position=i, user_id=user_id)
            db.session.add(c)
        db.session.commit()

def calculate_stats(tasks):
    return {
        "total": len(tasks),
        "pending": len([t for t in tasks if not t.complete]),
        "completed": len([t for t in tasks if t.complete]),
        "overdue": len([t for t in tasks if t.due_date and t.due_date < datetime.now() and not t.complete])
    }

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please login first!", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

# ---------- Reminder worker ----------
def reminder_worker():
    while True:
        with app.app_context():
            now = datetime.now()
            tasks = Task.query.filter_by(reminder_set=True, complete=False).all()
            for t in tasks:
                if t.due_date:
                    seconds_left = (t.due_date - now).total_seconds()
                    # send reminder if due within next 5 minutes
                    if 0 < seconds_left < 300:
                        try:
                            if app.config.get('MAIL_USERNAME') and app.config.get('MAIL_PASSWORD'):
                                msg = Message(
                                    subject="Task Reminder",
                                    sender=app.config['MAIL_USERNAME'],
                                    recipients=[t.user.email],
                                    body=f"Reminder: {t.title}\nDue at {t.due_date.strftime('%Y-%m-%d %H:%M')}\n\n{t.description or ''}"
                                )
                                mail.send(msg)
                        except Exception as e:
                            print("Reminder send error:", e)
                        t.reminder_set = False
                        db.session.commit()
        time.sleep(60)

# ---------- ROUTES ----------
@app.route('/')
@login_required
def index():
    user = User.query.get(session['user_id'])
    tasks = Task.query.filter_by(user_id=user.id).order_by(Task.due_date).all()
    stats = calculate_stats(tasks)
    return render_template('index.html', user=user, tasks=tasks, stats=stats, dark_mode=session.get('dark_mode', False))

@app.route('/workflow')  # renamed page
@login_required
def workflow():
    user = User.query.get(session['user_id'])
    ensure_default_columns(user.id)
    sort_by = request.args.get('sort', 'due_date')
    tasks_query = Task.query.filter_by(user_id=user.id)
    
    if sort_by == 'priority':
        tasks_query = tasks_query.order_by(
            db.case(
                (Task.priority == 'High', 0),
                (Task.priority == 'Medium', 1),
                (Task.priority == 'Low', 2),
                else_=3
            )
        )
    elif sort_by == 'created_at':
        tasks_query = tasks_query.order_by(Task.created_at.desc())
    else:
        tasks_query = tasks_query.order_by(Task.due_date.asc())

    tasks = tasks_query.all()
    columns = Column.query.filter_by(user_id=user.id).order_by(Column.position).all()
    default_names = ['To Do', 'In Progress', 'Completed', 'Pending', 'Done']
    existing_names = [col.name for col in columns]

    for name in default_names:
        if name not in existing_names:
            new_col = Column(name=name, user_id=user.id)
            db.session.add(new_col)

    
    # Safe sorting: put tasks with no due_date at the bottom
    for col in columns:
        col.tasks = sorted(col.tasks, key=lambda t: t.due_date if t.due_date else datetime.max)

    stats = calculate_stats(tasks)
    return render_template(
        'workflow.html',
        user=user,
        tasks=tasks,
        columns=columns,
        stats=stats,
        datetime=datetime,
        sort_by=sort_by,
        dark_mode=session.get('dark_mode', False)
    )

@app.route('/completed')
@login_required
def completed():
    user = User.query.get(session['user_id'])
    tasks = Task.query.filter_by(user_id=user.id, complete=True).order_by(Task.due_date).all()
    stats = calculate_stats(Task.query.filter_by(user_id=user.id).all())
    return render_template('completed.html', user=user, tasks=tasks, stats=stats, dark_mode=session.get('dark_mode', False))

@app.route('/pending')
@login_required
def pending():
    user = User.query.get(session['user_id'])
    tasks = Task.query.filter_by(user_id=user.id, complete=False).order_by(Task.due_date).all()
    stats = calculate_stats(Task.query.filter_by(user_id=user.id).all())
    return render_template('pending.html', user=user, tasks=tasks, stats=stats, dark_mode=session.get('dark_mode', False))

@app.route("/add", methods=["POST"])
@login_required
def add():
    title = (request.form.get("title") or "").strip()
    desc = request.form.get("desc")
    due = request.form.get("due")
    priority = request.form.get("priority") or "Medium"
    column_id = request.form.get("column_id")
    reminder = 'reminder' in request.form

    if not title:
        flash("Title required", "danger")
        return redirect(request.referrer or url_for('index'))

    # parse datetime safely
    due_date = None
    if due:
        try:
            due_date = datetime.strptime(due, "%Y-%m-%dT%H:%M")
        except Exception:
            due_date = None

    user_id = session['user_id']
    # if column provided use it, else put in first column (create defaults if necessary)
    ensure_default_columns(user_id)
    if column_id:
        try:
            column_id = int(column_id)
            col = Column.query.filter_by(id=column_id, user_id=user_id).first()
            if not col:
                column_id = Column.query.filter_by(user_id=user_id).order_by(Column.position).first().id
        except Exception:
            column_id = Column.query.filter_by(user_id=user_id).order_by(Column.position).first().id
    else:
        first_col = Column.query.filter_by(user_id=user_id).order_by(Column.position).first()
        column_id = first_col.id if first_col else None

    task = Task(
        title=title,
        description=desc,
        due_date=due_date,
        priority=priority,
        status="todo",
        user_id=user_id,
        column_id=column_id,
        reminder_set=bool(reminder)
    )
    db.session.add(task)
    db.session.commit()
    flash("Task added.", "success")
    return redirect(request.referrer or url_for('index'))

@app.route("/add_column", methods=["POST"])
@login_required
def add_column():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name required", "danger")
        return redirect(url_for('workflow'))
    user_id = session['user_id']
    pos = Column.query.filter_by(user_id=user_id).count()
    col = Column(name=name, user_id=user_id, position=pos)
    db.session.add(col)
    db.session.commit()
    flash("Column added.", "success")
    return redirect(url_for('workflow'))

@app.route("/update_column/<int:task_id>/<int:col_id>", methods=["POST"])
@login_required
def update_column(task_id, col_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    col = Column.query.filter_by(id=col_id, user_id=session['user_id']).first()
    if not col:
        return jsonify({"success": False, "error": "Column not found"}), 404
    task.column_id = col_id
    # optional: set status based on column name
    task.status = col.name.lower()
    db.session.commit()
    return jsonify({"success": True})

@app.route("/update_status/<int:task_id>/<string:new_status>", methods=["POST"])
@login_required
def update_status(task_id, new_status):
    task = Task.query.get_or_404(task_id)
    if task.user_id != session.get('user_id'):
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    task.status = new_status
    db.session.commit()
    return jsonify({"success": True})

@app.route("/toggle_reminder/<int:id>")
@login_required
def toggle_reminder(id):
    t = Task.query.get_or_404(id)
    if t.user_id != session.get('user_id'):
        flash("Not authorized", "danger")
        return redirect(request.referrer or url_for('index'))
    t.reminder_set = not t.reminder_set
    db.session.commit()
    flash("Reminder toggled.", "info")
    return redirect(request.referrer or url_for('index'))

@app.route('/toggle/<int:id>')
@login_required
def toggle(id):
    t = Task.query.get_or_404(id)
    if t.user_id != session.get('user_id'):
        flash("Not authorized.", "danger")
        return redirect(url_for('index'))
    t.complete = not t.complete
    db.session.commit()
    flash("Task updated.", "success")
    return redirect(request.referrer or url_for('index'))

@app.route('/delete/<int:id>')
@login_required
def delete(id):
    t = Task.query.get_or_404(id)
    if t.user_id != session.get('user_id'):
        flash("Not authorized.", "danger")
        return redirect(url_for('index'))
    db.session.delete(t)
    db.session.commit()
    flash("Task deleted.", "info")
    return redirect(request.referrer or url_for('index'))

# ---------- Auth ----------
@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        name = (request.form.get('name') or "").strip()
        email = (request.form.get('email') or "").strip().lower()
        password = (request.form.get('password') or "").strip()
        if not name or not email or not password:
            flash("Please fill all fields.", "danger")
            return render_template('signup.html')
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "warning")
            return render_template('signup.html')
        hashed = generate_password_hash(password)
        user = User(name=name, email=email, password=hashed)
        db.session.add(user)
        db.session.commit()
        # create default columns for new user
        ensure_default_columns(user.id)
        flash("Account created — please log in.", "success")
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or "").strip().lower()
        password = (request.form.get('password') or "").strip()
        if not email or not password:
            flash("Enter email and password.", "danger")
            return render_template('login.html')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            flash(f"Welcome, {user.name}!", "success")
            return redirect(url_for('index'))
        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.route("/toggle_theme")
@login_required
def toggle_theme():
    session['dark_mode'] = not session.get('dark_mode', False)
    return redirect(request.referrer or url_for('index'))

@app.route('/delete_column/<int:column_id>', methods=['POST'])
@login_required
def delete_column(column_id):
    column = Column.query.get(column_id)
    # Default columns should not be deleted
    default_columns = ['To Do', 'In Progress', 'Pending', 'Done']

    if column and column.name not in default_names:
        db.session.delete(column)
        db.session.commit()
        flash(f"Column '{column.name}' deleted successfully!", "success")
    else:
        flash("You cannot delete default columns.", "warning")
    return redirect(url_for('workflow'))


@app.route('/edit_task/<int:task_id>', methods=['POST'])
@login_required
def edit_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        flash("Task not found!", "danger")
        return redirect(url_for('workflow'))
    
    task.title = request.form['title']
    task.description = request.form['description']
    task.due_date = request.form['due_date'] or None
    task.priority = request.form['priority']
    db.session.commit()
    
    flash("Task updated!", "success")
    return redirect(url_for('workflow'))


# ---------- Start reminder thread and app ----------
if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        t = threading.Thread(target=reminder_worker, daemon=True)
        t.start()
    app.run(debug=True)
