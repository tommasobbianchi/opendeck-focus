use serde::Deserialize;
use std::path::PathBuf;

/// Which profile to select for a given focused window.
///
/// OpenDeck has its own application-to-profile mapping, but its window watcher only supports
/// X11 and KDE, so on GNOME Wayland that UI silently does nothing. Rather than pretend to
/// drive a mechanism we cannot reach, this plugin keeps its own small rules file.
#[derive(Debug, Deserialize)]
pub struct Config {
    /// OpenDeck device id, e.g. "n1-81D0DA783809". Find it in the plugin log.
    pub device: String,

    /// Selected when no rule matches.
    #[serde(default = "default_profile")]
    pub default_profile: String,

    #[serde(default)]
    pub rules: Vec<Rule>,
}

fn default_profile() -> String {
    "Default".to_string()
}

#[derive(Debug, Deserialize)]
pub struct Rule {
    /// Matched case-insensitively against the window's WM class.
    ///
    /// A substring match, because WM classes are inconsistent in practice: Firefox reports
    /// "firefox" on X11 but "org.mozilla.firefox" under some Wayland builds, and Electron apps
    /// vary by packaging.
    pub wm_class: String,
    pub profile: String,
}

impl Config {
    pub fn path() -> PathBuf {
        let base = std::env::var("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".config")
            });

        base.join("opendeck-focus").join("rules.json")
    }

    pub fn load() -> Result<Config, String> {
        let path = Self::path();

        let text = std::fs::read_to_string(&path)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;

        serde_json::from_str(&text).map_err(|e| format!("cannot parse {}: {e}", path.display()))
    }

    /// First matching rule wins, so more specific rules belong earlier in the file.
    pub fn profile_for(&self, wm_class: &str) -> &str {
        let wm_class = wm_class.to_ascii_lowercase();

        self.rules
            .iter()
            .find(|rule| wm_class.contains(&rule.wm_class.to_ascii_lowercase()))
            .map(|rule| rule.profile.as_str())
            .unwrap_or(&self.default_profile)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Config {
        serde_json::from_str(
            r#"{
                "device": "n1-TEST",
                "default_profile": "Default",
                "rules": [
                    { "wm_class": "code", "profile": "VSCode" },
                    { "wm_class": "firefox", "profile": "Web" }
                ]
            }"#,
        )
        .unwrap()
    }

    #[test]
    fn matches_are_case_insensitive_substrings() {
        let c = config();
        assert_eq!(c.profile_for("Code"), "VSCode");
        assert_eq!(c.profile_for("org.mozilla.firefox"), "Web");
        assert_eq!(c.profile_for("FIREFOX"), "Web");
    }

    #[test]
    fn unmatched_and_empty_classes_fall_back_to_the_default() {
        let c = config();
        assert_eq!(c.profile_for("gnome-terminal"), "Default");
        // An empty wm_class means nothing has focus; it must not match a rule by accident.
        assert_eq!(c.profile_for(""), "Default");
    }

    #[test]
    fn first_matching_rule_wins() {
        let c: Config = serde_json::from_str(
            r#"{
                "device": "d",
                "rules": [
                    { "wm_class": "code", "profile": "Specific" },
                    { "wm_class": "c", "profile": "Broad" }
                ]
            }"#,
        )
        .unwrap();
        assert_eq!(c.profile_for("code"), "Specific");
        assert_eq!(c.profile_for("chromium"), "Broad");
        // default_profile is optional and defaults to "Default"
        assert_eq!(c.profile_for("zzz"), "Default");
    }
}
