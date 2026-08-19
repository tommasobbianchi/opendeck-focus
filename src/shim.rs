//! Publishes the focused window as an X11 window, so X11-only tools see it on GNOME Wayland.
//!
//! OpenDeck's application watcher calls `active_win_pos_rs::get_active_window()`, which on
//! Wayland only knows KWin and Hyprland; on GNOME it falls through to the XCB path and reads
//! `_NET_ACTIVE_WINDOW` off the root window. Mutter points that at an XWayland-internal window
//! with no `WM_CLASS` whenever a native Wayland client has focus, so the watcher reads an empty
//! application name and per-application profiles never fire.
//!
//! Rather than patch OpenDeck, we answer the question it is already asking: keep one unmapped
//! window whose `WM_CLASS` mirrors the really-focused window, and point `_NET_ACTIVE_WINDOW` at
//! it. Mutter only rewrites that property when XWayland focus changes, so ours stands.

use x11rb::connection::Connection;
use x11rb::protocol::xproto::{self, ConnectionExt as _, PropMode};
use x11rb::rust_connection::RustConnection;
use x11rb::wrapper::ConnectionExt as _;

pub struct Shim {
    conn: RustConnection,
    window: xproto::Window,
    root: xproto::Window,
    net_active_window: xproto::Atom,
    net_wm_name: xproto::Atom,
    net_wm_pid: xproto::Atom,
    utf8_string: xproto::Atom,
}

impl Shim {
    pub fn new() -> Result<Self, Box<dyn std::error::Error>> {
        let (conn, screen_num) = x11rb::connect(None)?;
        let screen = &conn.setup().roots[screen_num];
        let root = screen.root;

        let window = conn.generate_id()?;
        conn.create_window(
            screen.root_depth,
            window,
            root,
            0,
            0,
            1,
            1,
            0,
            xproto::WindowClass::INPUT_OUTPUT,
            screen.root_visual,
            // Never mapped and never managed: this is a data carrier, not something the user
            // should ever see or alt-tab into.
            &xproto::CreateWindowAux::new().override_redirect(1),
        )?;

        let intern = |name: &str| -> Result<xproto::Atom, Box<dyn std::error::Error>> {
            Ok(conn.intern_atom(false, name.as_bytes())?.reply()?.atom)
        };

        let shim = Self {
            net_active_window: intern("_NET_ACTIVE_WINDOW")?,
            net_wm_name: intern("_NET_WM_NAME")?,
            net_wm_pid: intern("_NET_WM_PID")?,
            utf8_string: intern("UTF8_STRING")?,
            conn,
            window,
            root,
        };
        shim.conn.flush()?;
        Ok(shim)
    }

    /// Makes the shim window look like `wm_class` / `title` / `pid`, and marks it active.
    pub fn publish(&self, wm_class: &str, title: &str, pid: u32) -> Result<(), Box<dyn std::error::Error>> {
        // WM_CLASS is a pair of NUL-terminated strings; active-win-pos-rs takes the last
        // non-empty one, so the class has to come second.
        let mut class = Vec::new();
        class.extend_from_slice(wm_class.as_bytes());
        class.push(0);
        class.extend_from_slice(wm_class.as_bytes());
        class.push(0);

        self.conn.change_property8(PropMode::REPLACE, self.window, xproto::Atom::from(xproto::AtomEnum::WM_CLASS), xproto::Atom::from(xproto::AtomEnum::STRING), &class)?;
        self.conn.change_property8(PropMode::REPLACE, self.window, self.net_wm_name, self.utf8_string, title.as_bytes())?;
        self.conn.change_property8(PropMode::REPLACE, self.window, xproto::Atom::from(xproto::AtomEnum::WM_NAME), xproto::Atom::from(xproto::AtomEnum::STRING), title.as_bytes())?;
        self.conn.change_property32(PropMode::REPLACE, self.window, self.net_wm_pid, xproto::Atom::from(xproto::AtomEnum::CARDINAL), &[pid])?;
        self.conn.change_property32(PropMode::REPLACE, self.root, self.net_active_window, xproto::Atom::from(xproto::AtomEnum::WINDOW), &[self.window])?;
        self.conn.flush()?;
        Ok(())
    }
}
