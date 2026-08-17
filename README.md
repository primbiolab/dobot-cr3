# CR3_Remote_Lab - Plataforma de investigación y teleoperación remota para el robot colaborativo Dobot CR3.

Interfaz web para la teleoperación del **robot colaborativo Dobot CR3** en
`https://dobot-cr3.primbiolab.org`. Incorpora autenticación mediante Google, gestión de roles por proyecto, y el modelo de concurrencia basado en la asignación exclusiva del
control.

**Un operador controla, todos los demás observan — en tiempo real.** Cualquier
número de usuarios autenticados puede acceder al laboratorio simultáneamente.
En cada momento, exactamente un usuario posee el control del robot; los demás
usuarios pueden visualizar el mismo video, la misma telemetría, la misma pose
3D, quién está operando el robot y un registro en tiempo real de los comandos
ejecutados por dicho operador. Consulta
[docs/concurrency.md](docs/concurrency.md).

```text
 Navegador ──────────────► cr3-remote-lab.primbiolab.org    Interfaz de usuario
   │                       (Cloudflare Worker, siempre disponible,
   │                        con o sin conexión al robot)
   │
   ├── /api/control/*  ─► control de sesión, cola, presencia,
   │                       parada de emergencia y actividad
   │
   └── WebSocket + WebRTC ─► cr3-remote-lab-control.primbiolab.org
                                    (Cloudflare Tunnel)
                                    │
                         ┌──────────┴───────────┐
                    gatekeeper :8766        go2rtc :1984
                    (verifica el JWT de       (distribución de
                     Supabase + token de       video mediante WebRTC
                     control y distribuye      a los usuarios)
                     la actividad)
                         │
                  foxglove_bridge :8765 ──► ROS 2 · Dobot CR3
```

Cross-repo contract: [PLATFORM-GUIDE.md](PLATFORM-GUIDE.md) — la
versión oficial se encuentra en el repositorio central; no edites esta copia
localmente.

## Stack

Next.js (App Router) · TypeScript · Tailwind CSS v4 · Supabase (proyecto
compartido de la plataforma) · Redis (gestión de la asignación de control) · Cloudflare Tunnel →
foxglove_bridge (ROS 2) + go2rtc (WebRTC video) en el
computador del laboratorio.

## Desarrollo

```bash
git submodule update --init       # Espacio de trabajo ROS 2; también proporciona el modelo 3D
npm install
npm run dev        # http://localhost:3000
npm run typecheck  # tsc --noEmit — debe completarse sin errores antes de realizar commits
npm run lint
npm run build       # debe ejecutarse correctamente con y sin .env.local

npm run test:lease    # Asignación de control: un operador, N usuarios y recuperación ante fallos
npm run test:gateway   # Autorización en el extremo y ejecución de programas (requiere pytest)
```

La aplicación puede utilizarse sin variables de entorno configuradas y sin
hardware conectado: muestra un indicador de modo sin conexión, permite la
visualización únicamente y utiliza un controlador simulado. El modelo 3D se
genera a partir del submódulo y se copia en `public/robot/` durante cada
compilación mediante (`scripts/sync-robot-assets.mjs`); Si el submódulo no está
disponible, este paso se omite y la pestaña 3D informa que el modelo no está
disponible.

## Docs

- [docs/concurrency.md](docs/concurrency.md) — asignación de control, cola,
  parada de emergencia, presencia de usuarios y visualización de las acciones
  realizadas por el operador.
- [docs/hardware.md](docs/hardware.md) — arquitectura de ROS 2, mapa de
  servicios, protocolo del gatekeeper y modo sin conexión.
- [docs/bench-setup.md](docs/bench-setup.md) — configuración de un computador
  de trabajo en sustitución de la Raspberry Pi, identificación del robot en
  la red y diferencias entre ambas configuraciones.
- [docs/deploy-pi.md](docs/deploy-pi.md) — configuración del túnel,
  go2rtc, foxglove_bridge, gatekeeper y servicios systemd de supervisión
  (heartbeat) en el computador del laboratorio.
- [docs/ros2-node.md](docs/ros2-node.md) — modelo de ejecución y gatekeeper
  WebSocket con verificación mediante JWT.
- [edge/README.md](edge/README.md) — componentes que se ejecutan en el
  computador del laboratorio y justificación de la ubicación del gatekeeper
  como capa de protección de los servicios.
- [DEPLOY.md](DEPLOY.md) — los dos nombres de dominio y el procedimiento para
  desplegar cada componente.

---

## 📜 Créditos y Contexto

Desarrollado en la Universidad Nacional de Colombia, Sede La Paz, en el marco de las actividades académicas de sus autores.

---
## Afiliación institucional

Escuela de Pregrado, Dirección Académica, Vicerrectoría de Sede, Universidad Nacional de Colombia, Sede La Paz, Cesar, Colombia.
