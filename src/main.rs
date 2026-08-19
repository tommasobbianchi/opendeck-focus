//! Switches OpenDeck profiles to follow the focused window on GNOME Wayland.
//!
//! OpenDeck can already switch profiles per application, but its window watcher only covers
//! X11 and KDE. Under GNOME Wayland nothing can see the focused window from outside the
//! compositor -- org.gnome.Shell.Introspect answers AccessDenied to unlisted callers -- so the
//! companion shell extension publishes it on the session bus and this plugin relays it.

use futures_lite::StreamExt;
use openaction::*;
use serde::Serialize;
use std::sync::{Arc, LazyLock};
use tokio::sync::RwLock;

mod rules;
use rules::Config;

/// Non-spec, OpenDeck-specific: its inbound handler takes a bare device/profile pair.
#[derive(Serialize)]
struct SwitchProfileEvent {
    event: &'static str,
    device: String,
    profile: String,
}

/// The profile we last asked for, so an unchanged focus does not cause a repaint.
/// Every switch clears and redraws the deck, so this is not a micro-optimisation:
/// without it, focus churn makes the keys flicker.
static CURRENT: LazyLock<RwLock<Option<String>>> = LazyLock::new(|| RwLock::new(None));

struct GlobalEventHandler {}
impl openaction::GlobalEventHandler for GlobalEventHandler {
    async fn plugin_ready(&self, _outbound: &mut OutboundEventManager) -> EventHandlerResult {
        let config = match Config::load() {
            Ok(config) => config,
            Err(error) => {
                // Not fatal: a missing rules file should leave OpenDeck usable, not wedge it.
                log::error!("{error}");
                log::error!(
                    "Not watching focus. Write {} to enable it.",
                    Config::path().display()
                );
                return Ok(());
            }
        };

        log::info!(
            "Watching focus for device {} with {} rule(s), default profile {:?}",
            config.device,
            config.rules.len(),
            config.default_profile
        );

        tokio::spawn(watch_focus(Arc::new(config)));

        Ok(())
    }
}

struct ActionEventHandler {}
impl openaction::ActionEventHandler for ActionEventHandler {}

async fn watch_focus(config: Arc<Config>) {
    loop {
        if let Err(error) = watch_focus_once(&config).await {
            // The shell restarts on logout, and takes the extension's bus name with it.
            log::error!("Focus watch stopped: {error}. Retrying in 5s.");
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn watch_focus_once(config: &Config) -> Result<(), Box<dyn std::error::Error>> {
    let connection = zbus::Connection::session().await?;

    let mut stream = zbus::MessageStream::for_match_rule(
        zbus::MatchRule::builder()
            .msg_type(zbus::message::Type::Signal)
            .interface("org.gnome.Shell.Extensions.OpenDeckFocus")?
            .member("FocusedWindowChanged")?
            .build(),
        &connection,
        None,
    )
    .await?;

    // Ask once up front so the deck matches the window that already has focus, rather than
    // waiting for the user to alt-tab before anything happens.
    if let Ok(reply) = connection
        .call_method(
            Some("org.gnome.Shell"),
            "/org/gnome/Shell/Extensions/OpenDeckFocus",
            Some("org.gnome.Shell.Extensions.OpenDeckFocus"),
            "GetFocusedWindow",
            &(),
        )
        .await
        && let Ok(window) = reply.body().deserialize::<String>()
    {
        apply(config, &window).await;
    }

    log::info!("Subscribed to FocusedWindowChanged");

    while let Some(message) = stream.next().await {
        let message = message?;
        if let Ok(window) = message.body().deserialize::<String>() {
            apply(config, &window).await;
        }
    }

    Err("signal stream ended".into())
}

async fn apply(config: &Config, window_json: &str) {
    let wm_class = serde_json::from_str::<serde_json::Value>(window_json)
        .ok()
        .and_then(|v| v.get("wm_class")?.as_str().map(str::to_owned))
        .unwrap_or_default();

    let profile = config.profile_for(&wm_class).to_owned();

    {
        let current = CURRENT.read().await;
        if current.as_deref() == Some(profile.as_str()) {
            return;
        }
    }

    log::info!("Focus {wm_class:?} -> profile {profile:?}");

    let event = SwitchProfileEvent {
        event: "switchProfile",
        device: config.device.clone(),
        profile: profile.clone(),
    };

    if let Some(outbound) = OUTBOUND_EVENT_MANAGER.lock().await.as_mut() {
        match outbound.send_event(event).await {
            Ok(()) => *CURRENT.write().await = Some(profile),
            Err(error) => log::error!("Failed to switch profile: {error}"),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    simplelog::TermLogger::init(
        simplelog::LevelFilter::Info,
        simplelog::Config::default(),
        simplelog::TerminalMode::Stdout,
        simplelog::ColorChoice::Never,
    )
    .unwrap();

    init_plugin(GlobalEventHandler {}, ActionEventHandler {}).await?;

    Ok(())
}
