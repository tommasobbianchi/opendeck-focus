//! Remembers every identity the daemon has published.
//!
//! The picker in `opendeck-shortcuts` has to write a WM_CLASS into OpenDeck's
//! `applications.json`, and only this daemon knows what that string actually is: a friendly
//! name like `orca` produces a mapping that never fires, because what gets published is
//! `OrcaSlicer` -- or `OrcaBelt2608`, or `kitty:claude`. So write the published names down
//! where anything else can read them, rather than making every consumer guess.

use std::collections::BTreeMap;
use std::path::PathBuf;

/// Identities kept on disk. A terminal contributes one entry per program run inside it, so
/// this grows slowly but forever; keeping the most recent few hundred is plenty to offer a
/// picker, and bounds a file that nothing ever prunes.
const MAX_ENTRIES: usize = 200;

pub fn path() -> PathBuf {
    let cache = std::env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into())).join(".cache"));
    cache.join("opendeck-focus").join("seen.json")
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Merge `identity` into the map read from `file`, and write it back.
///
/// Errors are logged and swallowed: a deck that cannot write a cache file is still a working
/// deck, and this is a convenience for other tools rather than something focus depends on.
pub fn record(identity: &str) {
    let file = path();
    let mut seen = read(&file);
    if !merge(&mut seen, identity, now()) {
        return;
    }
    if let Err(error) = write(&file, &seen) {
        log::warn!("Could not record seen identity in {}: {error}", file.display());
    }
}

/// Add `identity` to `seen`, returning whether the map changed and so needs writing back.
///
/// The empty class is published on purpose -- it tells OpenDeck to fall back to its default
/// profile -- but it is not an application anyone can map, so it is not remembered.
fn merge(seen: &mut BTreeMap<String, u64>, identity: &str, at: u64) -> bool {
    if identity.is_empty() {
        return false;
    }
    seen.insert(identity.to_owned(), at);
    prune(seen);
    true
}

fn read(file: &PathBuf) -> BTreeMap<String, u64> {
    let Ok(text) = std::fs::read_to_string(file) else {
        return BTreeMap::new();
    };
    serde_json::from_str(&text).unwrap_or_default()
}

/// Drop the oldest entries once the map outgrows [`MAX_ENTRIES`].
fn prune(seen: &mut BTreeMap<String, u64>) {
    if seen.len() <= MAX_ENTRIES {
        return;
    }
    let mut by_age: Vec<(u64, String)> = seen.iter().map(|(k, v)| (*v, k.clone())).collect();
    by_age.sort();
    for (_, key) in by_age.into_iter().take(seen.len() - MAX_ENTRIES) {
        seen.remove(&key);
    }
}

/// Write via a temporary file in the same directory, so a reader never sees half a document.
fn write(file: &PathBuf, seen: &BTreeMap<String, u64>) -> std::io::Result<()> {
    if let Some(parent) = file.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let temporary = file.with_extension("json.tmp");
    std::fs::write(&temporary, serde_json::to_vec_pretty(seen)?)?;
    std::fs::rename(&temporary, file)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("opendeck-focus-seen-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_recorded_identity_survives_a_reread() {
        let file = temp_dir("roundtrip").join("seen.json");
        let mut seen = BTreeMap::new();
        seen.insert("OrcaSlicer".to_owned(), 10);
        seen.insert("kitty:claude".to_owned(), 20);
        write(&file, &seen).unwrap();
        assert_eq!(read(&file), seen);
    }

    #[test]
    fn an_unreadable_or_corrupt_file_reads_as_empty_rather_than_panicking() {
        let dir = temp_dir("corrupt");
        assert!(read(&dir.join("missing.json")).is_empty());
        let broken = dir.join("broken.json");
        std::fs::write(&broken, b"{not json").unwrap();
        assert!(read(&broken).is_empty());
    }

    #[test]
    fn pruning_keeps_the_most_recently_seen() {
        let mut seen: BTreeMap<String, u64> = (0..MAX_ENTRIES as u64 + 5)
            .map(|i| (format!("app{i}"), i))
            .collect();
        prune(&mut seen);
        assert_eq!(seen.len(), MAX_ENTRIES);
        assert!(!seen.contains_key("app0"), "oldest should have gone");
        assert!(seen.contains_key(&format!("app{}", MAX_ENTRIES + 4)), "newest should stay");
    }

    #[test]
    fn an_empty_identity_is_not_worth_remembering() {
        let mut seen = BTreeMap::new();
        assert!(!merge(&mut seen, "", 1), "empty class should not be recorded");
        assert!(seen.is_empty());
        assert!(merge(&mut seen, "OrcaSlicer", 1));
        assert_eq!(seen.get("OrcaSlicer"), Some(&1));
    }
}
