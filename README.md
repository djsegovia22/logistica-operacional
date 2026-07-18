# DESIGN · Logística Operacional v9

## Novedades

- Tiempo estimado de viaje calculado por internet mediante OpenRouteService.
- Origen y destino se consultan desde las direcciones activas del módulo Destinos.
- Jerarquía automática de prioridad según el perfil del solicitante.
- El operador visualiza la prioridad de cada actividad.
- El operador asignado y el administrador pueden finalizar actividades en ejecución.
- Las actividades finalizadas pasan automáticamente al Calendario.

## Perfiles y prioridad automática

- Administrador: Máxima.
- Gerencia/Dirección: Alta.
- Jefatura de área: Alta.
- Supervisor/Coordinador: Media.
- Usuario general: Normal.
- Operador: ejecuta requerimientos asignados y visualiza su prioridad.
- Las solicitudes de tipo Urgencias se registran con prioridad Máxima.

## Configurar OpenRouteService

1. Cree una cuenta gratuita en https://openrouteservice.org/dev/#/signup (solo pide correo, sin tarjeta de crédito).
2. Genere un token en su panel ("Request a token" / "Dashboard").
3. Copie `.env.example` como `.env`.
4. Pegue la clave en `ORS_API_KEY`.

Ejemplo:

```env
ORS_API_KEY=su_clave_real
SECRET_KEY=una_clave_privada
```

La aplicación lee automáticamente el archivo `.env`. Al guardar un destino con dirección, la app geocodifica automáticamente esa dirección (la convierte a coordenadas) y la guarda para no tener que repetir esa consulta en cada requerimiento. Sin una clave válida, el requerimiento se registra igual, pero queda en estado "Revisión y confirme" porque no se pudo confirmar el tiempo de viaje.

## Instalación

```bash
pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000`.

Cuenta inicial:

- Correo: `rcortes@dsg.cl`
- Contraseña: `1234`

Cambie la contraseña al comenzar a utilizar el sistema.
