# WarpSocket

Herramienta multiplataforma (cliente + servidor) para levantar un túnel **WireGuard sobre WebSocket** usando [wstunnel](https://github.com/erebe/wstunnel) como transporte. Pensada para entornos donde UDP está bloqueado pero HTTPS/WebSocket pasa (redes corporativas, Wi-Fi cautivos, móvil tras CGNAT, etc.).

## Origen del proyecto

Nace como reescritura de un script PowerShell portable (`C:\Users\ferra\Documents\wstunnel_10.5.2_windows_amd64.tar\script portable\`) que funcionaba solo en Windows y estaba atado al servidor personal del autor. Los problemas del script original que WarpSocket resuelve:

- **Valores hardcodeados en el código** (URL del servidor `vpn.fcrespo.tech`, IP interna `10.43.9.43`, secreto `ClaveSegura123`, IPs de Cloudflare, nombre del túnel WireGuard del autor).
- **Solo Windows**, PowerShell + WinForms. Frágil y difícil de mantener.
- **Sin wizard de configuración**: el usuario editaba el código.
- **Sin componente de servidor**: había que montar wstunnel a mano en el VPS.

El script original se mantiene intacto como referencia. **No lo modifiques** — es la versión que usa actualmente el autor en producción.

## Alcance

WarpSocket es una herramienta **genérica**: cualquier persona con un servidor propio debe poder usarla. No está atada a ninguna infraestructura concreta.

- **Cliente**: app de bandeja del sistema (tray) multiplataforma que lanza wstunnel y gestiona el túnel WireGuard asociado.
- **Servidor**: wizard CLI que instala y configura wstunnel como servicio en cualquier OS.
- **Cliente y servidor pueden estar en OS distintos** (ej. servidor Linux + cliente Windows, o servidor Windows + cliente macOS).

### Plataformas soportadas (cliente y servidor)

- Windows 10/11
- Linux (distros con systemd)
- macOS

## Stack técnico

**Lenguaje**: Python 3.11+

Se valoró C# + WinForms (descartado: no cross-platform sin reescribir UI entera) y Electron (descartado: instaladores de 150+ MB). Python ofrece el mejor equilibrio entre portabilidad, velocidad de desarrollo y tamaño del binario final.

### Cliente

| Rol | Librería |
|---|---|
| Tray icon | `pystray` |
| UI (wizard + diálogos) | `customtkinter` |
| Packaging | `PyInstaller` |
| Installer Windows | Inno Setup → `.exe` wizard |
| Installer Linux | `.deb` / AppImage / script |
| Installer macOS | `.app` + `.dmg` |

**Versión TUI futura**: hay un acuerdo de hacer una segunda versión del cliente como TUI (probablemente con [`textual`](https://textual.textualize.io/)) cuando la versión GUI sea estable. Encajaría como cliente alternativo del mismo `TunnelManager` — la lógica de túnel ya está separada de la UI y puede compartirse.

### Servidor

- Wizard CLI interactivo (`questionary` o `rich` + `prompt_toolkit`).
- Instalación como servicio nativo: **systemd** (Linux), **launchd** (macOS), **Windows Service Manager** (Windows).
- Genera el `config.json` listo para pegar en el cliente.

## Arquitectura

```
WarpSocket/
├── client/
│   ├── warpsocket/
│   │   ├── app.py            # Entry point, event loop
│   │   ├── tray.py           # pystray + menú contextual
│   │   ├── wizard.py         # Wizard de configuración inicial (customtkinter)
│   │   ├── tunnel.py         # Gestión del proceso wstunnel + watchdog + reconexión
│   │   ├── wireguard.py      # Fachada WireGuard (delega en platforms/)
│   │   ├── network.py        # Gestión de rutas (delega en platforms/)
│   │   ├── config.py         # Lectura/escritura config.json
│   │   ├── logs.py           # Logger + rotación + ventana de logs
│   │   └── platforms/
│   │       ├── base.py       # Interfaz abstracta
│   │       ├── windows.py
│   │       ├── linux.py
│   │       └── macos.py
│   ├── resources/
│   │   └── app_icon.{ico,png,icns}
│   ├── requirements.txt
│   └── pyproject.toml
│
├── server/
│   ├── warpsocket_server/
│   │   ├── setup.py          # Wizard CLI
│   │   ├── service.py        # Instalación como servicio (platform-specific)
│   │   └── platforms/
│   │       ├── linux.py      # systemd unit
│   │       ├── macos.py      # launchd plist
│   │       └── windows.py    # SCM via pywin32
│   └── requirements.txt
│
├── installer/
│   ├── windows/setup.iss     # Inno Setup
│   ├── linux/install.sh
│   └── macos/build_dmg.sh
│
├── config.example.json
├── README.md
└── CLAUDE.md                 # (este archivo)
```

### Abstracción por plataforma

El patrón: `platforms/base.py` define la interfaz; cada OS tiene su implementación. `wireguard.py` y `network.py` no contienen lógica específica de OS, solo importan el módulo correcto según `sys.platform`.

| Operación | Windows | Linux | macOS |
|---|---|---|---|
| Levantar tunnel WG | `wireguard.exe /installtunnelservice` | `wg-quick up` + systemd | `wg-quick up` (Homebrew) |
| Rutas estáticas | `route add X MASK Y Z` | `ip route add X via Y` | `route -n add -net X Y` |
| Config WG | `.conf.dpapi` (DPAPI) | `.conf` plano (`/etc/wireguard/`) | `.conf` plano |
| Servicio del servidor | SCM (pywin32) | systemd unit | launchd plist |

## Distribución e instalación

Un único one-liner por OS:

- Linux/macOS: `curl -fsSL <url>/install.sh | bash`
- Windows: `irm <url>/install.ps1 | iex`

El script bootstrap pregunta al usuario si instala **cliente** o **servidor**, descarga el repo, instala Python 3.11+ si falta, instala las deps y lanza el wizard correspondiente.

**Modelo de empaquetado (fase inicial)**: instalación desde fuente con `pip install -e .` para ambos, cliente y servidor. El cliente migrará a binarios PyInstaller pre-construidos vía GitHub Releases cuando haya CI montada (evita el requisito de Python en la máquina del usuario). El servidor se queda con instalación desde fuente.

**Registro como servicio**: el `install.sh`/`install.ps1` registra automáticamente el binario como servicio del SO al final del wizard (Windows Service / systemd unit / launchd plist). El usuario no tiene que hacer nada extra para que arranque al iniciar sesión.

Hosting del script: pendiente de decidir entre dominio propio y `raw.githubusercontent.com`. No bloquea el desarrollo.

## TLS y endpoint del servidor

El servidor está pensado para correr **sin dominio**. Implicaciones:

- El wizard del servidor **detecta la IP pública** (consultando un servicio tipo `api.ipify.org`), la propone como endpoint y permite override por si el usuario sí tiene dominio.
- El servidor **genera un certificado TLS auto-firmado** durante la instalación. wstunnel sigue usando WSS (necesario para atravesar firewalls corporativos).
- El cliente **no valida contra una CA**: hace **pinning del fingerprint SHA256** del cert, embebido en el `.warpcfg`. Cero dependencia de Let's Encrypt / DuckDNS / dominios.
- Ramas Let's Encrypt + dynamic DNS pueden añadirse más adelante como opción del wizard, pero no son la vía por defecto.

## Bypass routing (¿necesario siempre?)

Sí. Cuando WireGuard captura todo el tráfico (`AllowedIPs = 0.0.0.0/0`), el propio tráfico de wstunnel también caería dentro del túnel → loop. La excepción de routing hacia las IPs del endpoint es **obligatoria en cualquier setup**, no algo específico de Cloudflare.

- Si el servidor está detrás de Cloudflare/CDN, son las IPs del proxy (varias).
- Si el servidor es directo, es **una sola IP** (la pública del servidor).
- El servidor calcula sus propias IPs de bypass durante la instalación y las **embebe en el `.warpcfg`** — el cliente las aplica tal cual, sin pedirlas al usuario.

## Apertura de puertos

Sí, el servidor necesita un puerto público abierto (default **443** para mimetizarse con HTTPS). El wizard del servidor:

1. Pregunta qué puerto usar.
2. Tras instalar, ejecuta un **probe de conectividad desde fuera** (servicio externo) y avisa si no llega.
3. Imprime instrucciones específicas según el caso (router doméstico con port-forward vs. firewall de VPS).

Para usuarios sin homelab: necesitan VPS (Oracle Free Tier, Hetzner, etc.). No hay forma de evitarlo manteniendo el modelo self-hosted.

## Configuración

El servidor genera **un fichero `.warpcfg` por cliente** (formato JSON). Cada `.warpcfg` contiene todo lo necesario para conectarse — el cliente solo importa el fichero y arranca, sin más preguntas.

```json
{
  "server": {
    "endpoint": "203.0.113.42",
    "port": 443,
    "http_upgrade_path_prefix": "<secreto-aleatorio>"
  },
  "tls": {
    "cert_fingerprint_sha256": "AB:CD:EF:..."
  },
  "tunnel": {
    "local_port": 51820,
    "remote_host": "10.0.0.1",
    "remote_port": 51820
  },
  "wireguard": {
    "tunnel_name": "WarpSocket",
    "client_address": "10.0.0.42/32",
    "client_private_key": "<base64>",
    "server_public_key": "<base64>",
    "dns": ["1.1.1.1"]
  },
  "routing": {
    "bypass_ips": ["203.0.113.42"]
  },
  "reconnect": {
    "max_attempts": 5,
    "delays_seconds": [5, 10, 20, 30, 60]
  }
}
```

**El `.warpcfg` es sensible**: contiene la clave privada WireGuard del cliente. Quien tenga el fichero ES ese cliente. Tratarlo como una credencial.

El cliente, al importar el `.warpcfg`, lo guarda como `config.json` en la ruta de configuración del usuario (`%APPDATA%\WarpSocket\` en Windows, `~/.config/warpsocket/` en Linux/macOS).

### Comandos del servidor

Tras la instalación, el ejecutable del servidor expone subcomandos:

- `warpsocket-server add-client <nombre>` — genera nuevo par de claves WG, asigna IP del pool, escribe `<nombre>.warpcfg` en el directorio actual.
- `warpsocket-server list-clients` — lista clientes registrados.
- `warpsocket-server revoke-client <nombre>` — elimina cliente del peer-list de WireGuard.
- `warpsocket-server status` — estado del servicio wstunnel y de WireGuard.

## Funcionalidades heredadas del script original (a mantener)

- Mutex para evitar doble instancia.
- Watchdog que reinicia wstunnel si cae (backoff exponencial: 5s→10s→20s→30s→60s, max 5 intentos).
- Timer de estabilidad: si la conexión aguanta 30s, el contador se resetea.
- Rotación de log (limite 512 KB).
- Menú de bandeja: Ver logs, Reconectar, Acceso directo, Desconectar.
- Ventana de logs en vivo (tail -f style).
- Notificaciones al conectar / reconectar / fallar.
- Limpieza de rutas estáticas al desconectar.
- Desinstalación del servicio WireGuard al salir.

## Licencia

**WarpSocket** se distribuye bajo **MIT**. Confirmar antes de publicar la primera versión estable.

### Dependencias y sus licencias

| Componente | Licencia | Notas |
|---|---|---|
| wstunnel | BSD-3-Clause | Bundleable con atribución. No usar el nombre "wstunnel" para promover WarpSocket. |
| WireGuard (kernel/tools/Windows) | GPL-2.0 | Invocado vía subprocess, no linked → no contamina. Instalado por el OS package manager, no bundleado. Sin obligaciones GPL mientras no se incluya el binario en el instalador. |
| Protocolo WireGuard | Sin patente | Libre. |
| pystray | LGPL-3.0 | ⚠️ En binarios PyInstaller usar modo **one-folder** (no one-file) para que el usuario pueda reemplazar la lib. Incluir texto LGPL en `THIRD_PARTY_LICENSES`. |
| customtkinter | MIT | Sin restricciones. |
| Pillow | MIT-CMU (HPND) | Sin restricciones. |
| platformdirs | MIT | Sin restricciones. |
| Python (CPython) | PSF (BSD-style) | Sin restricciones. |

### Marcas registradas

- **"WireGuard"** es trademark de Jason Donenfeld. No usar en el nombre del proyecto ni para implicar endorsement.
- **"wstunnel"** — misma restricción (BSD-3-Clause cláusula 3). Por eso el proyecto se llama WarpSocket.

### Checklist antes de publicar la primera versión estable

- [ ] Confirmar licencia MIT (o cambiar a Apache-2.0 si se esperan contribuciones corporativas).
- [ ] Añadir fichero `LICENSE` en la raíz con el texto MIT.
- [ ] Añadir fichero `THIRD_PARTY_LICENSES` con BSD-3-Clause de wstunnel + LGPL-3.0 de pystray.
- [ ] Verificar que no queden referencias a `vpn.fcrespo.tech`, `ClaveSegura123`, `10.43.9.43`, `PortatilDesbloqueado` ni IPs del autor.

## Convenciones de código

- Python 3.11+, type hints obligatorios.
- `ruff` + `black` para formato/lint.
- Docstrings solo cuando el "por qué" no sea obvio del nombre (regla estándar del repo).
- Sin comentarios inline triviales.
- Tests donde tenga sentido (lógica de config, parser de logs, abstracciones de plataforma mockeables).

## Estado actual

**Fase 0 — Planificación** ✅
**Fase 1 — Scaffolding del cliente Python** ✅
**Fase 2 — Schema y loader del `.warpcfg`** ✅
**Fase 3 — Abstracción de plataforma** ✅
**Fase 4a — Orquestador del túnel** ✅ (`wireguard.py`, `network.py`, `tunnel.py`: build de la conf WG, TLS pinning en Python, `Tunnel.connect()/disconnect()`)
**Fase 4b — Watchdog y reconexión** ✅ (`TunnelManager` con thread de monitorización, backoff de la config, máquina de estados `DISCONNECTED/CONNECTING/CONNECTED/RECONNECTING/FAILED`, listeners para que el tray se enganche)

**72 tests** pasando (subprocess/socket/ssl mockeado; corren en cualquier OS).

**Siguiente**: tray icon (`tray.py` con pystray) que se suscribe al `TunnelManager` y refleja el estado, más wizard inicial para importar el `.warpcfg`. Esto cierra el flujo end-to-end en Windows: doble-clic al `.warpcfg` → tray icono activo → conectado.

El repo está en GitHub como privado: https://github.com/fcrespo07/WarpSocket

## Notas para futuras sesiones

- El directorio hermano `C:\Users\ferra\Documents\wstunnel_10.5.2_windows_amd64.tar\script portable\` contiene el script PowerShell original. Úsalo como referencia funcional (flujo de reconexión, estructura de menú, manejo de errores), pero **no** copies literales — la arquitectura Python es distinta.
- El autor prefiere iterar: no diseñar todo de golpe. Tras el scaffolding, priorizar que el cliente en Windows funcione end-to-end, luego portar a Linux, luego macOS, luego servidor.
- Antes de publicar el repo (privado primero, público después): revisar que no queden referencias a `vpn.fcrespo.tech`, `ClaveSegura123`, `10.43.9.43`, `PortatilDesbloqueado` ni IPs específicas del autor.
