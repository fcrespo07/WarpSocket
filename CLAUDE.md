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

## Configuración

Un único `config.json` gestiona el cliente. Generado por el **wizard al primer arranque** (no se edita a mano).

```json
{
  "server": {
    "url": "wss://tu-servidor.example.com",
    "http_upgrade_path_prefix": "tu-secreto-aleatorio"
  },
  "tunnel": {
    "local_port": 51820,
    "remote_host": "10.0.0.1",
    "remote_port": 51820
  },
  "wireguard": {
    "tunnel_name": "MiTunel"
  },
  "routing": {
    "bypass_ips": ["188.114.96.5", "188.114.97.5"]
  },
  "reconnect": {
    "max_attempts": 5,
    "delays_seconds": [5, 10, 20, 30, 60]
  }
}
```

El servidor genera este JSON al final de su wizard para que el usuario solo tenga que pegarlo en el cliente.

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

**wstunnel** está bajo **BSD-3-Clause**. Implicaciones para WarpSocket:

- ✅ Se puede redistribuir el binario de wstunnel junto con WarpSocket manteniendo aviso de copyright y disclaimer.
- ✅ Se pueden crear derivados y modificarlos.
- ❌ **No se puede usar el nombre "wstunnel"** ni nombres de contribuyentes para endosar o promocionar WarpSocket. Por eso el proyecto se llama **WarpSocket** y no "wstunnel-gui" o similar.

WarpSocket irá bajo MIT o Apache-2.0 (decidir antes de publicar).

## Convenciones de código

- Python 3.11+, type hints obligatorios.
- `ruff` + `black` para formato/lint.
- Docstrings solo cuando el "por qué" no sea obvio del nombre (regla estándar del repo).
- Sin comentarios inline triviales.
- Tests donde tenga sentido (lógica de config, parser de logs, abstracciones de plataforma mockeables).

## Estado actual

**Fase 0 — Planificación** ✅ (este documento)

**Siguiente**: bootstrapping del cliente Python — scaffolding de paquetes, `pyproject.toml`, config loader, wizard mínimo funcional.

El repo todavía **no** está en GitHub. Se subirá como privado a `WarpSocket` una vez haya un primer esqueleto funcional.

## Notas para futuras sesiones

- El directorio hermano `C:\Users\ferra\Documents\wstunnel_10.5.2_windows_amd64.tar\script portable\` contiene el script PowerShell original. Úsalo como referencia funcional (flujo de reconexión, estructura de menú, manejo de errores), pero **no** copies literales — la arquitectura Python es distinta.
- El autor prefiere iterar: no diseñar todo de golpe. Tras el scaffolding, priorizar que el cliente en Windows funcione end-to-end, luego portar a Linux, luego macOS, luego servidor.
- Antes de publicar el repo (privado primero, público después): revisar que no queden referencias a `vpn.fcrespo.tech`, `ClaveSegura123`, `10.43.9.43`, `PortatilDesbloqueado` ni IPs específicas del autor.
