// Exposes the focused window over D-Bus.
//
// GNOME's own org.gnome.Shell.Introspect.GetWindows answers AccessDenied for unlisted
// callers, and under Wayland an external process cannot ask the compositor directly, so a
// shell extension is the only way to learn what has focus. This is the smallest one that
// does the job: one signal, one getter.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.OpenDeckFocus">
    <method name="GetFocusedWindow">
      <arg type="s" direction="out" name="window"/>
    </method>
    <signal name="FocusedWindowChanged">
      <arg type="s" name="window"/>
    </signal>
  </interface>
</node>`;

const OBJECT_PATH = '/org/gnome/Shell/Extensions/OpenDeckFocus';

export default class OpenDeckFocusExtension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);

        this._focusHandler = global.display.connect('notify::focus-window', () =>
            this._emitFocusedWindow()
        );

        this._emitFocusedWindow();
    }

    disable() {
        if (this._focusHandler) {
            global.display.disconnect(this._focusHandler);
            this._focusHandler = null;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    // Returns JSON rather than a struct so consumers can add fields later without an
    // interface change forcing everyone to re-learn the signature.
    _describeFocusedWindow() {
        const window = global.display.focus_window;

        if (!window) {
            return JSON.stringify({ wm_class: '', title: '' });
        }

        return JSON.stringify({
            wm_class: window.get_wm_class() || '',
            title: window.get_title() || '',
        });
    }

    GetFocusedWindow() {
        return this._describeFocusedWindow();
    }

    _emitFocusedWindow() {
        if (!this._dbus) {
            return;
        }

        this._dbus.emit_signal(
            'FocusedWindowChanged',
            new GLib.Variant('(s)', [this._describeFocusedWindow()])
        );
    }
}
