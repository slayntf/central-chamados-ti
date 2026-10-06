from flask import Flask, request, redirect, url_for, session, render_template_string, flash
import sqlite3, os, secrets
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APP_DIR, 'chamados.db')
app = Flask(__name__)
app.secret_key = os.environ.get('TI_SECRET_KEY', secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('TI_COOKIE_SECURE','0') == '1')

STATUSES = ['Aberto', 'Em atendimento', 'Aguardando usuário', 'Aguardando terceiro', 'Resolvido', 'Encerrado']
PRIORITIES = ['Baixa', 'Média', 'Alta', 'Crítica']
CATEGORIES = ['Computador', 'Internet/Cabo/Wifi', 'Notebook', 'Celular', 'Impressora', 'ONT', 'Periférico', 'E-mail', 'Câmera', 'Acesso/Senha', 'Telefone', 'Sistema', 'Outros']
SECTORS = ['Comercial', 'Financeiro', 'RH', 'Estoque', 'NOC']
IMPACTS = ['Somente eu', 'Meu setor', 'Vários setores', 'Toda a empresa']
ROLES = [('colaborador', 'Colaborador'), ('tecnico', 'Técnico TI'), ('supervisor', 'Supervisor'), ('admin', 'Administrador')]
SLA = {
    'Crítica': {'response': 15, 'resolution': 120},
    'Alta': {'response': 30, 'resolution': 240},
    'Média': {'response': 120, 'resolution': 1 * 8 * 60},
    'Baixa': {'response': 240, 'resolution': 3 * 8 * 60},
}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'colaborador',
        sector TEXT NOT NULL DEFAULT 'Não informado',
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        category TEXT NOT NULL,
        priority TEXT NOT NULL,
        impact TEXT NOT NULL DEFAULT 'Somente eu',
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Aberto',
        assignee_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        resolution TEXT
    );
    CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    );
    ''')
    # Compatibilidade com banco de versões anteriores.
    cols = {r['name'] for r in c.execute('PRAGMA table_info(tickets)').fetchall()}
    if 'impact' not in cols:
        c.execute("ALTER TABLE tickets ADD COLUMN impact TEXT NOT NULL DEFAULT 'Somente eu'")
    c.commit()
    c.close()


def parse_dt(value):
    return datetime.strptime(value, '%d/%m/%Y %H:%M')


def fmt_dt(value):
    return value.strftime('%d/%m/%Y %H:%M')


def now_dt():
    return datetime.now().replace(second=0, microsecond=0)


def now():
    return fmt_dt(now_dt())


def is_working_time(dt):
    # Segunda a sábado: 08:30–18:00. Domingo: 08:30–12:00.
    start = dt.replace(hour=8, minute=30, second=0, microsecond=0)
    end = dt.replace(hour=12 if dt.weekday() == 6 else 18, minute=0, second=0, microsecond=0)
    return dt.weekday() <= 6 and start <= dt < end


def next_work_start(dt):
    dt = dt.replace(second=0, microsecond=0)
    for _ in range(8):
        start = dt.replace(hour=8, minute=30, second=0, microsecond=0)
        end = dt.replace(hour=12 if dt.weekday() == 6 else 18, minute=0, second=0, microsecond=0)
        if dt < start:
            return start
        if start <= dt < end:
            return dt
        dt = (dt + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
    return dt


def add_business_minutes(start, minutes):
    current = next_work_start(start)
    remaining = int(minutes)
    while remaining > 0:
        if not is_working_time(current):
            current = next_work_start(current)
            continue
        end = current.replace(hour=12 if current.weekday() == 6 else 18, minute=0, second=0, microsecond=0)
        available = int((end - current).total_seconds() // 60)
        if remaining <= available:
            return current + timedelta(minutes=remaining)
        remaining -= available
        current = (current + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
    return current


def business_minutes_between(start, end):
    if end <= start:
        return 0
    current = start
    total = 0
    while current < end:
        if is_working_time(current):
            close = current.replace(hour=12 if current.weekday() == 6 else 18, minute=0, second=0, microsecond=0)
            segment_end = min(close, end)
            total += max(0, int((segment_end - current).total_seconds() // 60))
            current = segment_end
        else:
            current = next_work_start(current + timedelta(minutes=1))
    return total


def ticket_sla(t, c=None):
    priority = t['priority']
    rules = SLA[priority]
    created = parse_dt(t['created_at'])
    response_due = add_business_minutes(created, rules['response'])
    resolution_due = add_business_minutes(created, rules['resolution'])
    nowv = now_dt()

    responded = False
    if c is None:
        own = db()
        rows = own.execute('''SELECT h.created_at, u.role FROM history h JOIN users u ON u.id=h.user_id
                             WHERE h.ticket_id=? AND h.action IN ('Comentário','Atualização') ORDER BY h.id LIMIT 1''', (t['id'],)).fetchall()
        own.close()
    else:
        rows = c.execute('''SELECT h.created_at, u.role FROM history h JOIN users u ON u.id=h.user_id
                            WHERE h.ticket_id=? AND h.action IN ('Comentário','Atualização') ORDER BY h.id LIMIT 1''', (t['id'],)).fetchall()
    first = next((r for r in rows if r['role'] in ('tecnico', 'supervisor', 'admin')), None)
    if first:
        responded = True
        response_due = parse_dt(first['created_at'])

    closed = t['status'] in ('Resolvido', 'Encerrado')
    if closed:
        end = parse_dt(t['updated_at'])
    else:
        end = nowv

    response_overdue = not responded and nowv > add_business_minutes(created, rules['response'])
    resolution_overdue = not closed and nowv > resolution_due
    response_remaining = 0 if responded else max(0, business_minutes_between(nowv, response_due))
    resolution_remaining = 0 if closed else max(0, business_minutes_between(nowv, resolution_due))

    if responded:
        response_label = 'Respondido'
        response_class = 'ok'
    elif response_overdue:
        response_label = 'Vencido'
        response_class = 'danger'
    else:
        response_label = f'{response_remaining} min'
        response_class = 'warning' if response_remaining <= max(15, rules['response'] // 2) else 'ok'

    if closed:
        solution_label = 'Concluído'
        solution_class = 'ok' if not resolution_overdue else 'danger'
    elif resolution_overdue:
        solution_label = 'Vencido'
        solution_class = 'danger'
    else:
        if resolution_remaining >= 1440:
            solution_label = f'{resolution_remaining // 1440}d {resolution_remaining % 1440 // 60}h'
        elif resolution_remaining >= 60:
            solution_label = f'{resolution_remaining // 60}h {resolution_remaining % 60}min'
        else:
            solution_label = f'{resolution_remaining} min'
        solution_class = 'warning' if resolution_remaining <= max(30, rules['resolution'] // 4) else 'ok'

    return {
        'response_due': response_due,
        'resolution_due': resolution_due,
        'response_overdue': response_overdue,
        'resolution_overdue': resolution_overdue,
        'response_label': response_label,
        'response_class': response_class,
        'solution_label': solution_label,
        'solution_class': solution_class,
        'breached': response_overdue or resolution_overdue,
    }


def user():
    if 'uid' not in session:
        return None
    c = db()
    u = c.execute('SELECT * FROM users WHERE id=? AND active=1', (session['uid'],)).fetchone()
    c.close()
    return u


def req(f):
    @wraps(f)
    def w(*a, **k):
        if not user():
            return redirect(url_for('login'))
        return f(*a, **k)
    return w


def staff(f):
    @wraps(f)
    def w(*a, **k):
        u = user()
        if not u:
            return redirect(url_for('login'))
        if u['role'] not in ('tecnico', 'supervisor', 'admin'):
            flash('Acesso restrito à equipe de TI, Supervisão e Administração.', 'error')
            return redirect(url_for('tickets'))
        return f(*a, **k)
    return w


BASE = '''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:#f4f6fb;color:#202636}header{background:#263b80;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;gap:16px}header strong{font-size:20px}nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap}nav a{color:#fff;text-decoration:none;font-size:14px}.container{max-width:1240px;margin:24px auto;padding:0 16px}.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 10px #0001;margin-bottom:18px}h1{margin-top:0;font-size:25px}h2{margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.stat{padding:18px;border-radius:10px;background:#eef1ff}.stat b{font-size:28px;display:block;margin-top:5px}.stat.danger{background:#fff0f1}.stat.warn{background:#fff7e6}.stat.ok{background:#edf9f0}label{font-weight:bold;font-size:13px;display:block;margin:12px 0 6px}input,select,textarea{width:100%;padding:11px;border:1px solid #ccd2df;border-radius:8px;font:inherit}textarea{min-height:130px}button,.btn{display:inline-block;background:#263b80;color:white;border:0;border-radius:8px;padding:11px 16px;text-decoration:none;cursor:pointer}.btn.secondary{background:#6b7280}.btn.light{background:#eef1ff;color:#263b80}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #edf0f5;font-size:14px}th{font-size:12px;color:#667085}.flash{padding:12px;border-radius:8px;background:#def7e8;margin-bottom:14px}.flash.error{background:#ffe1e5}.login{max-width:430px;margin:80px auto}.muted{color:#667085;font-size:13px}.timeline{border-left:3px solid #dce2f2;padding-left:16px}.event{margin-bottom:15px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:bold;background:#eef1ff;color:#263b80}.badge.danger{background:#ffe1e5;color:#a61b2b}.badge.warning{background:#fff0cf;color:#8a5a00}.badge.ok{background:#def7e8;color:#176b36}.small{font-size:12px}.notice{padding:12px;background:#f7f8fc;border-radius:8px}.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;align-items:end}.nowrap{white-space:nowrap}
</style></head><body><header><strong>Central de TI</strong><nav>{% if user %}{% if user['role'] in ['tecnico','supervisor','admin'] %}<a href="{{url_for('dashboard')}}">Dashboard</a>{% endif %}<a href="{{url_for('new_ticket')}}">Abrir chamado</a><a href="{{url_for('tickets')}}">Chamados</a>{% if user['role'] in ['supervisor','admin'] %}<a href="{{url_for('users')}}">Usuários</a>{% endif %}<a href="{{url_for('change_password')}}">Alterar senha</a><a href="{{url_for('logout')}}">Sair</a>{% endif %}</nav></header><div class="container">{% with msgs=get_flashed_messages(with_categories=true) %}{% for cat,msg in msgs %}<div class="flash {{'error' if cat=='error' else ''}}">{{msg}}</div>{% endfor %}{% endwith %}{{body|safe}}</div></body></html>'''


def page(body, title='Central de TI'):
    return render_template_string(BASE, body=body, title=title, user=user())


def priority_badge(priority):
    cls = {'Crítica': 'danger', 'Alta': 'danger', 'Média': 'warning', 'Baixa': 'ok'}.get(priority, '')
    return f'<span class="badge {cls}">{priority}</span>'


def sla_badges(t, c=None):
    s = ticket_sla(t, c)
    return f'<span class="badge {s["response_class"]}">Resposta: {s["response_label"]}</span> <span class="badge {s["solution_class"]}">Solução: {s["solution_label"]}</span>'


@app.before_request
def require_initial_setup():
    if request.endpoint in ('setup', 'static'):
        return None
    c = db()
    count = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    c.close()
    if count == 0:
        return redirect(url_for('setup'))
    return None


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    c = db()
    count = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    c.close()
    if count > 0:
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        username = request.form['username'].strip().lower()
        password = request.form['password']
        confirm = request.form['confirm']
        if len(password) < 8:
            flash('A senha precisa ter pelo menos 8 caracteres.', 'error')
        elif password != confirm:
            flash('As senhas não conferem.', 'error')
        elif not name or not username:
            flash('Preencha todos os campos.', 'error')
        else:
            c = db()
            try:
                c.execute('INSERT INTO users(name,username,password,role,sector) VALUES(?,?,?,?,?)', (name, username, generate_password_hash(password), 'admin', 'TI'))
                c.commit()
                c.close()
                flash('Administrador criado. Agora faça o login.', 'success')
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                c.close()
                flash('Esse usuário já existe.', 'error')
    return page('''<div class="login card"><h1>Configuração inicial</h1><p class="muted">Crie a primeira conta de administrador da Central de Chamados de TI.</p><form method="post"><label>Nome</label><input name="name" required><label>Usuário</label><input name="username" autocomplete="username" required><label>Senha</label><input name="password" type="password" minlength="8" autocomplete="new-password" required><label>Confirmar senha</label><input name="confirm" type="password" minlength="8" autocomplete="new-password" required><div class="actions"><button>Criar administrador</button></div></form></div>''', 'Configuração inicial')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        c = db(); u = c.execute('SELECT * FROM users WHERE username=? AND active=1', (request.form['username'],)).fetchone(); c.close()
        if u and check_password_hash(u['password'], request.form['password']):
            session['uid'] = u['id']
            return redirect(url_for('dashboard'))
        flash('Usuário ou senha inválidos.', 'error')
    return page('''<div class="login card"><h1>Entrar</h1><p class="muted">Central de Chamados de TI</p><form method="post"><label>Usuário</label><input name="username" required><label>Senha</label><input name="password" type="password" required><div class="actions"><button>Entrar</button></div></form></div>''', 'Login')


@app.route('/change-password', methods=['GET', 'POST'])
@req
def change_password():
    u = user()
    if request.method == 'POST':
        current = request.form['current']
        new = request.form['new']
        confirm = request.form['confirm']
        if not check_password_hash(u['password'], current):
            flash('A senha atual está incorreta.', 'error')
        elif len(new) < 8:
            flash('A nova senha precisa ter pelo menos 8 caracteres.', 'error')
        elif new != confirm:
            flash('As senhas não conferem.', 'error')
        else:
            c = db(); c.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(new), u['id'])); c.commit(); c.close()
            flash('Senha alterada com sucesso.', 'success')
            return redirect(url_for('tickets'))
    return page('''<div class="card" style="max-width:520px"><h1>Alterar senha</h1><form method="post"><label>Senha atual</label><input name="current" type="password" autocomplete="current-password" required><label>Nova senha</label><input name="new" type="password" minlength="8" autocomplete="new-password" required><label>Confirmar nova senha</label><input name="confirm" type="password" minlength="8" autocomplete="new-password" required><div class="actions"><button>Alterar senha</button></div></form></div>''', 'Alterar senha')


@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))


@app.route('/')
@req
def dashboard():
    u = user()
    if u['role'] not in ('tecnico', 'supervisor', 'admin'):
        return redirect(url_for('tickets'))
    c = db()
    rows = c.execute('''SELECT t.*,r.name requester,r.sector sector,a.name assignee FROM tickets t JOIN users r ON r.id=t.requester_id LEFT JOIN users a ON a.id=t.assignee_id ORDER BY t.id DESC''').fetchall()
    counts = {
        'total': len(rows),
        'open': sum(r['status'] not in ('Resolvido', 'Encerrado') for r in rows),
        'attending': sum(r['status'] == 'Em atendimento' for r in rows),
        'resolved': sum(r['status'] in ('Resolvido', 'Encerrado') for r in rows),
    }
    sla_data = [(r, ticket_sla(r, c)) for r in rows]
    breached = sum(s['breached'] for r, s in sla_data if r['status'] not in ('Resolvido', 'Encerrado'))
    near = sum((not s['breached']) and (s['response_class'] == 'warning' or s['solution_class'] == 'warning') for r, s in sla_data if r['status'] not in ('Resolvido', 'Encerrado'))
    priority_counts = {p: sum(r['priority'] == p for r in rows) for p in PRIORITIES}
    sector_counts = {s: sum(r['sector'] == s for r in rows) for s in SECTORS}
    recent = rows[:12]
    c.close()
    tr = ''.join(f"<tr><td class='nowrap'>TI-{r['id']:06d}</td><td><a href='{url_for('ticket',id=r['id'])}'>{r['subject']}</a></td><td>{r['sector']}</td><td>{priority_badge(r['priority'])}</td><td>{r['status']}</td><td>{r['assignee'] or '—'}</td><td>{sla_badges(r)}</td></tr>" for r in recent)
    pc = ''.join(f'<div class="stat"><span>{p}</span><b>{priority_counts[p]}</b></div>' for p in PRIORITIES)
    sc = ''.join(f'<div class="stat"><span>{s}</span><b>{sector_counts[s]}</b></div>' for s in SECTORS)
    return page(f'''<div class="top"><div><h1>Dashboard do TI</h1><p class="muted">Visão operacional — acesso restrito a TI, Supervisão e Administração.</p></div><a class="btn" href="{url_for('new_ticket')}">+ Abrir chamado</a></div>
    <div class="grid"><div class="stat">Total de chamados<b>{counts['total']}</b></div><div class="stat">Em aberto<b>{counts['open']}</b></div><div class="stat">Em atendimento<b>{counts['attending']}</b></div><div class="stat ok">Resolvidos/Encerrados<b>{counts['resolved']}</b></div><div class="stat danger">SLA vencido<b>{breached}</b></div><div class="stat warn">SLA próximo<b>{near}</b></div></div>
    <div class="card"><h2>Por prioridade</h2><div class="grid">{pc}</div></div>
    <div class="card"><h2>Por setor</h2><div class="grid">{sc}</div></div>
    <div class="card"><h2>Chamados recentes</h2><table><tr><th>Nº</th><th>Assunto</th><th>Setor</th><th>Prioridade</th><th>Status</th><th>Responsável</th><th>SLA</th></tr>{tr or '<tr><td colspan="7">Nenhum chamado.</td></tr>'}</table></div>''', 'Dashboard TI')


@app.route('/tickets')
@req
def tickets():
    u = user(); c = db(); q = '''SELECT t.*,r.name requester,r.sector sector,a.name assignee FROM tickets t JOIN users r ON r.id=t.requester_id LEFT JOIN users a ON a.id=t.assignee_id'''; args=[]
    if u['role'] not in ('tecnico', 'supervisor', 'admin'):
        q += ' WHERE t.requester_id=?'; args = [u['id']]
    status = request.args.get('status', '').strip()
    priority = request.args.get('priority', '').strip()
    if status in STATUSES:
        q += (' AND' if ' WHERE ' in q else ' WHERE') + ' t.status=?'; args.append(status)
    if priority in PRIORITIES:
        q += (' AND' if ' WHERE ' in q else ' WHERE') + ' t.priority=?'; args.append(priority)
    rows = c.execute(q + ' ORDER BY t.id DESC', args).fetchall(); c.close()
    tr = ''.join(f"<tr><td class='nowrap'>TI-{r['id']:06d}</td><td><a href='{url_for('ticket',id=r['id'])}'>{r['subject']}</a></td><td>{r['requester']}</td><td>{r['sector']}</td><td>{priority_badge(r['priority'])}</td><td>{r['status']}</td><td>{r['assignee'] or '—'}</td><td>{sla_badges(r)}</td></tr>" for r in rows)
    filters = f'''<div class="card"><form method="get"><div class="filters"><div><label>Status</label><select name="status"><option value="">Todos</option>{''.join(f"<option {'selected' if s==status else ''}>{s}</option>" for s in STATUSES)}</select></div><div><label>Prioridade</label><select name="priority"><option value="">Todas</option>{''.join(f"<option {'selected' if p==priority else ''}>{p}</option>" for p in PRIORITIES)}</select></div><div><button>Filtrar</button></div></div></form></div>'''
    return page(f'''<div class="top"><h1>{'Meus chamados' if u['role']=='colaborador' else 'Chamados'}</h1><a class="btn" href="{url_for('new_ticket')}">+ Abrir chamado</a></div>{filters}<div class="card"><table><tr><th>Nº</th><th>Assunto</th><th>Solicitante</th><th>Setor</th><th>Prioridade</th><th>Status</th><th>Responsável</th><th>SLA</th></tr>{tr or '<tr><td colspan="8">Nenhum chamado encontrado.</td></tr>'}</table></div>''', 'Chamados')


@app.route('/ticket/new', methods=['GET', 'POST'])
@req
def new_ticket():
    u = user()
    if request.method == 'POST':
        priority = request.form['priority']
        impact = request.form['impact']
        # Regras de proteção: somente equipe pode registrar/alterar prioridade Crítica.
        if priority == 'Crítica' and u['role'] == 'colaborador':
            priority = 'Alta'
        t = now(); c = db()
        cur = c.execute('''INSERT INTO tickets(requester_id,subject,category,priority,impact,description,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?)''', (u['id'], request.form['subject'], request.form['category'], priority, impact, request.form['description'], 'Aberto', t, t))
        tid = cur.lastrowid
        c.execute('INSERT INTO history(ticket_id,user_id,action,note,created_at) VALUES(?,?,?,?,?)', (tid, u['id'], 'Chamado aberto', f'Impacto informado: {impact}.', t))
        c.commit(); c.close()
        flash(f'Chamado TI-{tid:06d} aberto com sucesso.', 'success')
        return redirect(url_for('ticket', id=tid))
    cats=''.join(f'<option>{x}</option>' for x in CATEGORIES); prs=''.join(f'<option>{x}</option>' for x in PRIORITIES if x != 'Crítica' or u['role'] != 'colaborador'); impacts=''.join(f'<option>{x}</option>' for x in IMPACTS)
    return page(f'''<div class="card"><h1>Abrir chamado</h1><div class="notice">Informe o impacto real do problema. A equipe de TI poderá ajustar a prioridade quando necessário.</div><form method="post"><label>Assunto</label><input name="subject" placeholder="Ex.: Computador não liga" required><label>Categoria</label><select name="category">{cats}</select><label>Impacto</label><select name="impact">{impacts}</select><label>Prioridade</label><select name="priority">{prs}</select><label>Descrição do problema</label><textarea name="description" placeholder="Descreva o que aconteceu, quando começou e, se possível, o que já foi testado." required></textarea><div class="actions"><button>Abrir chamado</button><a class="btn secondary" href="{url_for('tickets')}">Cancelar</a></div></form></div>''', 'Abrir chamado')


@app.route('/ticket/<int:id>', methods=['GET', 'POST'])
@req
def ticket(id):
    u=user(); c=db(); t=c.execute('''SELECT t.*,r.name requester,r.sector sector,a.name assignee FROM tickets t JOIN users r ON r.id=t.requester_id LEFT JOIN users a ON a.id=t.assignee_id WHERE t.id=?''', (id,)).fetchone()
    if not t:
        c.close(); return 'Chamado não encontrado', 404
    is_staff = u['role'] in ('tecnico', 'supervisor', 'admin')
    if not is_staff and t['requester_id'] != u['id']:
        c.close(); return 'Acesso negado', 403
    if request.method == 'POST':
        n=now(); action=request.form['action']
        if action == 'comment':
            note=request.form['note'].strip()
            if not note:
                flash('O comentário não pode ficar vazio.', 'error')
            else:
                c.execute('INSERT INTO history(ticket_id,user_id,action,note,created_at) VALUES(?,?,?,?,?)', (id,u['id'],'Comentário',note,n))
                c.execute('UPDATE tickets SET updated_at=? WHERE id=?', (n,id))
        elif action == 'update' and is_staff:
            status=request.form['status']; pri=request.form['priority']; ass=request.form.get('assignee') or None; sol=request.form.get('resolution') or None
            c.execute('UPDATE tickets SET status=?,priority=?,assignee_id=?,resolution=?,updated_at=? WHERE id=?', (status,pri,ass,sol,n,id))
            c.execute('INSERT INTO history(ticket_id,user_id,action,note,created_at) VALUES(?,?,?,?,?)', (id,u['id'],'Atualização',f'Status: {status} | Prioridade: {pri} | Responsável: {ass or "Não atribuído"}',n))
        c.commit(); c.close(); return redirect(url_for('ticket',id=id))
    hist=c.execute('SELECT h.*,u.name,u.role FROM history h JOIN users u ON u.id=h.user_id WHERE ticket_id=? ORDER BY h.id DESC',(id,)).fetchall()
    techs=c.execute("SELECT id,name FROM users WHERE role IN ('tecnico','supervisor','admin') AND active=1 ORDER BY name").fetchall(); sla_info=ticket_sla(t,c); c.close()
    events=''.join(f"<div class='event'><b>{h['action']}</b> — {h['name']}<div class='muted'>{h['created_at']}</div><div>{h['note'] or ''}</div></div>" for h in hist)
    admin=''
    if is_staff:
        opts=''.join(f"<option value='{x['id']}' {'selected' if x['id']==t['assignee_id'] else ''}>{x['name']}</option>" for x in techs)
        admin=f'''<div class="card"><h2>Gestão do chamado</h2><form method="post"><input type="hidden" name="action" value="update"><label>Status</label><select name="status">{''.join(f"<option {'selected' if s==t['status'] else ''}>{s}</option>" for s in STATUSES)}</select><label>Prioridade</label><select name="priority">{''.join(f"<option {'selected' if p==t['priority'] else ''}>{p}</option>" for p in PRIORITIES)}</select><label>Responsável</label><select name="assignee"><option value="">Não atribuído</option>{opts}</select><label>Solução/observação final</label><textarea name="resolution">{t['resolution'] or ''}</textarea><div class="actions"><button>Salvar alterações</button></div></form></div>'''
    return page(f'''<div class="top"><h1>TI-{t['id']:06d}</h1><a class="btn light" href="{url_for('tickets')}">Voltar</a></div><div class="card"><h2>{t['subject']}</h2><p><b>Solicitante:</b> {t['requester']} ({t['sector']})</p><p><b>Categoria:</b> {t['category']} &nbsp; <b>Impacto:</b> {t['impact']} &nbsp; <b>Prioridade:</b> {priority_badge(t['priority'])} &nbsp; <b>Status:</b> {t['status']}</p><p><b>Aberto em:</b> {t['created_at']} &nbsp; <b>Atualizado:</b> {t['updated_at']}</p><div class="notice"><b>SLA de resposta:</b> {sla_info['response_label']} &nbsp; | &nbsp; <b>SLA de solução:</b> {sla_info['solution_label']}<br><span class="small">Expediente do TI: segunda a sábado 08:30–18:00; domingo 08:30–12:00.</span></div><hr><p>{t['description']}</p>{('<p><b>Solução:</b> '+t['resolution']+'</p>') if t['resolution'] else ''}</div>{admin}<div class="card"><h2>Interações</h2><div class="timeline">{events or '<p class="muted">Nenhuma interação.</p>'}</div><form method="post"><input type="hidden" name="action" value="comment"><label>Adicionar comentário</label><textarea name="note" required></textarea><div class="actions"><button>Enviar comentário</button></div></form></div>''',f"TI-{t['id']:06d}")


@app.route('/users', methods=['GET', 'POST'])
@staff
def users():
    u=user()
    if u['role'] not in ('supervisor', 'admin'):
        flash('A gestão de usuários é restrita à Supervisão e Administração.', 'error')
        return redirect(url_for('dashboard'))
    c=db()
    if request.method == 'POST':
        try:
            c.execute('INSERT INTO users(name,username,password,role,sector) VALUES(?,?,?,?,?)', (request.form['name'],request.form['username'],generate_password_hash(request.form['password']),request.form['role'],request.form['sector']))
            c.commit(); flash('Usuário criado.', 'success')
        except sqlite3.IntegrityError:
            flash('Esse usuário já existe.', 'error')
        c.close(); return redirect(url_for('users'))
    rows=c.execute('SELECT name,username,role,sector,active FROM users ORDER BY name').fetchall(); c.close()
    tr=''.join(f"<tr><td>{r['name']}</td><td>{r['username']}</td><td>{dict(ROLES).get(r['role'],r['role'])}</td><td>{r['sector']}</td><td>{'Ativo' if r['active'] else 'Inativo'}</td></tr>" for r in rows)
    return page(f'''<div class="card"><h1>Usuários</h1><form method="post"><div class="grid"><div><label>Nome</label><input name="name" required></div><div><label>Usuário</label><input name="username" required></div><div><label>Senha</label><input name="password" required></div><div><label>Setor</label><select name="sector">{''.join(f'<option>{x}</option>' for x in SECTORS)}</select></div><div><label>Perfil</label><select name="role">{''.join(f'<option value="{v}">{l}</option>' for v,l in ROLES)}</select></div></div><div class="actions"><button>Criar usuário</button></div></form></div><div class="card"><table><tr><th>Nome</th><th>Usuário</th><th>Perfil</th><th>Setor</th><th>Status</th></tr>{tr}</table></div>''','Usuários')


init_db()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
