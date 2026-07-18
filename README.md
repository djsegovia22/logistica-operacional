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
