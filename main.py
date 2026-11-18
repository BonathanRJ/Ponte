"""Ponte — acesso remoto estilo AnyDesk.

Executar:  python main.py
"""
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ponte import APP_NAME
from ponte.app import MainWindow


def light_palette() -> QPalette:
    p = QPalette()
    text, bg, base = QColor("#1f2328"), QColor("#f4f5f7"), QColor("#ffffff")
    for group in (QPalette.Active, QPalette.Inactive):
        p.setColor(group, QPalette.Window, bg)
        p.setColor(group, QPalette.WindowText, text)
        p.setColor(group, QPalette.Base, base)
        p.setColor(group, QPalette.AlternateBase, QColor("#eef0f3"))
        p.setColor(group, QPalette.Text, text)
        p.setColor(group, QPalette.Button, base)
        p.setColor(group, QPalette.ButtonText, text)
        p.setColor(group, QPalette.ToolTipBase, base)
        p.setColor(group, QPalette.ToolTipText, text)
        p.setColor(group, QPalette.PlaceholderText, QColor("#9aa0a6"))
        p.setColor(group, QPalette.Highlight, QColor("#e5484d"))
        p.setColor(group, QPalette.HighlightedText, QColor("#ffffff"))
        p.setColor(group, QPalette.BrightText, QColor("#ffffff"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor("#9aa0a6"))
    p.setColor(QPalette.Disabled, QPalette.Base, bg)
    p.setColor(QPalette.Disabled, QPalette.Button, bg)
    p.setColor(QPalette.Disabled, QPalette.Window, bg)
    return p


def main() -> int:
    # "Abrir a Ponte como administrador": reabre pedindo permissão ao Windows.
    # Se a pessoa recusar, segue como programa comum.
    if "--elevated" not in sys.argv:
        from ponte.config import Config
        from ponte.winadmin import is_admin, relaunch_as_admin
        if Config()["run_as_admin"] and not is_admin() and relaunch_as_admin():
            return 0
    if sys.platform == "win32":
        try:  # ícone próprio na barra de tarefas
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ponte.AcessoRemoto")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(light_palette())  # ignora o tema escuro do Windows (texto branco em fundo branco)
    app.setQuitOnLastWindowClosed(False)  # continua na bandeja enquanto aceita conexões
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
