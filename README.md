# DESIGN · Logística Operacional v9

## Novedades

- Tiempo estimado de viaje calculado por internet mediante Google Routes API, considerando tráfico.
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

## Configurar Google Routes API

1. Cree un proyecto en Google Cloud.
2. Active **Routes API** y la facturación de Google Maps Platform.
3. Cree una API key y restríngala para usar Routes API.
4. Copie `.env.example` como `.env`.
5. Pegue la clave en `GOOGLE_MAPS_API_KEY`.

Ejemplo:

```env
GOOGLE_MAPS_API_KEY=su_clave_real
SECRET_KEY=una_clave_privada
```

La aplicación lee automáticamente el archivo `.env`. Sin una clave válida, no permitirá crear el requerimiento porque no podrá confirmar el tiempo de viaje.

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
