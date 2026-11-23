"use strict";

// Windows virtual-desktop watcher for the desktop pet.
//
// The pet's transparent/topmost windows are not tracked by the Windows
// virtual-desktop manager (GetWindowDesktopId returns a null GUID), so the
// shell mirrors them onto every desktop and Task View offers no per-window
// "show on all desktops" toggle. The only reliable confinement is to watch
// the current desktop ourselves and hide/show the pet windows when the user
// switches away from the desktop where the pet was launched.

const { spawn } = require("child_process");
const path = require("path");
const readline = require("readline");

const INDEX_LINE = /^INDEX=(\d+)$/;

function parseDesktopLine(line) {
  const match = INDEX_LINE.exec(String(line || "").trim());
  return match ? Number(match[1]) : null;
}

function createVirtualDesktopGuard(options = {}) {
  const platform = options.platform || process.platform;
  const spawnImpl = options.spawnImpl || spawn;
  const scriptPath = options.scriptPath || path.join(
    __dirname, "..", "vendor", "virtual-desktop", "watch.ps1"
  );
  const onDesktopChange = typeof options.onDesktopChange === "function"
    ? options.onDesktopChange
    : () => {};
  const onError = typeof options.onError === "function"
    ? options.onError
    : () => {};

  let child = null;
  let lines = null;
  let started = false;

  function start() {
    if (platform !== "win32" || started) return false;
    started = true;
    try {
      child = spawnImpl("powershell.exe", [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", scriptPath,
      ], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      onError(err);
      return false;
    }

    child.on("error", (err) => onError(err));
    child.on("exit", (code) => {
      if (started && code !== 0) {
        onError(new Error(`virtual-desktop watcher exited with code ${code}`));
      }
    });
    if (child.stderr) {
      child.stderr.on("data", () => {});
    }

    lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      const index = parseDesktopLine(line);
      if (index !== null) onDesktopChange(index);
    });
    return true;
  }

  function stop() {
    started = false;
    try { if (lines) lines.close(); } catch {}
    lines = null;
    try { if (child) child.kill(); } catch {}
    child = null;
  }

  return { start, stop };
}

module.exports = { createVirtualDesktopGuard, parseDesktopLine };
