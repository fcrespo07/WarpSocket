from __future__ import annotations

import logging
import sys
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

import customtkinter as ctk

from warpsocket.config import ConfigError, ClientConfig, default_config_path, import_warpcfg

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def pick_warpcfg_file() -> Path | None:
    """Open a native file dialog and return the selected .warpcfg path, or None."""
    path = filedialog.askopenfilename(
        title="Seleccionar archivo .warpcfg",
        filetypes=[("WarpSocket config", "*.warpcfg"), ("JSON files", "*.json"), ("All", "*.*")],
    )
    return Path(path) if path else None


def try_import(warpcfg_path: Path, dest: Path | None = None) -> ClientConfig:
    """Validate and import a .warpcfg file. Raises ConfigError on failure."""
    return import_warpcfg(warpcfg_path, dest)


class ImportWizard(ctk.CTk):
    """Minimal wizard: pick a .warpcfg file, validate, import."""

    def __init__(self, on_done: callable | None = None) -> None:
        super().__init__()
        self._on_done = on_done
        self._imported_config: ClientConfig | None = None

        self.title("WarpSocket — Configuración inicial")
        self.geometry("480x260")
        self.resizable(False, False)

        ctk.CTkLabel(
            self,
            text="Bienvenido a WarpSocket",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(30, 5))

        ctk.CTkLabel(
            self,
            text=(
                "No se ha encontrado una configuración.\n"
                "Importa el archivo .warpcfg que te proporcionó\n"
                "el administrador del servidor."
            ),
            font=ctk.CTkFont(size=13),
            justify="center",
        ).pack(pady=(5, 20))

        ctk.CTkButton(
            self,
            text="Importar .warpcfg…",
            width=200,
            height=40,
            command=self._do_import,
        ).pack(pady=(0, 10))

        ctk.CTkButton(
            self,
            text="Salir",
            width=120,
            height=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            command=self._do_quit,
        ).pack()

        self.protocol("WM_DELETE_WINDOW", self._do_quit)

        if sys.platform == "win32":
            try:
                from warpsocket.tray import _RESOURCES
                ico = _RESOURCES / "app_icon.ico"
                if ico.exists():
                    self.iconbitmap(str(ico))
            except Exception:
                pass

    @property
    def imported_config(self) -> ClientConfig | None:
        return self._imported_config

    def _do_import(self) -> None:
        path = pick_warpcfg_file()
        if path is None:
            return

        try:
            config = try_import(path)
        except ConfigError as exc:
            log.warning("Import failed for %s: %s", path, exc)
            messagebox.showerror(
                "Error de importación",
                f"El archivo no es un .warpcfg válido:\n\n{exc}",
                parent=self,
            )
            return
        except Exception as exc:
            log.exception("Unexpected error importing %s", path)
            messagebox.showerror(
                "Error inesperado",
                f"No se pudo importar el archivo:\n\n{exc}",
                parent=self,
            )
            return

        dest = default_config_path()
        log.info("Config imported from %s → %s", path, dest)

        messagebox.showinfo(
            "Importación exitosa",
            f"Configuración importada correctamente.\n\n"
            f"Servidor: {config.server.endpoint}:{config.server.port}\n"
            f"Túnel: {config.wireguard.tunnel_name}",
            parent=self,
        )
        self._imported_config = config
        self.destroy()
        if self._on_done:
            self._on_done(config)

    def _do_quit(self) -> None:
        self._imported_config = None
        self.destroy()


def run_wizard() -> ClientConfig | None:
    """Run the import wizard and return the imported config, or None if cancelled."""
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    wizard = ImportWizard()
    wizard.mainloop()
    return wizard.imported_config
