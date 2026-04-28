# Bugs conocidos — WarpSocket

Catálogo vivo de bugs detectados durante el desarrollo, con su estado, causa raíz, fix aplicado y notas para no repetirlos.

Leyenda de estado:

- ✅ Resuelto
- 🟡 Resuelto parcialmente / pendiente verificar
- 🔴 Abierto

---

## Resueltos

### ✅ B-001 — `uninstall` no existía y faltaban métodos en la ABC de plataforma
**Síntomas:** No había forma limpia de revertir `setup`. La interfaz `ServerPlatform` tampoco exponía la operación.
**Causa raíz:** Comando y métodos no implementados.
**Fix:** Añadido `_cmd_uninstall` con confirmación + `uninstall_wg_config()` y `uninstall_wstunnel_service()` en la ABC y en `LinuxServerPlatform`. (commit `5376bed`)
**Prevención:** Cuando se añade un `setup`/`install`, planificar el inverso desde el principio y cubrirlo con tests.

### ✅ B-002 — Wizard del servidor moría con `EOFError` al lanzarse vía `curl | bash`
**Síntomas:** Al usar el one-liner, el primer `input()` del wizard recibía EOF y abortaba.
**Causa raíz:** `stdin` del proceso era el contenido del pipe, no el TTY del usuario.
**Fix:** El instalador lanza el wizard con `</dev/tty` para reabrir entrada interactiva. (commit `802e80e`)
**Prevención:** Cualquier wizard interactivo invocado desde un instalador `curl|bash` debe redirigir explícitamente a `/dev/tty`.

### ✅ B-003 — `ModuleNotFoundError` ejecutando `warpsocket-server` como usuario no-root
**Síntomas:** Tras `sudo install.sh`, el binario fallaba al importarse para usuarios distintos de root.
**Causa raíz:** Instalación editable (`pip install -e`) dejaba el código en `/root/WarpSocket` (perms 700), inaccesible para otros usuarios. El symlink en `/usr/local/bin` apuntaba a un venv que cargaba `sys.path` desde esa ruta.
**Fix:** El instalador usa `pip install` sin `-e` (copia los paquetes al venv). (commit `3977d75`)
**Prevención:** Nunca usar instalación editable para deploys de producción cuando el repo de origen vive en un home con permisos restrictivos.

### ✅ B-004 — Cliente Windows: `route add` requería elevación, fallaba silencioso
**Síntomas:** El cliente arrancaba sin admin, no podía añadir la ruta de bypass al endpoint y la conexión moría.
**Causa raíz:** `route add` en Windows necesita admin. La app no se auto-elevaba.
**Fix:** `_ensure_elevated()` en `app.py` que detecta falta de admin y relanza vía `ShellExecuteW("runas", ...)`. (commit `2364894`)
**Prevención:** Toda operación que toque la tabla de rutas o servicios del SO debe verificarse al arranque de la app y forzar elevación si falta.

### ✅ B-005 — Servidor: clientes WG conectados pero sin internet
**Síntomas:** El handshake WireGuard funcionaba, los pings al servidor también, pero el cliente no salía a internet.
**Causa raíz:** Faltaba `net.ipv4.ip_forward=1` persistente y reglas iptables NAT MASQUERADE / FORWARD.
**Fix:** `build_server_wg_conf` añade `PostUp`/`PostDown` con sysctl + iptables, y el wizard escribe `/etc/sysctl.d/99-warpsocket.conf` para persistencia. (commit `8285ec7`)
**Prevención:** Para cualquier servidor WireGuard que actúe de gateway, verificar siempre los tres ingredientes: ip_forward, FORWARD ACCEPT y NAT POSTROUTING.

### ✅ B-006 — Visor de logs roto en el cliente (thread-safety tkinter/pystray)
**Síntomas:** Click en "Ver logs" desde el menú del tray congelaba la app o no abría nada.
**Causa raíz:** pystray ejecuta callbacks en su propio thread; tkinter sólo admite operaciones desde el thread principal.
**Fix:** Patrón cola + polling — los callbacks del tray hacen `ui_queue.put(...)` y un `root.after(50, _pump_ui_queue)` consume desde el main thread. Tray usa `run_detached()` para no bloquear el loop principal. (commits `e664a8f`, `f016b71`)
**Prevención:** Cualquier integración tray ↔ tkinter debe pasar por una cola; nunca llamar widgets directamente desde callbacks de pystray.

### ✅ B-007 — `add-client` escribía `wg0.conf` en el path equivocado
**Síntomas:** Tras `add-client`, el peer no aparecía en WireGuard hasta editar a mano.
**Causa raíz:** El comando escribía la conf en `/etc/warpsocket/` en vez de delegar en `platform.install_wg_config()`, que sabe que la ruta correcta es `/etc/wireguard/wg0.conf`.
**Fix:** `add-client` y `revoke-client` ahora llaman a `platform.install_wg_config()`. (commit `f016b71`)
**Prevención:** Nunca duplicar paths del SO en `cli.py`; toda escritura de configuración del sistema pasa por el módulo `platforms/`.

### ✅ B-008 — `list-clients` no mostraba estado en vivo
**Síntomas:** Sólo listaba nombre/IP del fichero de config, sin saber quién está realmente conectado.
**Causa raíz:** Faltaba parsear `wg show wg0 dump`.
**Fix:** Añadido `LivePeer` + `get_live_peers()` y nuevas columnas (online/offline/idle, último handshake, RX/TX). (commit `8da9432`)
**Prevención:** Para cualquier comando "list" sobre un servicio runtime, separar siempre estado declarado (config) de estado real (runtime).

### ✅ B-009 — Instalador Linux fallaba con `ensurepip is not available`
**Síntomas:** En sistemas con Python pero sin el paquete `pythonX.Y-venv`, `python -m venv` rompía.
**Causa raíz:** Las distros Debian/Ubuntu separan `venv` en su propio paquete y no lo incluyen con la build mínima de Python.
**Fix:** `ensure_python_venv()` valida `python -m venv --help` y, si falla, instala `pythonX.Y-venv` vía apt. (commit `4369aa0`)
**Prevención:** Detectar capacidades reales (probar el comando) en vez de asumir que `python` instalado implica venv funcional.

### ✅ B-010 — Comandos privilegiados crasheaban sin `sudo`
**Síntomas:** `add-client`/`revoke-client`/etc. lanzaban tracebacks crípticos al intentar escribir en `/etc/`.
**Causa raíz:** No se validaba EUID antes de ejecutar comandos que requieren root.
**Fix:** `_require_root()` + `_PRIVILEGED_COMMANDS` frozenset. Mensaje claro pidiendo `sudo`. (commit `658c623`)
**Prevención:** Cada comando que toca `/etc/` o servicios del SO debe declarar explícitamente que necesita root.

### ✅ B-011 — `uninstall` dejaba huérfanos (venv, symlink, sysctl drop-in)
**Síntomas:** Tras `uninstall`, `/opt/warpsocket-server`, `/usr/local/bin/warpsocket-server` y `/etc/sysctl.d/99-warpsocket.conf` seguían en disco.
**Causa raíz:** El comando sólo limpiaba la conf de WG y el unit de systemd.
**Fix:** `uninstall_wg_config()` borra el sysctl drop-in y reaplica `sysctl --system`; `_spawn_deferred_cleanup()` lanza un script bash separado que borra el venv y el symlink tras salir el proceso (no se puede borrar el venv que estás ejecutando). (commit `3516456`)
**Prevención:** Cuando un proceso se desinstala a sí mismo, la limpieza del propio binario tiene que diferirse a otro proceso.

---

## Abiertos

### ✅ B-012 — Servidor Linux Mint pierde su propia conexión a internet tras `setup`
**Síntomas:** Al instalar WarpSocket en Mint (VirtualBox, Red NAT), la VM servidor pierde acceso a internet (no sólo el cliente: el propio servidor).
**Causa raíz confirmada:** Dos problemas combinados:
1. `wg-quick` con `Table = auto` (por defecto) modifica la tabla de rutas del kernel al levantar `wg0`, pudiendo dejar la ruta default inaccesible en setups con NetworkManager o VirtualBox NAT.
2. Las reglas `iptables -A FORWARD` se añadían al final de la cadena; si `ufw` tiene política DROP por defecto, su regla DROP precede a nuestro ACCEPT y bloquea el tráfico de forwarding del cliente (y en algunos setups también afecta al servidor).
**Fix:**
- Añadido `Table = off` en la sección `[Interface]` del servidor: wg-quick ya no toca la tabla de rutas; el routing lo gestiona iptables vía MASQUERADE. La ruta directa `10.0.0.0/24 dev wg0` la crea el kernel al asignar la IP a la interfaz.
- Cambiado `-A FORWARD` por `-I FORWARD 1` y `-I FORWARD 2` en PostUp: las reglas ACCEPT se insertan al principio de la cadena, antes de cualquier DROP de ufw u otras políticas.
- Añadida función `_configure_ufw_if_active()` en el wizard: si ufw está activo, abre el puerto wstunnel y pone `DEFAULT_FORWARD_POLICY="ACCEPT"` en `/etc/default/ufw`.
**Prevención:** Para cualquier servidor WG gateway: siempre `Table = off` + `-I` en lugar de `-A` para reglas FORWARD. El wizard debe detectar ufw automáticamente.

### ✅ B-013 — `list-clients` no refleja cambios entre ejecuciones rápidas
**Síntomas:** Tras conectar/desconectar un cliente, varias ejecuciones consecutivas devuelven el mismo estado.
**Causa raíz:** Comportamiento esperado de WireGuard. `wg show dump` devuelve el último handshake conocido. WireGuard renegocia sesiones cada ~2 minutos cuando hay tráfico, o cuando una sesión lleva ~3 minutos sin actividad. El threshold `_ONLINE_WINDOW_SECONDS = 180` refleja este TTL: un peer no puede marcarse como "offline" en menos de 3 minutos desde el último handshake porque WireGuard mantiene la sesión abierta ese tiempo.
**No hay fix de código**: es una limitación de la API de WireGuard. El estado se actualiza dentro de la ventana de 3 minutos para peers con tráfico activo.
**Prevención:** Documentar en `--help` de `list-clients` que el estado offline puede tardar hasta 3 minutos en reflejarse.

### ✅ B-014 — Cliente Windows: la auto-elevación no dispara UAC
**Síntomas:** Pese a `_ensure_elevated()`, ejecutando como usuario no-admin la app arranca sin pedir UAC y luego falla en `route add`.
**Causa raíz:** Cuando pip instala el entry point en Windows, crea `warpsocket.exe` en `Scripts/`. Al ejecutarlo, `sys.argv[0]` apuntaba a ese `.exe`, pero el código usaba siempre `sys.executable` (python.exe) + script path. Llamar a `ShellExecuteW("runas", "python.exe", "warpsocket.exe")` relanzaba Python sin los argumentos correctos para activar la app.
**Fix:** Añadida detección del caso pip entry point: si `sys.argv[0].endswith(".exe")`, se relanza ese `.exe` directamente elevado con `params=None`. Tres ramas: PyInstaller (frozen), pip .exe, y python directo.
**Prevención:** Cualquier app con auto-elevación en Windows debe distinguir los tres casos de arranque (frozen / pip exe / python script) explícitamente.

### ✅ B-015 — Botón "Ver logs" sigue sin abrir nada en Windows
**Síntomas:** Tras el fix de thread-safety (B-006), el botón sigue sin responder en algunos casos.
**Causa raíz:** Dos problemas:
1. Múltiples clicks abrían ventanas huérfanas sin enfocar porque la ventana previa podía seguir abierta pero no visible (detrás de otras).
2. `CTkToplevel` en algunas versiones de customtkinter/Windows no se renderiza correctamente si el root no ha procesado sus primeras tareas de idle (`update_idletasks` no llamado antes de crear la ventana hija).
**Fix:**
- Singleton: `_log_window` dict en módulo guarda referencia a la ventana activa; si ya existe y `winfo_exists()` devuelve True, se hace `deiconify()` + `lift()` + `focus_force()` en vez de abrir otra.
- Llamada a `root.update_idletasks()` antes de crear `CTkToplevel`.
- `win.deiconify()` explícito tras crear la ventana para garantizar que no arranque minimizada.
**Prevención:** Toda ventana secundaria en una app de bandeja debe ser singleton con mecanismo de re-raise.

### ✅ B-016 — Cliente Windows: WireGuard no se cierra del todo al desconectar
**Síntomas:** Tras pulsar "Salir" (o reconectar), el túnel WireGuard queda visible en WireGuard for Windows o el servicio sigue apareciendo activo en `sc query`. Los reintentos de conexión fallaban en `tcp_probe` porque el adaptador WG (con `AllowedIPs=0.0.0.0/0`) seguía activo cuando ya se había borrado la ruta de bypass.
**Causa raíz:** Tres problemas combinados en `platforms/windows.py`:
1. `wireguard.exe /uninstalltunnelservice <name>` es **asíncrono**: el proceso retorna inmediatamente pero el SCM sigue parando el servicio en background. `disconnect()` no esperaba a que desapareciera.
2. El fichero `.conf` en `%LOCALAPPDATA%\WarpSocket\wireguard\<name>.conf` **no se borraba** tras el uninstall. WireGuard for Windows lo detecta y lo lista como "tunnel disponible" aunque el servicio esté parado.
3. Sin polling posterior, la ruta de bypass se borraba mientras el adaptador WG aún enrutaba tráfico → `tcp_probe` fallaba en el siguiente intento.
**Fix:** `uninstall_wg_tunnel` ahora hace polling de `sc query WireGuardTunnel$<name>` tras el uninstall (timeout 8 s, polling 250 ms) y borra el `.conf` una vez confirmado que el servicio ha desaparecido. Warning en log si se agota el timeout.
**Prevención:** Cualquier operación con SCM de Windows debe asumir asincronía y esperar confirmación explícita antes de modificar rutas o ficheros dependientes.

### ✅ B-017 — wstunnel muere <1 segundo: no verifica cert auto-firmado correctamente
**Síntomas:** El log del cliente muestra "Starting wstunnel" seguido de "Tunnel died unexpectedly" ~700 ms después. El servidor no registra ninguna conexión WebSocket tras el fingerprint check. Los reintentos fallan en `tcp_probe` (B-016 cascada).
**Causa raíz:** wstunnel valida el certificado TLS del servidor contra el CA store del sistema operativo. El certificado auto-firmado generado por el wizard no está en ningún CA store → wstunnel sale inmediatamente con error de verificación TLS.
**Fix:** Añadido `--dangerous-disable-certificate-verification` al comando de wstunnel en `build_wstunnel_command`. La seguridad de identidad del servidor la proporciona el pinning SHA-256 (`verify_tls_fingerprint`) que se ejecuta antes de arrancar wstunnel, por lo que omitir la validación CA de wstunnel no introduce vulnerabilidades adicionales.
**Prevención:** Al integrar cualquier binario externo que haga TLS, verificar siempre cómo gestiona certs auto-firmados. No asumir que el flujo Python y el binario heredan la misma configuración de confianza TLS.

---

## Cómo añadir un bug nuevo

1. Asignar `B-XXX` consecutivo.
2. Rellenar: síntomas, causa raíz (o hipótesis si abierto), fix con commit, prevención.
3. Mover a la sección correcta cuando cambie de estado.
