//! Makes an OpenDeck deck contextual on GNOME Wayland, and gives it a launcher page.
//!
//! OpenDeck already switches profiles per application and already has a launcher plugin; the
//! only thing missing on GNOME is that nothing outside the compositor can see which window has
//! focus (`org.gnome.Shell.Introspect` answers AccessDenied, and XWayland's
//! `_NET_ACTIVE_WINDOW` is meaningless for native Wayland clients). So this daemon does two
//! small things and delegates the rest:
//!
//!   * a companion shell extension reports focus changes on the session bus, and we mirror them
//!     onto an X11 shim window that OpenDeck's own watcher already looks at (see `shim`);
//!   * a mode socket lets a deck key pin a synthetic application, which OpenDeck maps to the
//!     launcher profile exactly like any real one.
//!
//! No OpenDeck patch, no profile-switch events, no rules file: the application-to-profile
//! mapping lives in OpenDeck's own UI, where a user would look for it.

use futures_lite::StreamExt;
use tokio::sync::mpsc;

mod banks;
mod identity;
mod seen;
mod shim;
mod titles;
use shim::Shim;

/// Synthetic WM_CLASS published while the launcher is pinned. It shows up in OpenDeck's
/// application list like any other app, and is mapped to the launcher profile there.
const LAUNCHER_CLASS: &str = "OpenDeckLauncher";

const DBUS_INTERFACE: &str = "org.gnome.Shell.Extensions.OpenDeckFocus";
const DBUS_PATH: &str = "/org/gnome/Shell/Extensions/OpenDeckFocus";

#[derive(Clone, Debug, PartialEq)]
struct Window {
    wm_class: String,
    title: String,
    pid: u32,
}

enum Event {
    Focus(Window),
    Mode(Mode),
    /// The dial: +1 for a turn one way, -1 the other.
    Page(isize),
    Poll,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    Contextual,
    Launcher,
}

fn socket_path() -> std::path::PathBuf {
    let runtime = std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".into());
    std::path::Path::new(&runtime).join("opendeck-focus.sock")
}

fn parse_window(json: &str) -> Option<Window> {
    let value: serde_json::Value = serde_json::from_str(json).ok()?;
    Some(Window {
        wm_class: value.get("wm_class")?.as_str()?.to_owned(),
        title: value.get("title").and_then(|v| v.as_str()).unwrap_or_default().to_owned(),
        pid: value.get("pid").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
    })
}

/// Relays `FocusedWindowChanged` from the shell extension. Reconnects forever: the extension
/// goes away with every shell restart, and this daemon outliving it is the whole point.
async fn watch_focus(tx: mpsc::Sender<Event>) {
    loop {
        if let Err(error) = watch_focus_once(&tx).await {
            log::warn!("Focus watch stopped: {error}. Retrying in 5s.");
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn watch_focus_once(tx: &mpsc::Sender<Event>) -> Result<(), Box<dyn std::error::Error>> {
    let connection = zbus::Connection::session().await?;

    let mut stream = zbus::MessageStream::for_match_rule(
        zbus::MatchRule::builder()
            .msg_type(zbus::message::Type::Signal)
            .interface(DBUS_INTERFACE)?
            .member("FocusedWindowChanged")?
            .build(),
        &connection,
        None,
    )
    .await?;

    // Ask once up front, so the deck matches the window that already has focus instead of
    // waiting for the user to alt-tab before anything happens.
    match connection
        .call_method(Some("org.gnome.Shell"), DBUS_PATH, Some(DBUS_INTERFACE), "GetFocusedWindow", &())
        .await
    {
        Ok(reply) => {
            log::info!("Shell extension is loaded");
            if let Ok(json) = reply.body().deserialize::<String>()
                && let Some(window) = parse_window(&json)
            {
                let _ = tx.send(Event::Focus(window)).await;
            }
        }
        // Not fatal, and the common case on first install: the shell only scans for new
        // extensions at session start, so it appears at the next login and the signal
        // subscription below starts producing then.
        Err(error) => log::warn!("Shell extension not answering ({error}); waiting for it"),
    }

    while let Some(message) = stream.next().await {
        if let Ok(json) = message?.body().deserialize::<String>()
            && let Some(window) = parse_window(&json)
        {
            let _ = tx.send(Event::Focus(window)).await;
        }
    }

    Err("signal stream ended".into())
}

/// Listens for `launcher` / `contextual` / `toggle` on a datagram socket, which is what the
/// deck's two screenless buttons send through OpenDeck's Run Command action.
async fn watch_mode(tx: mpsc::Sender<Event>) -> Result<(), Box<dyn std::error::Error>> {
    let path = socket_path();
    let _ = std::fs::remove_file(&path);
    let socket = tokio::net::UnixDatagram::bind(&path)?;
    log::info!("Mode socket at {}", path.display());

    let mut buffer = [0u8; 64];
    loop {
        let length = socket.recv(&mut buffer).await?;
        let message = String::from_utf8_lossy(&buffer[..length]).trim().to_owned();
        let event = match message.as_str() {
            "launcher" => Event::Mode(Mode::Launcher),
            "contextual" => Event::Mode(Mode::Contextual),
            "page next" | "page" => Event::Page(1),
            "page previous" | "page prev" => Event::Page(-1),
            "page first" => Event::Page(0),
            other => {
                log::warn!("Unknown message {other:?}");
                continue;
            }
        };
        let _ = tx.send(event).await;
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let shim = Shim::new()?;
    let (tx, mut rx) = mpsc::channel(16);

    tokio::spawn(watch_focus(tx.clone()));

    let poll_tx = tx.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_millis(1000));
        loop {
            interval.tick().await;
            if poll_tx.send(Event::Poll).await.is_err() {
                break;
            }
        }
    });

    tokio::spawn(async move {
        if let Err(error) = watch_mode(tx).await {
            log::error!("Mode socket failed: {error}");
        }
    });

    let mut mode = Mode::Contextual;
    let mut focused: Option<Window> = None;
    // The window that had focus when the launcher was pinned. Pressing a deck key does not move
    // focus, so the launcher stays up while you read it -- but launching something does, and at
    // that point you want the new app's keys, not the launcher you just used.
    let mut launcher_anchor: Option<String> = None;
    let mut published = String::new();
    // Which page of keys is showing, and for which application. A page belongs to the app it
    // was turned to: walking away and coming back starts at the first page, because the second
    // page of Onshape means nothing while you are in a browser tab.
    let mut bank: usize = 0;
    let mut bank_owner = String::new();

    while let Some(event) = rx.recv().await {
        match event {
            Event::Focus(window) => {
                if mode == Mode::Launcher && launcher_anchor.as_deref() != Some(window.wm_class.as_str()) {
                    log::info!("Focus moved to {:?}; leaving launcher", window.wm_class);
                    mode = Mode::Contextual;
                    launcher_anchor = None;
                }
                focused = Some(window);
            }
            Event::Mode(requested) => {
                mode = requested;
                launcher_anchor = match requested {
                    Mode::Launcher => focused.as_ref().map(|w| w.wm_class.clone()),
                    Mode::Contextual => None,
                };
                log::info!("Mode {mode:?}");
            }
            Event::Page(step) => {
                let pages = banks::count(&bank_owner);
                bank = if step == 0 { 0 } else { banks::advance(bank, pages, step) };
                log::info!("Page {} of {pages} for {bank_owner:?}", bank + 1);
            }
            // The foreground program can change without any focus event (you type `claude` in
            // an already-focused window), so recompute the identity on a timer.
            Event::Poll => {}
        }

        let (class, title, pid) = match (mode, &focused) {
            (Mode::Launcher, _) => (LAUNCHER_CLASS.to_owned(), "OpenDeck launcher".to_owned(), std::process::id()),
            (Mode::Contextual, Some(window)) => (
                identity::resolve(&window.wm_class, window.pid, &window.title),
                window.title.clone(),
                window.pid,
            ),
            // Focus unknown -- the shell extension has not loaded yet. Publishing an empty
            // class is not a no-op: OpenDeck reads it as "no mapping", falls back to its
            // opendeck_default profile, and so leaving the launcher still takes you somewhere
            // instead of stranding you on it.
            (Mode::Contextual, None) => (String::new(), String::new(), 0),
        };

        // A different application is a fresh start: its pages are not this one's pages.
        if class != bank_owner {
            bank_owner = class.clone();
            bank = 0;
        }
        let class = banks::with_bank(&class, bank);

        // The watcher polls four times a second and only reacts to a changed name, so
        // republishing an unchanged class would be pure noise in the log.
        if class == published {
            continue;
        }
        published = class.clone();

        log::info!("Publishing {class:?}");
        seen::record(&class);
        if let Err(error) = shim.publish(&class, &title, pid) {
            log::error!("Failed to publish to X11: {error}");
        }
    }

    Err("event channel closed".into())
}

/// `opendeck-focus mode launcher|contextual`, `opendeck-focus page next|previous|first` --
/// what the deck's screenless buttons and its dial run.
fn send_mode(mode: &str) -> Result<(), Box<dyn std::error::Error>> {
    let socket = std::os::unix::net::UnixDatagram::unbound()?;
    socket.send_to(mode.as_bytes(), socket_path())?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<String> = std::env::args().collect();
    if arguments.get(1).map(|s| s.as_str()) == Some("identity") {
        if arguments.len() < 4 || arguments.len() > 5 {
            eprintln!("usage: opendeck-focus identity <wm_class> <pid> [window title]");
            std::process::exit(2);
        }
        let pid: u32 = match arguments[3].parse() {
            Ok(pid) => pid,
            Err(_) => {
                eprintln!("usage: opendeck-focus identity <wm_class> <pid>");
                std::process::exit(2);
            }
        };
        let title = arguments.get(4).map(String::as_str).unwrap_or("");
        println!("{}", identity::resolve(&arguments[2], pid, title));
        return Ok(());
    }
    if arguments.len() == 3 && arguments[1] == "mode" {
        return send_mode(&arguments[2]);
    }
    // `page next` is what the dial runs; `page` alone means the same, because a Run Command
    // action bound to a rotation is easier to type without an argument.
    if arguments.len() >= 2 && arguments[1] == "page" {
        let which = arguments.get(2).map(String::as_str).unwrap_or("next");
        return send_mode(&format!("page {which}"));
    }
    if arguments.len() > 1 {
        eprintln!(
            "usage: opendeck-focus [mode launcher|contextual | page next|previous|first \
             | identity <wm_class> <pid> [title]]"
        );
        std::process::exit(2);
    }

    simplelog::TermLogger::init(
        simplelog::LevelFilter::Info,
        simplelog::Config::default(),
        simplelog::TerminalMode::Stdout,
        simplelog::ColorChoice::Never,
    )
    .unwrap();

    run().await
}
