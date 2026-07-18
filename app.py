from __future__ import annotations
import os, sqlite3, json, urllib.request, urllib.error
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR=Path(__file__).resolve().parent; DB_PATH=BASE_DIR/'design.db'

def load_local_env():
    env_path=BASE_DIR/'.env'
    if not env_path.exists(): return
    for raw in env_path.read_text(encoding='utf-8').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key,value=line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('\"').strip("'"))

load_local_env()
app=Flask(__name__); app.config['SECRET_KEY']=os.environ.get('SECRET_KEY','design-logistica-demo')
PRIORITY_LABEL={5:'Máxima',4:'Alta',3:'Media',2:'Normal',1:'Baja'}
USER_ROLES=['Administrador','Gerencia/Dirección','Jefatura de área','Supervisor/Coordinador','Usuario general','Operador']
PRIORITY_BY_ROLE={'Administrador':5,'Gerencia/Dirección':4,'Jefatura de área':4,'Supervisor/Coordinador':3,'Usuario general':2,'Solicitante':2}
REQUEST_TYPES=['Traslado de personal','Retiro de compra de material','Retiro despacho camión','Solicitud de camión y/o maquinaria','Solicitud de camioneta','Traslado de maquinaria','Trámite administrativo','Urgencias','Otras gestiones operacionales']
GOOGLE_ROUTES_URL='https://routes.googleapis.com/directions/v2:computeRoutes'

def is_operator(user=None):
    user=user or current_user()
    return bool(user and user['role']=='Operador')

def is_requester(user=None):
    user=user or current_user()
    return bool(user and user['role'] not in ('Administrador','Operador'))

def estimate_route(origin_address, destination_address):
    """Obtiene duración y distancia usando Google Routes API.

    Requiere GOOGLE_MAPS_API_KEY en las variables de entorno.
    """
    if origin_address.strip()==destination_address.strip():
        return {'minutes':0,'distance_km':0.0,'source':'Mismo lugar'}
    api_key=os.environ.get('GOOGLE_MAPS_API_KEY','').strip()
    if not api_key:
        raise RuntimeError('Falta configurar GOOGLE_MAPS_API_KEY.')
    payload={
        'origin':{'address':origin_address},
        'destination':{'address':destination_address},
        'travelMode':'DRIVE',
        'routingPreference':'TRAFFIC_AWARE',
        'languageCode':'es-CL',
        'units':'METRIC'
    }
    req=urllib.request.Request(
        GOOGLE_ROUTES_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type':'application/json',
            'X-Goog-Api-Key':api_key,
            'X-Goog-FieldMask':'routes.duration,routes.distanceMeters'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req,timeout=12) as response:
            data=json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode('utf-8',errors='ignore')
        raise RuntimeError(f'Google Routes respondió {exc.code}: {detail[:180]}') from exc
    except (urllib.error.URLError,TimeoutError) as exc:
        raise RuntimeError('No fue posible conectarse al servicio de rutas.') from exc
    routes=data.get('routes') or []
    if not routes:
        raise RuntimeError('No se encontró una ruta entre las direcciones seleccionadas.')
    route=routes[0]
    seconds=float(str(route.get('duration','0s')).rstrip('s') or 0)
    return {
        'minutes':max(1,round(seconds/60)),
        'distance_km':round(float(route.get('distanceMeters',0))/1000,1),
        'source':'Google Routes API'
    }
def db():
    if 'db' not in g:
        g.db=sqlite3.connect(DB_PATH); g.db.row_factory=sqlite3.Row; g.db.execute('PRAGMA foreign_keys=ON')
    return g.db
@app.teardown_appcontext
def close_db(_):
    c=g.pop('db',None)
    if c: c.close()

def init_db():
    """Crea una instalación limpia con una única cuenta administradora inicial."""
    c=sqlite3.connect(DB_PATH)
    c.executescript('''
    CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT,email TEXT UNIQUE,password_hash TEXT,role TEXT,phone TEXT DEFAULT '');
    CREATE TABLE operators(id INTEGER PRIMARY KEY,user_id INTEGER UNIQUE,status TEXT DEFAULT 'Habilitado',detail TEXT DEFAULT '',licenses TEXT DEFAULT 'B',base TEXT DEFAULT '',accredited_sites TEXT DEFAULT '',score REAL DEFAULT 4.8,FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE operator_shifts(id INTEGER PRIMARY KEY,operator_id INTEGER,shift_date TEXT,start_time TEXT,end_time TEXT,shift_name TEXT DEFAULT 'Turno',FOREIGN KEY(operator_id) REFERENCES operators(id));
    CREATE TABLE vehicles(id INTEGER PRIMARY KEY,name TEXT,type TEXT,plate TEXT,capacity INTEGER DEFAULT 0,license_required TEXT,status TEXT DEFAULT 'Disponible',maintenance_due TEXT DEFAULT '');
    CREATE TABLE requirements(id INTEGER PRIMARY KEY,requester_id INTEGER,type TEXT,need_at TEXT,origin TEXT,destination TEXT,duration_min INTEGER,travel_min INTEGER,travel_distance_km REAL DEFAULT 0,travel_source TEXT DEFAULT '',description TEXT,dimensions TEXT DEFAULT '',weight_kg REAL DEFAULT 0,priority INTEGER,status TEXT DEFAULT 'Pendiente',created_at TEXT,operator_id INTEGER,vehicle_id INTEGER,assigned_start TEXT,assigned_end TEXT,controller_note TEXT DEFAULT '',rejection_reason TEXT DEFAULT '',rating INTEGER,feedback TEXT DEFAULT '',started_at TEXT,finished_at TEXT,incident TEXT DEFAULT '',FOREIGN KEY(requester_id) REFERENCES users(id),FOREIGN KEY(operator_id) REFERENCES operators(id),FOREIGN KEY(vehicle_id) REFERENCES vehicles(id));
    CREATE TABLE audit(id INTEGER PRIMARY KEY,requirement_id INTEGER,user_id INTEGER,action TEXT,detail TEXT,created_at TEXT,FOREIGN KEY(requirement_id) REFERENCES requirements(id),FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE destinations(id INTEGER PRIMARY KEY,name TEXT UNIQUE,address TEXT DEFAULT '',maps_url TEXT DEFAULT '',category TEXT DEFAULT 'Faena',active INTEGER DEFAULT 1);
    ''')
    c.execute('INSERT INTO users(name,email,password_hash,role,phone) VALUES(?,?,?,?,?)',(
        'Rodrigo Cortés','rcortes@dsg.cl',generate_password_hash('1234'),'Administrador',''
    ))
    c.commit(); c.close()

def ensure_schema():
    """Mantiene las tablas necesarias sin cargar registros ficticios."""
    c=sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS destinations(
        id INTEGER PRIMARY KEY,name TEXT UNIQUE,address TEXT DEFAULT '',
        maps_url TEXT DEFAULT '',category TEXT DEFAULT 'Faena',active INTEGER DEFAULT 1
    )""")
    columns={row[1] for row in c.execute('PRAGMA table_info(requirements)').fetchall()}
    if 'travel_distance_km' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN travel_distance_km REAL DEFAULT 0")
    if 'travel_source' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN travel_source TEXT DEFAULT ''")
    c.commit(); c.close()

def login_required(fn):
    @wraps(fn)
    def w(*a,**k): return fn(*a,**k) if session.get('user_id') else redirect(url_for('login'))
    return w

def current_user(): return db().execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone() if session.get('user_id') else None
def controller_only(): return current_user()['role']=='Administrador'
def audit(req_id,action,detail=''):
    db().execute('INSERT INTO audit(requirement_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)',(req_id,current_user()['id'],action,detail,datetime.now().isoformat(timespec='seconds')))
@app.context_processor
def globals_(): return {'current_user':current_user(),'PRIORITY_LABEL':PRIORITY_LABEL,'USER_ROLES':USER_ROLES,'PRIORITY_BY_ROLE':PRIORITY_BY_ROLE}

@app.route('/')
def index():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    u=current_user()
    if is_operator(u):
        return redirect(url_for('operator_view'))
    if is_requester(u):
        return redirect(url_for('new_requirement'))
    return redirect(url_for('dashboard'))
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        u=db().execute('SELECT * FROM users WHERE email=?',(request.form['email'].strip().lower(),)).fetchone()
        if u and check_password_hash(u['password_hash'],request.form['password']):
            session.clear(); session['user_id']=u['id']
            if is_operator(u): return redirect(url_for('operator_view'))
            if is_requester(u): return redirect(url_for('new_requirement'))
            return redirect(url_for('dashboard'))
        flash('Correo o contraseña incorrectos.','danger')
    return render_template('login.html')
@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    u=current_user()
    if is_operator(u): return redirect(url_for('operator_view'))
    if is_requester(u): return redirect(url_for('new_requirement'))
    where='' if controller_only() else ' WHERE requester_id=?'; params=() if controller_only() else (u['id'],); cond=' WHERE' if not where else where+' AND'
    stats={'total':db().execute('SELECT COUNT(*) FROM requirements'+where,params).fetchone()[0],'pending':db().execute("SELECT COUNT(*) FROM requirements"+cond+" status='Pendiente'",params).fetchone()[0],'assigned':db().execute("SELECT COUNT(*) FROM requirements"+cond+" status IN ('Asignado','En ejecución')",params).fetchone()[0],'concluded':db().execute("SELECT COUNT(*) FROM requirements"+cond+" status='Finalizado'",params).fetchone()[0],'scheduled_today':db().execute("SELECT COUNT(*) FROM requirements"+cond+" substr(COALESCE(assigned_start,need_at),1,10)=?",params+(datetime.now().date().isoformat(),)).fetchone()[0]}
    q='''SELECT r.*,u.name requester FROM requirements r JOIN users u ON u.id=r.requester_id'''+('' if controller_only() else ' WHERE r.requester_id=?')+' ORDER BY r.created_at DESC LIMIT 8'
    recent=db().execute(q,params).fetchall(); return render_template('dashboard.html',stats=stats,recent=recent)

@app.route('/nuevo',methods=['GET','POST'])
@login_required
def new_requirement():
    if is_operator(): return redirect(url_for('operator_view'))
    locations=db().execute('SELECT * FROM destinations WHERE active=1 ORDER BY category,name').fetchall()
    if request.method=='POST':
        typ=request.form['type']; dur=int(request.form['duration_min']); need=datetime.fromisoformat(request.form['need_at']); o=request.form['origin']; de=request.form['destination']
        valid_names={row['name'] for row in locations}
        if o not in valid_names or de not in valid_names:
            flash('El origen o destino seleccionado ya no está disponible. Actualice la página e inténtelo nuevamente.','danger')
            return render_template('new.html',types=REQUEST_TYPES,locations=locations)
        origin_row=next((row for row in locations if row['name']==o),None)
        destination_row=next((row for row in locations if row['name']==de),None)
        try:
            route=estimate_route(origin_row['address'] or origin_row['name'],destination_row['address'] or destination_row['name'])
        except RuntimeError as exc:
            flash(f'No se pudo calcular el tiempo de traslado: {exc}','danger')
            return render_template('new.html',types=REQUEST_TYPES,locations=locations)
        travel=route['minutes']
        if need-datetime.now()<timedelta(minutes=dur*3) and typ!='Urgencias':
            flash(f'La solicitud requiere al menos {dur*3} minutos de anticipación.','danger')
            return render_template('new.html',types=REQUEST_TYPES,locations=locations)
        p=5 if typ=='Urgencias' else PRIORITY_BY_ROLE.get(current_user()['role'],2)
        cur=db().execute('''INSERT INTO requirements(requester_id,type,need_at,origin,destination,duration_min,travel_min,travel_distance_km,travel_source,description,dimensions,weight_kg,priority,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(current_user()['id'],typ,need.isoformat(timespec='minutes'),o,de,dur,travel,route['distance_km'],route['source'],request.form['description'],request.form.get('dimensions',''),float(request.form.get('weight_kg') or 0),p,datetime.now().isoformat(timespec='seconds')))
        req_id=cur.lastrowid
        audit(req_id,'Creación','Requerimiento creado')
        db().commit()
        return redirect(url_for('requirement_created',req_id=req_id))
    return render_template('new.html',types=REQUEST_TYPES,locations=locations)

@app.route('/api/tiempo-viaje')
@login_required
def route_estimate_api():
    origin=request.args.get('origin','').strip(); destination=request.args.get('destination','').strip()
    if not origin or not destination:
        return {'ok':False,'error':'Seleccione origen y destino.'},400
    rows=db().execute('SELECT name,address FROM destinations WHERE active=1 AND name IN (?,?)',(origin,destination)).fetchall()
    by_name={row['name']:row for row in rows}
    if origin not in by_name or destination not in by_name:
        return {'ok':False,'error':'Una de las direcciones ya no está activa.'},400
    try:
        result=estimate_route(by_name[origin]['address'] or origin,by_name[destination]['address'] or destination)
        return {'ok':True,**result}
    except RuntimeError as exc:
        return {'ok':False,'error':str(exc)},503

@app.route('/requerimiento-creado/<int:req_id>')
@login_required
def requirement_created(req_id):
    req=db().execute('''SELECT r.*,u.name requester FROM requirements r JOIN users u ON u.id=r.requester_id WHERE r.id=?''',(req_id,)).fetchone()
    if not req or (not controller_only() and req['requester_id']!=current_user()['id']):
        flash('No fue posible encontrar el requerimiento creado.','danger')
        return redirect(url_for('new_requirement'))
    return render_template('requirement_created.html',req=req)

@app.route('/requerimientos')
@login_required
def requirements_panel():
    if not controller_only(): return redirect(url_for('my_requests'))
    rows=db().execute("""SELECT r.*,u.name requester,ou.name operator_name,v.name vehicle_name FROM requirements r JOIN users u ON u.id=r.requester_id LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users ou ON ou.id=o.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.status NOT IN ('Finalizado','Concluido') ORDER BY CASE r.status WHEN 'Pendiente' THEN 1 WHEN 'Asignado' THEN 2 WHEN 'En ejecución' THEN 3 WHEN 'Cancelado' THEN 4 WHEN 'Rechazado' THEN 5 ELSE 6 END, r.priority DESC, r.need_at""").fetchall()
    operators=db().execute('SELECT o.*,u.name FROM operators o JOIN users u ON u.id=o.user_id ORDER BY u.name').fetchall()
    vehicles=db().execute('SELECT * FROM vehicles ORDER BY type,name').fetchall()
    return render_template('requirements.html',rows=rows,operators=operators,vehicles=vehicles)

@app.route('/requerimientos/<int:req_id>/guardar',methods=['POST'])
@login_required
def save_requirement(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    req=db().execute('SELECT * FROM requirements WHERE id=?',(req_id,)).fetchone()
    if not req:
        flash('Requerimiento no encontrado.','danger')
        return redirect(url_for('requirements_panel'))

    priority=max(1,min(5,int(request.form.get('priority',req['priority']))))
    operator_id=int(request.form['operator_id']) if request.form.get('operator_id') else None
    vehicle_id=int(request.form['vehicle_id']) if request.form.get('vehicle_id') else None
    assigned_start=request.form.get('assigned_start') or req['assigned_start'] or req['need_at']
    controller_note=request.form.get('controller_note','').strip()
    requested_status=request.form.get('status',req['status'])

    allowed_status={'Pendiente','Asignado','En ejecución','Cancelado','Rechazado'}
    if requested_status not in allowed_status:
        requested_status=req['status']

    if requested_status in {'Asignado','En ejecución'} and (not operator_id or not vehicle_id):
        flash('Para asignar o iniciar el requerimiento debe seleccionar operador y vehículo.','danger')
        return redirect(url_for('requirements_panel'))

    duration=req['duration_min']+req['travel_min']
    start_dt=datetime.fromisoformat(assigned_start)
    assigned_end=(start_dt+timedelta(minutes=duration)).isoformat(timespec='minutes')

    if operator_id and vehicle_id and requested_status in {'Asignado','En ejecución'}:
        collision=db().execute("""SELECT 1 FROM requirements
            WHERE id<>? AND status IN ('Asignado','En ejecución')
            AND (operator_id=? OR vehicle_id=?)
            AND assigned_start<? AND assigned_end>? LIMIT 1""",
            (req_id,operator_id,vehicle_id,assigned_end,start_dt.isoformat(timespec='minutes'))).fetchone()
        if collision:
            flash('El operador o vehículo ya tiene una actividad en ese horario.','danger')
            return redirect(url_for('requirements_panel'))

    started_at=req['started_at']
    if requested_status=='En ejecución' and req['status']!='En ejecución':
        started_at=datetime.now().isoformat(timespec='seconds')

    db().execute("""UPDATE requirements SET priority=?,operator_id=?,vehicle_id=?,assigned_start=?,assigned_end=?,
        controller_note=?,status=?,started_at=? WHERE id=?""",
        (priority,operator_id,vehicle_id,assigned_start,assigned_end,controller_note,requested_status,started_at,req_id))
    audit(req_id,'Actualización integral',f'Estado: {requested_status}; operador: {operator_id}; vehículo: {vehicle_id}')
    db().commit()
    flash('Todos los cambios del requerimiento fueron guardados correctamente.','success')
    return redirect(url_for('requirements_panel'))

@app.route('/mis-solicitudes')
@login_required
def my_requests():
    if is_operator(): return redirect(url_for('operator_view'))
    rows=db().execute('''SELECT r.*,ou.name operator_name,v.name vehicle_name FROM requirements r LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users ou ON ou.id=o.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.requester_id=? ORDER BY r.created_at DESC''',(current_user()['id'],)).fetchall(); return render_template('my_requests.html',rows=rows)

@app.route('/prioridad/<int:req_id>',methods=['POST'])
@login_required
def update_priority(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    p=max(1,min(5,int(request.form['priority']))); db().execute('UPDATE requirements SET priority=? WHERE id=?',(p,req_id)); audit(req_id,'Cambio de prioridad',PRIORITY_LABEL[p]); db().commit(); flash('Prioridad actualizada.','success'); return redirect(url_for('requirements_panel'))
@app.route('/asignar/<int:req_id>',methods=['POST'])
@login_required
def manual_assign(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    r=db().execute('SELECT * FROM requirements WHERE id=?',(req_id,)).fetchone(); op=int(request.form['operator_id']); ve=int(request.form['vehicle_id']); start=datetime.fromisoformat(request.form['assigned_start']); end=start+timedelta(minutes=r['duration_min']+r['travel_min'])
    col=db().execute("SELECT 1 FROM requirements WHERE id<>? AND status IN ('Asignado','En ejecución') AND (operator_id=? OR vehicle_id=?) AND assigned_start<? AND assigned_end>?",(req_id,op,ve,end.isoformat(timespec='minutes'),start.isoformat(timespec='minutes'))).fetchone()
    if col: flash('El operador o vehículo ya está ocupado en ese horario.','danger'); return redirect(url_for('requirements_panel'))
    db().execute("UPDATE requirements SET status='Asignado',operator_id=?,vehicle_id=?,assigned_start=?,assigned_end=? WHERE id=?",(op,ve,start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes'),req_id)); audit(req_id,'Asignación',f'Operador {op}, vehículo {ve}'); db().commit(); flash('Requerimiento asignado.','success'); return redirect(url_for('requirements_panel'))
@app.route('/estado/<int:req_id>',methods=['POST'])
@login_required
def change_status(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    st=request.form['status']; db().execute('UPDATE requirements SET status=? WHERE id=?',(st,req_id)); audit(req_id,'Cambio de estado',st); db().commit(); flash('Estado actualizado.','success'); return redirect(url_for('requirements_panel'))
@app.route('/rechazar/<int:req_id>',methods=['POST'])
@login_required
def reject(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    reason=request.form.get('reason','Sin disponibilidad'); db().execute("UPDATE requirements SET status='Rechazado',rejection_reason=? WHERE id=?",(reason,req_id)); audit(req_id,'Rechazo',reason); db().commit(); flash('Solicitud rechazada.','success'); return redirect(url_for('requirements_panel'))
@app.route('/autoprogramar/<int:req_id>',methods=['POST'])
@login_required
def auto_schedule(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    r=db().execute('SELECT * FROM requirements WHERE id=?',(req_id,)).fetchone(); wanted='Camión' if ('camión' in r['type'].lower() or 'material' in r['type'].lower()) else ('Maquinaria' if 'maquinaria' in r['type'].lower() else 'Camioneta')
    for v in db().execute("SELECT * FROM vehicles WHERE type=? AND status='Disponible'",(wanted,)):
        for o in db().execute("SELECT * FROM operators WHERE status='Habilitado' AND instr(','||licenses||',',','||?||',')>0",(v['license_required'],)):
            start=datetime.fromisoformat(r['need_at']); end=start+timedelta(minutes=r['duration_min']+r['travel_min']); col=db().execute("SELECT 1 FROM requirements WHERE status IN ('Asignado','En ejecución') AND (operator_id=? OR vehicle_id=?) AND assigned_start<? AND assigned_end>?",(o['id'],v['id'],end.isoformat(timespec='minutes'),start.isoformat(timespec='minutes'))).fetchone()
            if not col:
                db().execute("UPDATE requirements SET status='Asignado',operator_id=?,vehicle_id=?,assigned_start=?,assigned_end=? WHERE id=?",(o['id'],v['id'],start.isoformat(timespec='minutes'),end.isoformat(timespec='minutes'),req_id)); audit(req_id,'Autoprogramación',v['name']); db().commit(); flash('Autoprogramación realizada.','success'); return redirect(url_for('requirements_panel'))
    flash('No existe combinación disponible.','danger'); return redirect(url_for('requirements_panel'))

@app.route('/operadores',methods=['GET','POST'])
@login_required
def operators_day():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        action=request.form.get('action','update')
        if action=='shift':
            db().execute('INSERT INTO operator_shifts(operator_id,shift_date,start_time,end_time,shift_name) VALUES(?,?,?,?,?)',(int(request.form['operator_id']),request.form['shift_date'],request.form['start_time'],request.form['end_time'],request.form.get('shift_name','Turno')))
            db().commit(); flash('Turno agregado al calendario.','success')
        else:
            oid=int(request.form['operator_id'])
            db().execute('UPDATE operators SET status=?,detail=?,licenses=?,base=?,accredited_sites=? WHERE id=?',(request.form['status'],request.form['detail'],request.form['licenses'],request.form['base'],request.form.get('accredited_sites',''),oid))
            db().commit(); flash('Operador actualizado.','success')
    date=request.args.get('date',datetime.now().date().isoformat())
    rows=db().execute('SELECT o.*,u.name,u.email,u.phone FROM operators o JOIN users u ON u.id=o.user_id ORDER BY u.name').fetchall()
    shifts=db().execute('''SELECT s.*,u.name operator_name,o.base,o.accredited_sites FROM operator_shifts s JOIN operators o ON o.id=s.operator_id JOIN users u ON u.id=o.user_id WHERE s.shift_date=? ORDER BY s.start_time,u.name''',(date,)).fetchall()
    locations=db().execute('SELECT name FROM destinations WHERE active=1 ORDER BY category,name').fetchall()
    return render_template('operators.html',rows=rows,shifts=shifts,date=date,locations=[x['name'] for x in locations])

@app.route('/usuarios',methods=['GET','POST'])
@login_required
def users_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        email=request.form['email'].strip().lower()
        role=request.form['role']
        if role not in USER_ROLES:
            flash('Tipo de usuario no válido.','danger')
            return redirect(url_for('users_view'))
        if db().execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():
            flash('Ya existe un usuario con ese correo.','danger')
        else:
            cur=db().execute('INSERT INTO users(name,email,password_hash,role,phone) VALUES(?,?,?,?,?)',(
                request.form['name'].strip(),email,generate_password_hash(request.form.get('password') or '1234'),role,request.form.get('phone','').strip()))
            if role=='Operador':
                db().execute('INSERT INTO operators(user_id,status,detail,licenses,base,accredited_sites,score) VALUES(?,?,?,?,?,?,?)',(
                    cur.lastrowid,'Habilitado',request.form.get('detail',''),request.form.get('licenses','B'),request.form.get('base','Antofagasta Centro'),request.form.get('accredited_sites',''),4.8))
            db().commit(); flash('Usuario creado correctamente.','success')
    rows=db().execute('''SELECT u.*,o.status operator_status,o.licenses,o.base,o.accredited_sites FROM users u LEFT JOIN operators o ON o.user_id=u.id ORDER BY u.role,u.name''').fetchall()
    locations=db().execute('SELECT name FROM destinations WHERE active=1 ORDER BY category,name').fetchall()
    return render_template('users.html',rows=rows,locations=[x['name'] for x in locations])

@app.route('/usuarios/<int:user_id>/editar',methods=['GET','POST'])
@login_required
def edit_user(user_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    user=db().execute("""SELECT u.*,o.id operator_id,o.status operator_status,o.detail,o.licenses,o.base,o.accredited_sites
                         FROM users u LEFT JOIN operators o ON o.user_id=u.id WHERE u.id=?""",(user_id,)).fetchone()
    if not user:
        flash('Usuario no encontrado.','danger'); return redirect(url_for('users_view'))
    if request.method=='POST':
        email=request.form['email'].strip().lower(); role=request.form['role']
        if role not in USER_ROLES:
            flash('Tipo de usuario no válido.','danger'); return redirect(url_for('edit_user',user_id=user_id))
        duplicate=db().execute('SELECT id FROM users WHERE email=? AND id<>?',(email,user_id)).fetchone()
        if duplicate:
            flash('El correo ya está registrado por otro usuario.','danger')
        else:
            db().execute('UPDATE users SET name=?,email=?,role=?,phone=? WHERE id=?',(request.form['name'].strip(),email,role,request.form.get('phone','').strip(),user_id))
            if request.form.get('password','').strip():
                db().execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(request.form['password']),user_id))
            op=db().execute('SELECT id FROM operators WHERE user_id=?',(user_id,)).fetchone()
            if role=='Operador':
                if op:
                    db().execute('UPDATE operators SET status=?,detail=?,licenses=?,base=?,accredited_sites=? WHERE user_id=?',(request.form.get('operator_status','Habilitado'),request.form.get('detail',''),request.form.get('licenses','B'),request.form.get('base','Antofagasta Centro'),request.form.get('accredited_sites',''),user_id))
                else:
                    db().execute('INSERT INTO operators(user_id,status,detail,licenses,base,accredited_sites,score) VALUES(?,?,?,?,?,?,?)',(user_id,request.form.get('operator_status','Habilitado'),request.form.get('detail',''),request.form.get('licenses','B'),request.form.get('base','Antofagasta Centro'),request.form.get('accredited_sites',''),4.8))
            elif op:
                linked=db().execute('SELECT 1 FROM requirements WHERE operator_id=? LIMIT 1',(op['id'],)).fetchone()
                if linked:
                    flash('Se actualizaron los datos, pero el perfil se mantiene como operador porque tiene requerimientos asociados.','danger')
                    db().execute("UPDATE users SET role='Operador' WHERE id=?",(user_id,))
                else:
                    db().execute('DELETE FROM operator_shifts WHERE operator_id=?',(op['id'],)); db().execute('DELETE FROM operators WHERE id=?',(op['id'],))
            db().commit(); flash('Usuario actualizado correctamente.','success'); return redirect(url_for('users_view'))
    locations=db().execute('SELECT name FROM destinations WHERE active=1 ORDER BY category,name').fetchall()
    return render_template('user_edit.html',user=user,locations=[x['name'] for x in locations])

@app.route('/usuarios/<int:user_id>/eliminar',methods=['POST'])
@login_required
def delete_user(user_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    if user_id==current_user()['id']:
        flash('No puede eliminar su propia cuenta mientras tiene la sesión iniciada.','danger'); return redirect(url_for('users_view'))
    op=db().execute('SELECT id FROM operators WHERE user_id=?',(user_id,)).fetchone()
    has_requests=db().execute('SELECT 1 FROM requirements WHERE requester_id=? OR operator_id=? LIMIT 1',(user_id,op['id'] if op else -1)).fetchone()
    has_audit=db().execute('SELECT 1 FROM audit WHERE user_id=? LIMIT 1',(user_id,)).fetchone()
    if has_requests or has_audit:
        flash('No se puede eliminar el usuario porque posee registros asociados. Puede editar sus datos o cambiar su rol.','danger')
    else:
        if op:
            db().execute('DELETE FROM operator_shifts WHERE operator_id=?',(op['id'],)); db().execute('DELETE FROM operators WHERE id=?',(op['id'],))
        db().execute('DELETE FROM users WHERE id=?',(user_id,)); db().commit(); flash('Usuario eliminado correctamente.','success')
    return redirect(url_for('users_view'))

@app.route('/destinos',methods=['GET','POST'])
@login_required
def destinations_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        did=request.form.get('destination_id'); name=request.form['name'].strip()
        values=(name,request.form.get('address','').strip(),request.form.get('maps_url','').strip(),request.form.get('category','Faena'),1 if request.form.get('active')=='1' else 0)
        try:
            if did:
                db().execute('UPDATE destinations SET name=?,address=?,maps_url=?,category=?,active=? WHERE id=?',values+(int(did),))
            else:
                db().execute('INSERT INTO destinations(name,address,maps_url,category,active) VALUES(?,?,?,?,?)',values)
            db().commit(); flash('Destino guardado correctamente.','success')
        except sqlite3.IntegrityError:
            flash('Ya existe un destino con ese nombre.','danger')
    rows=db().execute('SELECT * FROM destinations ORDER BY category,name').fetchall()
    return render_template('destinations.html',rows=rows)

@app.route('/destinos/<int:destination_id>/eliminar',methods=['POST'])
@login_required
def delete_destination(destination_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    destination=db().execute('SELECT * FROM destinations WHERE id=?',(destination_id,)).fetchone()
    if not destination:
        flash('Destino no encontrado.','danger')
    elif db().execute('SELECT 1 FROM requirements WHERE origin=? OR destination=? LIMIT 1',(destination['name'],destination['name'])).fetchone():
        db().execute('UPDATE destinations SET active=0 WHERE id=?',(destination_id,)); db().commit(); flash('El destino tiene historial y fue desactivado.','success')
    else:
        db().execute('DELETE FROM destinations WHERE id=?',(destination_id,)); db().commit(); flash('Destino eliminado.','success')
    return redirect(url_for('destinations_view'))

@app.route('/vehiculos',methods=['GET','POST'])
@login_required
def vehicles_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        if request.form.get('vehicle_id'):
            db().execute('UPDATE vehicles SET status=?,maintenance_due=? WHERE id=?',(request.form['status'],request.form.get('maintenance_due',''),int(request.form['vehicle_id'])))
        else:
            db().execute('INSERT INTO vehicles(name,type,plate,capacity,license_required,status,maintenance_due) VALUES(?,?,?,?,?,?,?)',(request.form['name'],request.form['type'],request.form['plate'],int(request.form.get('capacity') or 0),request.form['license_required'],request.form['status'],request.form.get('maintenance_due','')))
        db().commit(); flash('Vehículo guardado.','success')
    rows=db().execute('SELECT * FROM vehicles ORDER BY type,name').fetchall(); return render_template('vehicles.html',rows=rows)
@app.route('/calendario')
@login_required
def calendar_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    month=request.args.get('month',datetime.now().strftime('%Y-%m'))
    rows=db().execute('''SELECT r.*,u.name requester,ou.name operator_name,v.name vehicle_name,
        substr(COALESCE(r.finished_at,r.assigned_end,r.need_at),1,10) final_date
        FROM requirements r JOIN users u ON u.id=r.requester_id
        LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users ou ON ou.id=o.user_id
        LEFT JOIN vehicles v ON v.id=r.vehicle_id
        WHERE r.status IN ('Finalizado','Concluido')
        AND substr(COALESCE(r.finished_at,r.assigned_end,r.need_at),1,7)=?
        ORDER BY final_date DESC,COALESCE(r.finished_at,r.assigned_end,r.need_at) DESC''',(month,)).fetchall()
    grouped={}
    for row in rows:
        grouped.setdefault(row['final_date'],[]).append(row)
    return render_template('calendar.html',grouped=grouped,month=month)

@app.route('/reportes')
@login_required
def reports():
    if not controller_only(): return redirect(url_for('dashboard'))
    by_type=db().execute('SELECT type,COUNT(*) qty FROM requirements GROUP BY type ORDER BY qty DESC').fetchall(); by_status=db().execute('SELECT status,COUNT(*) qty FROM requirements GROUP BY status ORDER BY qty DESC').fetchall(); operators=db().execute("SELECT u.name,COUNT(r.id) jobs,ROUND(AVG(r.rating),1) rating FROM operators o JOIN users u ON u.id=o.user_id LEFT JOIN requirements r ON r.operator_id=o.id GROUP BY o.id ORDER BY jobs DESC").fetchall(); by_site=db().execute("SELECT destination site,COUNT(*) qty FROM requirements WHERE destination IN ('SQM','Albemarle','Sierra Gorda') GROUP BY destination ORDER BY qty DESC").fetchall(); top_site=by_site[0] if by_site else None; return render_template('reports.html',by_type=by_type,by_status=by_status,operators=operators,by_site=by_site,top_site=top_site)
@app.route('/auditoria')
@login_required
def audit_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    rows=db().execute('''SELECT a.*,u.name user_name,r.type FROM audit a JOIN users u ON u.id=a.user_id JOIN requirements r ON r.id=a.requirement_id ORDER BY a.created_at DESC LIMIT 100''').fetchall(); return render_template('audit.html',rows=rows)

@app.route('/operador')
@login_required
def operator_view():
    op=db().execute('SELECT * FROM operators WHERE user_id=?',(current_user()['id'],)).fetchone()
    if not op: return redirect(url_for('dashboard'))
    rows=db().execute('''SELECT r.*,u.name requester,u.phone requester_phone,v.name vehicle_name,d.maps_url destination_maps_url,d.address destination_address FROM requirements r JOIN users u ON u.id=r.requester_id LEFT JOIN vehicles v ON v.id=r.vehicle_id LEFT JOIN destinations d ON d.name=r.destination WHERE r.operator_id=? AND r.status IN ('Asignado','En ejecución') ORDER BY r.assigned_start''',(op['id'],)).fetchall(); return render_template('operator.html',rows=rows)
@app.route('/iniciar/<int:req_id>',methods=['POST'])
@login_required
def start_job(req_id):
    u=current_user()
    if u['role']=='Administrador':
        result=db().execute("UPDATE requirements SET status='En ejecución',started_at=? WHERE id=? AND operator_id IS NOT NULL AND status='Asignado'",(datetime.now().isoformat(timespec='seconds'),req_id))
        destination='requirements_panel'
    elif u['role']=='Operador':
        op=db().execute('SELECT id FROM operators WHERE user_id=?',(u['id'],)).fetchone()
        result=db().execute("UPDATE requirements SET status='En ejecución',started_at=? WHERE id=? AND operator_id=? AND status='Asignado'",(datetime.now().isoformat(timespec='seconds'),req_id,op['id'] if op else -1))
        destination='operator_view'
    else:
        return redirect(url_for('new_requirement'))
    if result.rowcount:
        audit(req_id,'Inicio de actividad'); db().commit(); flash('Actividad iniciada correctamente.','success')
    else:
        db().rollback(); flash('Solo el operador asignado o un administrador puede iniciar esta actividad.','danger')
    return redirect(url_for(destination))

@app.route('/concluir/<int:req_id>',methods=['POST'])
@login_required
def conclude(req_id):
    u=current_user(); incident=request.form.get('incident','').strip()
    if u['role']=='Administrador':
        result=db().execute("UPDATE requirements SET status='Finalizado',finished_at=?,incident=? WHERE id=? AND status='En ejecución'",(datetime.now().isoformat(timespec='seconds'),incident,req_id))
        destination='requirements_panel'
    elif u['role']=='Operador':
        op=db().execute('SELECT id FROM operators WHERE user_id=?',(u['id'],)).fetchone()
        result=db().execute("UPDATE requirements SET status='Finalizado',finished_at=?,incident=? WHERE id=? AND operator_id=? AND status='En ejecución'",(datetime.now().isoformat(timespec='seconds'),incident,req_id,op['id'] if op else -1))
        destination='operator_view'
    else:
        flash('Solo el operador asignado o un administrador puede finalizar la actividad.','danger')
        return redirect(url_for('new_requirement'))
    if result.rowcount:
        audit(req_id,'Finalización',incident); db().commit(); flash('Actividad finalizada. El requerimiento fue enviado al calendario.','success')
    else:
        db().rollback(); flash('La actividad debe estar en ejecución y usted debe tener autorización para finalizarla.','danger')
    return redirect(url_for(destination))
@app.route('/evaluar/<int:req_id>',methods=['POST'])
@login_required
def rate(req_id):
    db().execute('UPDATE requirements SET rating=?,feedback=? WHERE id=? AND requester_id=?',(max(1,min(5,int(request.form['rating']))),request.form.get('feedback',''),req_id,current_user()['id'])); db().commit(); flash('Evaluación guardada.','success'); return redirect(url_for('my_requests'))
if __name__=='__main__':
    if not DB_PATH.exists(): init_db()
    ensure_schema()
    app.run(host='0.0.0.0',port=5000,debug=True)
