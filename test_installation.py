"""Exercise setup/uninstall in an isolated HOME with mock package managers."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class Installation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.project = self.home / "project"
        tools = self.project / "tools"
        tools.mkdir(parents=True)
        for name in ("install-retro.sh", "uninstall.sh"):
            source = (ROOT / "tools" / name).read_text()
            source = source.replace(
                "/Applications/", str(self.home / "Applications") + "/"
            )
            (tools / name).write_text(source)
        for utility in (
            "mkdir",
            "basename",
            "dirname",
            "mv",
            "grep",
            "sort",
            "tr",
            "touch",
            "rm",
            "rmdir",
        ):
            (self.bin / utility).symlink_to(shutil.which(utility))
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin))
        self.command("uname", 'printf "Darwin\\n"')
        self.command("id", 'printf "0\\n"')
        self.command("curl", 'printf "download\\n" > "$3"')
        self.command(
            "brew",
            """
printf '%s\n' "$*" >> "$HOME/brew-calls"
case "$1" in
  install) touch "$HOME/kitty-installed" ;;
  list) test -f "$HOME/kitty-installed" ;;
  --cache) printf '%s\n' "$HOME/kitty.dmg" ;;
  uninstall) rm -f "$HOME/kitty-installed" ;;
esac
""",
        )

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def run_script(self, name, success=True):
        result = subprocess.run(
            ["/bin/sh", str(self.project / "tools" / name)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_mac_install_uninstall_and_repeat(self):
        self.run_script("install-retro.sh")
        state = self.home / "Library/Application Support/cka-megatrainer/install-state"
        self.assertTrue((state / "kitty").exists())
        for relative in (
            ".cache/cka-megatrainer",
            "Library/Caches/kitty",
            "project/dist",
            "project/__pycache__",
        ):
            directory = self.home / relative
            directory.mkdir(parents=True)
            (directory / "generated").touch()
        (self.home / "kitty.dmg").touch()
        self.run_script("uninstall.sh")
        self.assertFalse(state.exists())
        self.assertFalse((self.home / "Library/Fonts/VT323-Regular.ttf").exists())
        self.assertFalse(
            (self.home / "Library/Fonts/PressStart2P-Regular.ttf").exists()
        )
        self.assertFalse((self.home / "kitty-installed").exists())
        self.assertFalse((self.home / "kitty.dmg").exists())
        self.assertFalse((self.project / "dist").exists())
        calls = (self.home / "brew-calls").read_text()
        self.run_script("uninstall.sh")
        self.assertEqual((self.home / "brew-calls").read_text(), calls)
        self.run_script("install-retro.sh")
        self.assertTrue((state / "kitty").exists())

    def test_preserve_existing_terminal_and_fonts(self):
        self.command("kitty", "exit 0")
        fonts = self.home / "Library/Fonts"
        fonts.mkdir(parents=True)
        font = fonts / "VT323-Regular.ttf"
        font.write_text("preexisting font")
        self.run_script("install-retro.sh")
        self.run_script("uninstall.sh")
        self.assertEqual(font.read_text(), "preexisting font")
        self.assertTrue((self.bin / "kitty").exists())
        self.assertFalse((self.home / "brew-calls").exists())

    def test_failed_uninstall_retains_ownership_for_retry(self):
        self.run_script("install-retro.sh")
        self.command(
            "brew",
            'case "$1" in uninstall) exit 7 ;; --cache) printf "%s\\n" "$HOME/kitty.dmg" ;; esac',
        )
        self.run_script("uninstall.sh", success=False)
        self.assertTrue(
            (
                self.home
                / "Library/Application Support/cka-megatrainer/install-state/kitty"
            ).exists()
        )

    def test_linux_package_managers_and_repeat_cleanup(self):
        self.command("uname", 'printf "Linux\\n"')
        self.command("fc-cache", "exit 0")
        self.command("fc-list", 'printf "VT323\\nPress Start 2P\\n"')
        for manager in ("apt-get", "dnf", "pacman", "zypper", "apk"):
            for wayland in (False, True):
                with self.subTest(manager=manager, wayland=wayland):
                    self.env["WAYLAND_DISPLAY"] = "wayland-0" if wayland else ""
                    self.command(manager, 'printf "%s\\n" "$*" >> "$HOME/packages"')
                    self.run_script("install-retro.sh")
                    terminal = "foot" if wayland else "xterm"
                    state = self.home / ".local/share/cka-megatrainer/install-state"
                    self.assertTrue((state / f"package-{manager}-{terminal}").exists())
                    self.run_script("uninstall.sh")
                    self.assertFalse(state.exists())
                    calls = (self.home / "packages").read_text()
                    self.assertIn(terminal, calls)
                    self.assertIn(
                        "remove"
                        if manager in ("apt-get", "dnf", "zypper")
                        else "-R"
                        if manager == "pacman"
                        else "del",
                        calls,
                    )
                    self.run_script("uninstall.sh")
                    self.assertEqual((self.home / "packages").read_text(), calls)
                    (self.home / "packages").unlink()
                    (self.bin / manager).unlink()
