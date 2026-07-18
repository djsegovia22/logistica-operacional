from __future__ import annotations
import os, sqlite3, json, calendar, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, date
from functools import wraps
from pathlib import Path
from flask import Flask, flash, g, redirect, render_template, request, session, url_for, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR=Path(__file__).resolve().parent; DB_PATH=BASE_DIR/'design.db'; UPLOAD_DIR=BASE_DIR/'uploads'

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
app.config['MAX_CONTENT_LENGTH']=16*1024*1024
PRIORITY_LABEL={5:'Máxima',4:'Alta',3:'Media',2:'Normal',1:'Baja'}
USER_ROLES=['Administrador','Gerencia/Dirección','Jefatura de área','Supervisor/Coordinador','Usuario general','Operador']
PRIORITY_BY_ROLE={'Administrador':5,'Gerencia/Dirección':4,'Jefatura de área':4,'Supervisor/Coordinador':3,'Usuario general':2,'Solicitante':2}
REQUEST_TYPES=[
    {'label':'Traslado de pasajeros','icon':'🚌','hint':'Traslado en camioneta o van'},
    {'label':'Retiro o despacho camión','icon':'🚛','hint':'Traslado de material pesado'},
    {'label':'Retiro o compra de material','icon':'🛒','hint':'Indicar OC'},
    {'label':'Traslado de personal','icon':'👷','hint':'Subida de personal a faena'},
    {'label':'Solicitud de camioneta','icon':'🛻','hint':'Camioneta para faena o caso excepcional'},
    {'label':'Solicitud de camión o maquinaria','icon':'🚚','hint':'Equipo solicitado en faena'},
    {'label':'Traslado de maquinaria','icon':'🚜','hint':'Equipo solicitado en faena'},
    {'label':'Trámite administrativo','icon':'🖨','hint':'Movilización y gestión de trámite'},
    {'label':'Otra gestión operacional','icon':'🪄','hint':'Todo tipo de servicio'},
    {'label':'Urgencia','icon':'🚨','hint':'Atención inmediata'},
]
STATUS_CLASS={'Revisión y confirme':'status-review','Pendiente':'status-pending','Asignado':'status-assigned','En ejecución':'status-running','Finalizado':'status-done','Concluido':'status-done','Cancelado':'status-cancelled','Rechazado':'status-cancelled','Disponible':'status-available','Descanso':'status-resting','Vacaciones':'status-vacation','Licencia':'status-leave','Exámenes':'status-exam','En uso':'status-assigned','Mantención':'status-running','Fuera de servicio':'status-resting'}
VEHICLE_EXCEPTION_TYPES=['Mantención','Fuera de servicio']
VEHICLE_TYPES=['Camioneta','Camión','Bus','Maquinaria']
TYPE_ICON={t['label']:t['icon'] for t in REQUEST_TYPES}
EXCEPTION_TYPES=['Vacaciones','Licencia','Exámenes']
SHIFT_PATTERNS={'7x7':(7,7),'14x14':(14,14)}
ORS_GEOCODE_URL='https://api.openrouteservice.org/geocode/search'
ORS_DIRECTIONS_URL='https://api.openrouteservice.org/v2/directions/driving-car'

def is_operator(user=None):
    user=user or current_user()
    return bool(user and user['role']=='Operador')

def is_requester(user=None):
    user=user or current_user()
    return bool(user and user['role'] not in ('Administrador','Operador'))

def geocode_address(address):
    """Convierte una dirección de texto en (lat,lng) usando el geocodificador de OpenRouteService."""
    api_key=os.environ.get('ORS_API_KEY','').strip()
    address=(address or '').strip()
    if not api_key or not address: return None
    params=urllib.parse.urlencode({'api_key':api_key,'text':address,'size':1,'boundary.country':'CL'})
    try:
        with urllib.request.urlopen(f'{ORS_GEOCODE_URL}?{params}',timeout=10) as response:
            data=json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError,TimeoutError):
        return None
    features=data.get('features') or []
    if not features: return None
    lng,lat=features[0]['geometry']['coordinates']
    return (lat,lng)

def estimate_route(origin, destination):
    """Obtiene duración y distancia usando OpenRouteService (Directions API).

    origin/destination son filas de la tabla destinations (con name/address/lat/lng).
    Requiere ORS_API_KEY en las variables de entorno.
    """
    if origin['name'].strip()==destination['name'].strip():
        return {'minutes':0,'distance_km':0.0,'source':'Mismo lugar'}
    api_key=os.environ.get('ORS_API_KEY','').strip()
    if not api_key:
        raise RuntimeError('Falta configurar ORS_API_KEY.')

    def coords_for(place):
        if place['lat'] is not None and place['lng'] is not None:
            return (place['lat'],place['lng'])
        found=geocode_address(place['address'] or place['name'])
        if not found:
            raise RuntimeError(f"No se pudo ubicar la dirección de \"{place['name']}\".")
        db().execute('UPDATE destinations SET lat=?,lng=? WHERE id=?',(found[0],found[1],place['id'])); db().commit()
        return found

    o_lat,o_lng=coords_for(origin)
    d_lat,d_lng=coords_for(destination)
    payload={'coordinates':[[o_lng,o_lat],[d_lng,d_lat]]}
    req=urllib.request.Request(
        ORS_DIRECTIONS_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization':api_key,'Content-Type':'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req,timeout=12) as response:
            data=json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode('utf-8',errors='ignore')
        raise RuntimeError(f'OpenRouteService respondió {exc.code}: {detail[:180]}') from exc
    except (urllib.error.URLError,TimeoutError) as exc:
        raise RuntimeError('No fue posible conectarse al servicio de rutas.') from exc
    routes=data.get('routes') or []
    if not routes:
        raise RuntimeError('No se encontró una ruta entre las direcciones seleccionadas.')
    summary=routes[0]['summary']
    return {
        'minutes':max(1,round(summary['duration']/60)),
        'distance_km':round(summary['distance']/1000,1),
        'source':'OpenRouteService'
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
    CREATE TABLE operators(id INTEGER PRIMARY KEY,user_id INTEGER UNIQUE,status TEXT DEFAULT 'Habilitado',detail TEXT DEFAULT '',licenses TEXT DEFAULT 'B',base TEXT DEFAULT '',accredited_sites TEXT DEFAULT '',score REAL DEFAULT 4.8,shift_pattern TEXT DEFAULT '7x7',shift_cycle_start TEXT DEFAULT '',FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE operator_shifts(id INTEGER PRIMARY KEY,operator_id INTEGER,shift_date TEXT,start_time TEXT,end_time TEXT,shift_name TEXT DEFAULT '',type TEXT DEFAULT 'Vacaciones',FOREIGN KEY(operator_id) REFERENCES operators(id));
    CREATE TABLE vehicles(id INTEGER PRIMARY KEY,name TEXT,type TEXT,plate TEXT,capacity INTEGER DEFAULT 0,license_required TEXT,status TEXT DEFAULT 'Disponible',maintenance_due TEXT DEFAULT '',brand_model TEXT DEFAULT '',year TEXT DEFAULT '',base TEXT DEFAULT '',accredited_sites TEXT DEFAULT '');
    CREATE TABLE vehicle_blocks(id INTEGER PRIMARY KEY,vehicle_id INTEGER,block_date TEXT,start_time TEXT,end_time TEXT,type TEXT DEFAULT 'Mantención',label TEXT DEFAULT '',FOREIGN KEY(vehicle_id) REFERENCES vehicles(id));
    CREATE TABLE requirements(id INTEGER PRIMARY KEY,requester_id INTEGER,type TEXT,need_at TEXT,origin TEXT,destination TEXT,duration_min INTEGER,travel_min INTEGER,travel_distance_km REAL DEFAULT 0,travel_source TEXT DEFAULT '',description TEXT,dimensions TEXT DEFAULT '',weight_kg REAL DEFAULT 0,priority INTEGER,status TEXT DEFAULT 'Pendiente',created_at TEXT,operator_id INTEGER,vehicle_id INTEGER,assigned_start TEXT,assigned_end TEXT,controller_note TEXT DEFAULT '',rejection_reason TEXT DEFAULT '',rating INTEGER,feedback TEXT DEFAULT '',started_at TEXT,finished_at TEXT,incident TEXT DEFAULT '',document_path TEXT DEFAULT '',document_name TEXT DEFAULT '',FOREIGN KEY(requester_id) REFERENCES users(id),FOREIGN KEY(operator_id) REFERENCES operators(id),FOREIGN KEY(vehicle_id) REFERENCES vehicles(id));
    CREATE TABLE audit(id INTEGER PRIMARY KEY,requirement_id INTEGER,user_id INTEGER,action TEXT,detail TEXT,created_at TEXT,FOREIGN KEY(requirement_id) REFERENCES requirements(id),FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE destinations(id INTEGER PRIMARY KEY,name TEXT UNIQUE,address TEXT DEFAULT '',maps_url TEXT DEFAULT '',category TEXT DEFAULT 'Faena',active INTEGER DEFAULT 1,lat REAL,lng REAL);
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
    destination_columns={row[1] for row in c.execute('PRAGMA table_info(destinations)').fetchall()}
    if 'lat' not in destination_columns:
        c.execute("ALTER TABLE destinations ADD COLUMN lat REAL")
    if 'lng' not in destination_columns:
        c.execute("ALTER TABLE destinations ADD COLUMN lng REAL")
    columns={row[1] for row in c.execute('PRAGMA table_info(requirements)').fetchall()}
    if 'travel_distance_km' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN travel_distance_km REAL DEFAULT 0")
    if 'travel_source' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN travel_source TEXT DEFAULT ''")
    if 'document_path' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN document_path TEXT DEFAULT ''")
    if 'document_name' not in columns:
        c.execute("ALTER TABLE requirements ADD COLUMN document_name TEXT DEFAULT ''")
    operator_columns={row[1] for row in c.execute('PRAGMA table_info(operators)').fetchall()}
    if 'shift_pattern' not in operator_columns:
        c.execute("ALTER TABLE operators ADD COLUMN shift_pattern TEXT DEFAULT '7x7'")
    if 'shift_cycle_start' not in operator_columns:
        c.execute("ALTER TABLE operators ADD COLUMN shift_cycle_start TEXT DEFAULT ''")
    shift_columns={row[1] for row in c.execute('PRAGMA table_info(operator_shifts)').fetchall()}
    if 'type' not in shift_columns:
        c.execute("ALTER TABLE operator_shifts ADD COLUMN type TEXT DEFAULT 'Vacaciones'")
    vehicle_columns={row[1] for row in c.execute('PRAGMA table_info(vehicles)').fetchall()}
    for col,ddl in (('brand_model',"ALTER TABLE vehicles ADD COLUMN brand_model TEXT DEFAULT ''"),
                    ('year',"ALTER TABLE vehicles ADD COLUMN year TEXT DEFAULT ''"),
                    ('base',"ALTER TABLE vehicles ADD COLUMN base TEXT DEFAULT ''"),
                    ('accredited_sites',"ALTER TABLE vehicles ADD COLUMN accredited_sites TEXT DEFAULT ''")):
        if col not in vehicle_columns: c.execute(ddl)
    c.execute("""CREATE TABLE IF NOT EXISTS vehicle_blocks(
        id INTEGER PRIMARY KEY,vehicle_id INTEGER,block_date TEXT,start_time TEXT,end_time TEXT,
        type TEXT DEFAULT 'Mantención',label TEXT DEFAULT '',FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
    )""")
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
def globals_(): return {'current_user':current_user(),'PRIORITY_LABEL':PRIORITY_LABEL,'USER_ROLES':USER_ROLES,'PRIORITY_BY_ROLE':PRIORITY_BY_ROLE,'STATUS_CLASS':STATUS_CLASS,'TYPE_ICON':TYPE_ICON}

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
        typ=request.form.get('type','').strip()
        try: dur=max(10,int(request.form.get('duration_min') or 60))
        except ValueError: dur=60
        try: need=datetime.fromisoformat(request.form['need_at'])
        except (KeyError,ValueError): need=datetime.now()+timedelta(days=1)
        o=request.form.get('origin','').strip(); de=request.form.get('destination','').strip()
        by_name={row['name']:row for row in locations}

        review_reason=''
        travel=0; distance=0.0; travel_source=''
        if o not in by_name or de not in by_name:
            review_reason='El origen o destino seleccionado ya no está disponible; debe validarse manualmente.'
        else:
            origin_row=by_name[o]; destination_row=by_name[de]
            try:
                route=estimate_route(origin_row,destination_row)
                travel=route['minutes']; distance=route['distance_km']; travel_source=route['source']
            except RuntimeError as exc:
                review_reason=f'No se pudo calcular el tiempo de traslado: {exc}'
        if not review_reason and need-datetime.now()<timedelta(minutes=dur*3) and typ!='Urgencia':
            review_reason=f'La solicitud fue registrada con menos de {dur*3} minutos de anticipación.'

        p=5 if typ=='Urgencia' else PRIORITY_BY_ROLE.get(current_user()['role'],2)
        status='Revisión y confirme' if review_reason else 'Pendiente'
        cur=db().execute('''INSERT INTO requirements(requester_id,type,need_at,origin,destination,duration_min,travel_min,travel_distance_km,travel_source,description,dimensions,weight_kg,priority,status,created_at,controller_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(current_user()['id'],typ,need.isoformat(timespec='minutes'),o,de,dur,travel,distance,travel_source,request.form.get('description',''),request.form.get('dimensions',''),float(request.form.get('weight_kg') or 0),p,status,datetime.now().isoformat(timespec='seconds'),review_reason))
        req_id=cur.lastrowid
        audit(req_id,'Creación','Requerimiento creado en revisión: '+review_reason if review_reason else 'Requerimiento creado')
        db().commit()
        if review_reason:
            flash('El requerimiento quedó registrado, pero requiere revisión y confirmación del administrador: '+review_reason,'danger')
        return redirect(url_for('requirement_created',req_id=req_id))
    return render_template('new.html',types=REQUEST_TYPES,locations=locations)

@app.route('/api/tiempo-viaje')
@login_required
def route_estimate_api():
    origin=request.args.get('origin','').strip(); destination=request.args.get('destination','').strip()
    if not origin or not destination:
        return {'ok':False,'error':'Seleccione origen y destino.'},400
    rows=db().execute('SELECT * FROM destinations WHERE active=1 AND name IN (?,?)',(origin,destination)).fetchall()
    by_name={row['name']:row for row in rows}
    if origin not in by_name or destination not in by_name:
        return {'ok':False,'error':'Una de las direcciones ya no está activa.'},400
    try:
        result=estimate_route(by_name[origin],by_name[destination])
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
    rows=db().execute("""SELECT r.*,u.name requester,ou.name operator_name,v.name vehicle_name FROM requirements r JOIN users u ON u.id=r.requester_id LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users ou ON ou.id=o.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.status NOT IN ('Finalizado','Concluido') AND (r.status NOT IN ('Cancelado','Rechazado') OR date(r.created_at)>=date('now','-1 month')) ORDER BY CASE r.status WHEN 'Revisión y confirme' THEN 0 WHEN 'Pendiente' THEN 1 WHEN 'Asignado' THEN 2 WHEN 'En ejecución' THEN 3 WHEN 'Cancelado' THEN 4 WHEN 'Rechazado' THEN 5 ELSE 6 END, r.priority DESC, r.need_at""").fetchall()
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

    if operator_id and vehicle_id and requested_status=='Pendiente':
        requested_status='Asignado'

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

@app.route('/requerimientos/<int:req_id>/documento',methods=['POST'])
@login_required
def upload_document(req_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    req=db().execute('SELECT * FROM requirements WHERE id=?',(req_id,)).fetchone()
    if not req:
        flash('Requerimiento no encontrado.','danger'); return redirect(url_for('requirements_panel'))
    file=request.files.get('document')
    if not file or not file.filename:
        flash('Seleccione un archivo para cargar.','danger'); return redirect(url_for('requirements_panel'))
    UPLOAD_DIR.mkdir(exist_ok=True)
    original_name=file.filename
    stored_name=f"req{req_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(original_name)}"
    file.save(UPLOAD_DIR/stored_name)
    db().execute('UPDATE requirements SET document_path=?,document_name=? WHERE id=?',(stored_name,original_name,req_id))
    audit(req_id,'Documento adjunto',original_name)
    db().commit(); flash('Documento cargado correctamente.','success')
    return redirect(url_for('requirements_panel'))

@app.route('/documentos/<int:req_id>')
@login_required
def view_document(req_id):
    req=db().execute('SELECT * FROM requirements WHERE id=?',(req_id,)).fetchone()
    if not req or not req['document_path']:
        flash('Documento no encontrado.','danger'); return redirect(url_for('dashboard'))
    u=current_user()
    op=db().execute('SELECT id FROM operators WHERE user_id=?',(u['id'],)).fetchone()
    allowed=controller_only() or req['requester_id']==u['id'] or (op and req['operator_id']==op['id'])
    if not allowed:
        flash('No tiene autorización para ver este documento.','danger'); return redirect(url_for('dashboard'))
    return send_from_directory(UPLOAD_DIR,req['document_path'],download_name=req['document_name'])

@app.route('/requerimientos/devolver-pendiente',methods=['POST'])
@login_required
def revert_to_pending():
    if not controller_only(): return redirect(url_for('dashboard'))
    req_id=request.form.get('req_id',type=int)
    next_url=request.form.get('next') or url_for('requirements_panel')
    if not req_id:
        flash('Seleccione un requerimiento para devolver a pendiente.','danger')
        return redirect(next_url)
    db().execute("""UPDATE requirements SET status='Pendiente',operator_id=NULL,vehicle_id=NULL,
        assigned_start=NULL,assigned_end=NULL,started_at=NULL,finished_at=NULL,incident='' WHERE id=?""",(req_id,))
    audit(req_id,'Devuelto a pendiente','Se quitó operador, vehículo y horario asignado')
    db().commit(); flash('Requerimiento devuelto a Pendiente.','success')
    return redirect(next_url)

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

MONTHS_ES=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre']
WEEKDAYS_SHORT_ES=['Lun','Mar','Mié','Jue','Vie','Sáb','Dom']
WEEKDAYS_LONG_ES=['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
ESTADO_OPTIONS=['Disponible','Asignado','En ejecución','Descanso','Vacaciones','Licencia','Exámenes']
AVATAR_COLORS=['#ffb511','#1976d2','#2e9d50','#6f42c1','#c2185b','#ef8b17','#0c8599']

def build_period(view,anchor):
    if view=='dia':
        dates=[anchor]
        d=dates[0]; label=f'{WEEKDAYS_LONG_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month-1]} de {d.year}'
    elif view=='mes':
        first=anchor.replace(day=1)
        days_in_month=calendar.monthrange(first.year,first.month)[1]
        dates=[first+timedelta(days=i) for i in range(days_in_month)]
        label=f'{MONTHS_ES[first.month-1].capitalize()} {first.year}'
    else:
        view='semana'
        dates=[anchor+timedelta(days=i) for i in range(7)]
        a,b=dates[0],dates[-1]
        if a.month==b.month: label=f'{a.day} – {b.day} {MONTHS_ES[b.month-1].capitalize()} {b.year}'
        else: label=f'{a.day} {MONTHS_ES[a.month-1][:3].capitalize()} – {b.day} {MONTHS_ES[b.month-1][:3].capitalize()} {b.year}'
    return view,dates,label

def pattern_is_on(shift_pattern,cycle_start_iso,d):
    """True/False si el día es de turno u descanso según el ciclo; None si no hay ciclo configurado."""
    if not cycle_start_iso: return None
    on,off=SHIFT_PATTERNS.get(shift_pattern,(7,7))
    idx=(d-date.fromisoformat(cycle_start_iso)).days % (on+off)
    return idx<on

@app.route('/operadores',methods=['GET','POST'])
@login_required
def operators_day():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        action=request.form.get('action','update')
        if action=='exception':
            exc_type=request.form.get('type','Vacaciones')
            if exc_type not in EXCEPTION_TYPES: exc_type='Vacaciones'
            label=request.form.get('shift_name','').strip() or exc_type
            oid=int(request.form['operator_id'])
            try:
                start=date.fromisoformat(request.form['start_date']); end=date.fromisoformat(request.form['end_date'])
            except ValueError:
                flash('Fechas de excepción inválidas.','danger')
                return redirect(url_for('operators_day'))
            if end<start: start,end=end,start
            n=0; d=start
            while d<=end and n<366:
                db().execute('INSERT INTO operator_shifts(operator_id,shift_date,start_time,end_time,shift_name,type) VALUES(?,?,?,?,?,?)',
                    (oid,d.isoformat(),'00:00','23:59',label,exc_type))
                d+=timedelta(days=1); n+=1
            db().commit(); flash(f'{exc_type} registrada del {start.isoformat()} al {end.isoformat()}.','success')
        else:
            oid=int(request.form['operator_id'])
            pattern=request.form.get('shift_pattern','7x7')
            if pattern not in SHIFT_PATTERNS: pattern='7x7'
            db().execute('UPDATE operators SET status=?,detail=?,licenses=?,base=?,accredited_sites=?,shift_pattern=?,shift_cycle_start=? WHERE id=?',(
                request.form['status'],request.form['detail'],request.form['licenses'],request.form['base'],
                request.form.get('accredited_sites',''),pattern,request.form.get('shift_cycle_start',''),oid))
            db().commit(); flash('Operador actualizado.','success')
        return redirect(url_for('operators_day',view=request.form.get('view','semana'),anchor=request.form.get('anchor',''),
            operator=request.form.get('operator_filter',''),estado=request.form.get('estado_filter','')))

    view=request.args.get('view','semana')
    if view not in ('dia','semana','mes'): view='semana'
    try: anchor=date.fromisoformat(request.args.get('anchor') or '')
    except ValueError: anchor=datetime.now().date()
    view,dates,period_label=build_period(view,anchor)
    period_start,period_end=dates[0].isoformat(),dates[-1].isoformat()

    all_rows=db().execute('SELECT o.*,u.name,u.email,u.phone FROM operators o JOIN users u ON u.id=o.user_id ORDER BY u.name').fetchall()
    exceptions=db().execute('SELECT * FROM operator_shifts WHERE shift_date BETWEEN ? AND ?',(period_start,period_end)).fetchall()
    reqs=db().execute("""SELECT * FROM requirements WHERE operator_id IS NOT NULL
        AND substr(COALESCE(assigned_start,need_at),1,10) BETWEEN ? AND ?""",(period_start,period_end)).fetchall()

    exception_blocks={}
    for s in exceptions:
        exception_blocks.setdefault(s['operator_id'],{})[s['shift_date']]={'start':s['start_time'],'end':s['end_time'],'label':s['shift_name'] or s['type'],'status':s['type'],'status_class':STATUS_CLASS.get(s['type'],'')}
    req_blocks={}
    for r in reqs:
        source=r['assigned_start'] or r['need_at']
        day=source[:10]
        req_blocks.setdefault(r['operator_id'],{}).setdefault(day,[]).append(
            {'start':source[11:16],'end':(r['assigned_end'] or r['need_at'])[11:16],'label':r['type'],'status':r['status'],'status_class':STATUS_CLASS.get(r['status'],'')})
    for op_id in req_blocks:
        for day in req_blocks[op_id]:
            req_blocks[op_id][day].sort(key=lambda b:b['start'])

    AVAILABLE_BLOCK={'start':'','end':'','label':'Disponible','status':'Disponible','status_class':STATUS_CLASS['Disponible']}
    RESTING_BLOCK={'start':'','end':'','label':'Descanso','status':'Descanso','status_class':STATUS_CLASS['Descanso']}

    def day_blocks_for(o,d):
        iso=d.isoformat()
        blocks=[]
        exc=exception_blocks.get(o['id'],{}).get(iso)
        if exc: blocks.append(exc)
        today_reqs=req_blocks.get(o['id'],{}).get(iso)
        if today_reqs: blocks.extend(today_reqs)
        if blocks:
            return sorted(blocks,key=lambda b:b['start'] or '')
        is_on=pattern_is_on(o['shift_pattern'],o['shift_cycle_start'],d)
        if is_on is False: return [RESTING_BLOCK]
        return [AVAILABLE_BLOCK]

    today=datetime.now().date(); today_iso=today.isoformat()
    operators=[]
    for i,o in enumerate(all_rows):
        today_blocks=day_blocks_for(o,today)
        current_status,current_sub='Disponible','Sin actividades'
        for b in today_blocks:
            if b['status'] in ('Descanso','Vacaciones','Licencia','Exámenes'):
                current_status,current_sub=b['status'],b['label']; break
        else:
            running=next((b for b in today_blocks if b['status']=='En ejecución'),None)
            assigned=next((b for b in today_blocks if b['status']=='Asignado'),None)
            if running: current_status,current_sub='En ejecución',running['label']
            elif assigned: current_status,current_sub='Asignado',assigned['label']
        sites=[x.strip() for x in (o['accredited_sites'] or '').split(',') if x.strip()]
        operators.append({
            'id':o['id'],'name':o['name'],'email':o['email'],'phone':o['phone'],'status':o['status'],'detail':o['detail'],
            'licenses':o['licenses'],'base':o['base'],'accredited_sites':o['accredited_sites'],
            'shift_pattern':o['shift_pattern'],'shift_cycle_start':o['shift_cycle_start'],
            'sites_shown':sites[:2],'sites_extra':max(0,len(sites)-2),
            'avatar_letter':(o['name'] or '?').strip()[:1].upper(),'avatar_color':AVATAR_COLORS[i%len(AVATAR_COLORS)],
            'current_status':current_status,'current_sub':current_sub,
            'current_status_class':STATUS_CLASS.get(current_status,''),
            'days':{d.isoformat():day_blocks_for(o,d) for d in dates},
        })

    operator_filter=request.args.get('operator',type=int)
    estado_filter=request.args.get('estado','')
    visible=[o for o in operators if (not operator_filter or o['id']==operator_filter) and (not estado_filter or o['current_status']==estado_filter)]

    if view=='mes':
        prev_anchor=(dates[0]-timedelta(days=1)).isoformat(); next_anchor=(dates[-1]+timedelta(days=1)).isoformat()
    else:
        step=1 if view=='dia' else 7
        prev_anchor=(anchor-timedelta(days=step)).isoformat(); next_anchor=(anchor+timedelta(days=step)).isoformat()
    period_dates=[{'iso':d.isoformat(),'day_num':d.day,'weekday_short':WEEKDAYS_SHORT_ES[d.weekday()],'is_today':d.isoformat()==today_iso} for d in dates]

    return render_template('operators.html',
        operators=visible,all_operators=operators,view=view,anchor=anchor.isoformat(),period_label=period_label,
        period_dates=period_dates,operator_filter=operator_filter,estado_filter=estado_filter,estado_options=ESTADO_OPTIONS,
        exception_types=EXCEPTION_TYPES,shift_patterns=list(SHIFT_PATTERNS),
        prev_anchor=prev_anchor,next_anchor=next_anchor)

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
        address=request.form.get('address','').strip()
        coords=None
        manual=request.form.get('coordinates','').strip()
        if manual and ',' in manual:
            try:
                lat_txt,lng_txt=manual.split(',',1)
                coords=(float(lat_txt.strip()),float(lng_txt.strip()))
            except ValueError:
                coords=None
        if not coords and address:
            coords=geocode_address(address)
        values=(name,address,request.form.get('maps_url','').strip(),request.form.get('category','Faena'),1 if request.form.get('active')=='1' else 0,
            coords[0] if coords else None,coords[1] if coords else None)
        try:
            if did:
                db().execute('UPDATE destinations SET name=?,address=?,maps_url=?,category=?,active=?,lat=?,lng=? WHERE id=?',values+(int(did),))
            else:
                db().execute('INSERT INTO destinations(name,address,maps_url,category,active,lat,lng) VALUES(?,?,?,?,?,?,?)',values)
            db().commit()
            if (address or manual) and not coords:
                flash('Destino guardado, pero no se pudieron obtener coordenadas válidas. Verifique la dirección o ingrese coordenadas exactas manualmente.','danger')
            else:
                flash('Destino guardado correctamente.','success')
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

def time_to_minutes(hhmm):
    if not hhmm: return None
    h,m=hhmm.split(':'); return int(h)*60+int(m)

def vehicle_day_blocks(v,d,exception_blocks,req_blocks):
    iso=d.isoformat()
    blocks=[]
    excs=exception_blocks.get(v['id'],{}).get(iso)
    if excs: blocks.extend(excs)
    todays=req_blocks.get(v['id'],{}).get(iso)
    if todays: blocks.extend(todays)
    if blocks: return sorted(blocks,key=lambda b:b['start'] or '')
    if v['status'] in ('Mantención','Fuera de servicio'):
        return [{'start':'','end':'','status':v['status'],'status_class':STATUS_CLASS.get(v['status'],''),'operator_name':'','type':v['status'],'destination':'','req_id':None}]
    return [{'start':'','end':'','status':'Disponible','status_class':STATUS_CLASS['Disponible'],'operator_name':'','type':'Disponible','destination':'','req_id':None}]

@app.route('/vehiculos',methods=['GET','POST'])
@login_required
def vehicles_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    if request.method=='POST':
        action=request.form.get('action','update')
        if action=='create':
            db().execute('''INSERT INTO vehicles(name,type,plate,capacity,license_required,status,maintenance_due,brand_model,year,base,accredited_sites)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(
                request.form['name'],request.form['type'],request.form['plate'],int(request.form.get('capacity') or 0),
                request.form['license_required'],request.form['status'],request.form.get('maintenance_due',''),
                request.form.get('brand_model',''),request.form.get('year',''),request.form.get('base',''),request.form.get('accredited_sites','')))
            db().commit(); flash('Vehículo agregado.','success')
        elif action=='exception':
            vid=int(request.form['vehicle_id']); exc_type=request.form.get('type','Mantención')
            if exc_type not in VEHICLE_EXCEPTION_TYPES: exc_type='Mantención'
            label=request.form.get('label','').strip() or exc_type
            try:
                start=date.fromisoformat(request.form['start_date']); end=date.fromisoformat(request.form['end_date'])
            except ValueError:
                flash('Fechas inválidas.','danger'); return redirect(url_for('vehicles_view'))
            if end<start: start,end=end,start
            d=start; n=0
            while d<=end and n<366:
                db().execute('INSERT INTO vehicle_blocks(vehicle_id,block_date,start_time,end_time,type,label) VALUES(?,?,?,?,?,?)',
                    (vid,d.isoformat(),request.form.get('start_time','00:00'),request.form.get('end_time','23:59'),exc_type,label))
                d+=timedelta(days=1); n+=1
            db().commit(); flash(f'{exc_type} registrada del {start.isoformat()} al {end.isoformat()}.','success')
        else:
            vid=int(request.form['vehicle_id'])
            db().execute('''UPDATE vehicles SET name=?,type=?,plate=?,capacity=?,license_required=?,status=?,maintenance_due=?,
                brand_model=?,year=?,base=?,accredited_sites=? WHERE id=?''',(
                request.form['name'],request.form['type'],request.form['plate'],int(request.form.get('capacity') or 0),
                request.form['license_required'],request.form['status'],request.form.get('maintenance_due',''),
                request.form.get('brand_model',''),request.form.get('year',''),request.form.get('base',''),request.form.get('accredited_sites',''),vid))
            db().commit(); flash('Vehículo actualizado.','success')
        return redirect(url_for('vehicles_view',view=request.form.get('view','dia'),anchor=request.form.get('anchor','')))

    view=request.args.get('view','dia')
    if view not in ('dia','semana','mes'): view='dia'
    try: anchor=date.fromisoformat(request.args.get('anchor') or '')
    except ValueError: anchor=datetime.now().date()
    view,dates,period_label=build_period(view,anchor)
    period_start,period_end=dates[0].isoformat(),dates[-1].isoformat()

    all_rows=db().execute('SELECT * FROM vehicles ORDER BY type,name').fetchall()
    exceptions=db().execute('SELECT * FROM vehicle_blocks WHERE block_date BETWEEN ? AND ?',(period_start,period_end)).fetchall()
    reqs=db().execute("""SELECT r.*,u2.name operator_name FROM requirements r
        LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users u2 ON u2.id=o.user_id
        WHERE r.vehicle_id IS NOT NULL AND substr(COALESCE(r.assigned_start,r.need_at),1,10) BETWEEN ? AND ?""",(period_start,period_end)).fetchall()

    exception_blocks={}
    for s in exceptions:
        exception_blocks.setdefault(s['vehicle_id'],{}).setdefault(s['block_date'],[]).append(
            {'start':s['start_time'],'end':s['end_time'],'status':s['type'],'status_class':STATUS_CLASS.get(s['type'],''),
             'operator_name':'','type':s['label'] or s['type'],'destination':'','req_id':None})
    req_blocks={}
    for r in reqs:
        source=r['assigned_start'] or r['need_at']
        day=source[:10]
        label_status='En uso' if r['status'] in ('Asignado','En ejecución') else r['status']
        req_blocks.setdefault(r['vehicle_id'],{}).setdefault(day,[]).append({
            'start':source[11:16],'end':(r['assigned_end'] or r['need_at'])[11:16],
            'status':label_status,'status_class':STATUS_CLASS.get(label_status,''),
            'operator_name':r['operator_name'] or 'Sin operador','type':r['type'],'destination':r['destination'],'req_id':r['id']})
    for vid in req_blocks:
        for day in req_blocks[vid]:
            req_blocks[vid][day].sort(key=lambda b:b['start'])

    today_iso=datetime.now().date().isoformat()
    faena_filter=request.args.get('faena','')
    type_filter=request.args.get('tipo','')
    estado_filter=request.args.get('estado','')

    vehicles=[]
    for v in all_rows:
        today_blocks=vehicle_day_blocks(v,datetime.now().date(),exception_blocks,req_blocks)
        current=today_blocks[0]
        sites=[x.strip() for x in (v['accredited_sites'] or '').split(',') if x.strip()]
        day_map={d.isoformat():vehicle_day_blocks(v,d,exception_blocks,req_blocks) for d in dates}
        faenas_today={b['destination'] for blocks in day_map.values() for b in blocks if b['destination']}
        vehicles.append({
            'id':v['id'],'name':v['name'],'type':v['type'],'plate':v['plate'],'capacity':v['capacity'],
            'license_required':v['license_required'],'status':v['status'],'maintenance_due':v['maintenance_due'],
            'brand_model':v['brand_model'],'year':v['year'],'base':v['base'],'accredited_sites':v['accredited_sites'],
            'sites_shown':sites[:3],'sites_extra':max(0,len(sites)-3),
            'current_status':current['status'],'current_status_class':current['status_class'],
            'days':day_map,'faenas_today':faenas_today,
        })

    visible=[v for v in vehicles if (not faena_filter or faena_filter in v['faenas_today'])
             and (not type_filter or v['type']==type_filter) and (not estado_filter or v['current_status']==estado_filter)]

    window_start,window_end=7*60,19*60
    if view=='dia':
        all_today=[b for v in visible for b in v['days'].get(dates[0].isoformat(),[])]
        starts=[time_to_minutes(b['start']) for b in all_today if b['start']]
        ends=[time_to_minutes(b['end']) for b in all_today if b['end']]
        if starts: window_start=min(window_start,min(starts)-(min(starts)%60))
        if ends:
            we=max(window_end,max(ends))
            window_end=we if we%60==0 else we+(60-we%60)
        for v in visible:
            blocks=v['days'].get(dates[0].isoformat(),[])
            for b in blocks:
                s=time_to_minutes(b['start']); e=time_to_minutes(b['end'])
                if s is None or e is None or s==e: s,e=window_start,window_end
                b['slot_start']=max(1,round((s-window_start)/30)+1)
                b['slot_end']=max(b['slot_start']+1,round((e-window_start)/30)+1)
    hour_marks=[]
    h=window_start
    while h<=window_end:
        hour_marks.append({'label':f'{h//60:02d}:00','slot':round((h-window_start)/30)+1})
        h+=60
    total_slots=round((window_end-window_start)/30)

    destinations=db().execute('SELECT name FROM destinations WHERE active=1 ORDER BY category,name').fetchall()

    selected_vehicle_id=request.args.get('selected_vehicle_id',type=int) or (visible[0]['id'] if visible else (vehicles[0]['id'] if vehicles else None))
    selected_vehicle=next((v for v in vehicles if v['id']==selected_vehicle_id),None)
    selected_req_id=request.args.get('selected_req_id',type=int)
    selected_block=None
    if selected_vehicle:
        if selected_req_id:
            selected_block=db().execute("""SELECT r.*,u2.name operator_name FROM requirements r
                LEFT JOIN operators o ON o.id=r.operator_id LEFT JOIN users u2 ON u2.id=o.user_id WHERE r.id=?""",(selected_req_id,)).fetchone()
        else:
            today_blocks=selected_vehicle['days'].get(today_iso) or vehicle_day_blocks(
                next(vv for vv in all_rows if vv['id']==selected_vehicle_id),datetime.now().date(),exception_blocks,req_blocks)
            first=today_blocks[0] if today_blocks else None
            if first and first['req_id']: selected_req_id=first['req_id']

    if view=='mes':
        prev_anchor=(dates[0]-timedelta(days=1)).isoformat(); next_anchor=(dates[-1]+timedelta(days=1)).isoformat()
    else:
        step=1 if view=='dia' else 7
        prev_anchor=(anchor-timedelta(days=step)).isoformat(); next_anchor=(anchor+timedelta(days=step)).isoformat()
    period_dates=[{'iso':d.isoformat(),'day_num':d.day,'weekday_short':WEEKDAYS_SHORT_ES[d.weekday()],'is_today':d.isoformat()==today_iso} for d in dates]

    return render_template('vehicles.html',
        vehicles=visible,all_vehicles=vehicles,view=view,anchor=anchor.isoformat(),period_label=period_label,
        period_dates=period_dates,faena_filter=faena_filter,type_filter=type_filter,estado_filter=estado_filter,
        destinations=[x['name'] for x in destinations],vehicle_types=VEHICLE_TYPES,
        estado_options=['En uso','Disponible','Mantención','Fuera de servicio'],exception_types=VEHICLE_EXCEPTION_TYPES,
        hour_marks=hour_marks,total_slots=total_slots,single_date=dates[0].isoformat() if view=='dia' else None,
        selected_vehicle=selected_vehicle,selected_req_id=selected_req_id,selected_block=selected_block,
        prev_anchor=prev_anchor,next_anchor=next_anchor)

@app.route('/vehiculos/<int:vehicle_id>/eliminar',methods=['POST'])
@login_required
def delete_vehicle(vehicle_id):
    if not controller_only(): return redirect(url_for('dashboard'))
    vehicle=db().execute('SELECT * FROM vehicles WHERE id=?',(vehicle_id,)).fetchone()
    if not vehicle:
        flash('Vehículo no encontrado.','danger')
    elif db().execute('SELECT 1 FROM requirements WHERE vehicle_id=? LIMIT 1',(vehicle_id,)).fetchone():
        db().execute("UPDATE vehicles SET status='Fuera de servicio' WHERE id=?",(vehicle_id,))
        db().commit(); flash('El vehículo tiene historial de uso; en lugar de eliminarse fue marcado como Fuera de servicio.','success')
    else:
        db().execute('DELETE FROM vehicle_blocks WHERE vehicle_id=?',(vehicle_id,))
        db().execute('DELETE FROM vehicles WHERE id=?',(vehicle_id,)); db().commit(); flash('Vehículo eliminado.','success')
    return redirect(url_for('vehicles_view'))

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
    by_type=db().execute('SELECT type,COUNT(*) qty FROM requirements GROUP BY type ORDER BY qty DESC').fetchall()
    by_status=db().execute('SELECT status,COUNT(*) qty FROM requirements GROUP BY status ORDER BY qty DESC').fetchall()
    operators=db().execute("SELECT u.name,COUNT(r.id) jobs,ROUND(AVG(r.rating),1) rating FROM operators o JOIN users u ON u.id=o.user_id LEFT JOIN requirements r ON r.operator_id=o.id GROUP BY o.id ORDER BY jobs DESC").fetchall()
    by_site=db().execute('''SELECT site,SUM(qty) qty FROM (
        SELECT origin site,COUNT(*) qty FROM requirements GROUP BY origin
        UNION ALL
        SELECT destination site,COUNT(*) qty FROM requirements GROUP BY destination
    ) GROUP BY site ORDER BY qty DESC''').fetchall()
    top_route=db().execute("SELECT origin,destination,COUNT(*) qty FROM requirements GROUP BY origin,destination ORDER BY qty DESC LIMIT 1").fetchone()
    top_destination=db().execute("SELECT destination site,COUNT(*) qty FROM requirements GROUP BY destination ORDER BY qty DESC LIMIT 1").fetchone()
    by_user=db().execute("SELECT u.name,COUNT(r.id) qty FROM users u JOIN requirements r ON r.requester_id=u.id GROUP BY u.id ORDER BY qty DESC").fetchall()
    return render_template('reports.html',by_type=by_type,by_status=by_status,operators=operators,by_site=by_site,top_route=top_route,top_destination=top_destination,by_user=by_user)
@app.route('/auditoria')
@login_required
def audit_view():
    if not controller_only(): return redirect(url_for('dashboard'))
    rows=db().execute('''SELECT a.*,u.name user_name,r.type FROM audit a JOIN users u ON u.id=a.user_id JOIN requirements r ON r.id=a.requirement_id ORDER BY a.created_at DESC LIMIT 100''').fetchall(); return render_template('audit.html',rows=rows)

AGENDA_QUERY='''SELECT r.*,u.name requester,u.phone requester_phone,v.name vehicle_name,d.maps_url destination_maps_url,d.address destination_address FROM requirements r JOIN users u ON u.id=r.requester_id LEFT JOIN vehicles v ON v.id=r.vehicle_id LEFT JOIN destinations d ON d.name=r.destination WHERE r.operator_id=? AND r.status IN ('Asignado','En ejecución') ORDER BY r.assigned_start'''

@app.route('/operador')
@login_required
def operator_view():
    u=current_user()
    if controller_only():
        operators=db().execute('SELECT o.*,u2.name name FROM operators o JOIN users u2 ON u2.id=o.user_id ORDER BY u2.name').fetchall()
        selected_id=request.args.get('operator_id',type=int)
        selected=next((o for o in operators if o['id']==selected_id),None)
        rows=db().execute(AGENDA_QUERY,(selected['id'],)).fetchall() if selected else []
        return render_template('operator.html',rows=rows,operators=operators,selected_operator=selected)
    op=db().execute('SELECT * FROM operators WHERE user_id=?',(u['id'],)).fetchone()
    if not op: return redirect(url_for('dashboard'))
    rows=db().execute(AGENDA_QUERY,(op['id'],)).fetchall()
    return render_template('operator.html',rows=rows,operators=None,selected_operator=None)
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
