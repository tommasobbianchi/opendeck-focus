//! What is running on the *other* side of an ssh connection.
//!
//! The identity walk reads the local process tree, so a terminal holding an ssh session
//! resolves to `kitty:ssh` no matter what is running at the far end. There is no local process
//! to find: the interesting one is on another machine.
//!
//! One thing does cross the connection -- the title. A remote program sets it with an OSC
//! escape, ssh carries the bytes, and the terminal puts it in the window title, which the shell
//! extension already hands us alongside the class. So when the foreground program is a remote
//! shell, the title is the only evidence there is, and this module reads it.
//!
//! The rules are data, not code: a JSON file at `$XDG_CONFIG_HOME/opendeck-focus/remote.json`
//! adds or replaces them, so a new remote program needs an edit, not a release. Matching is
//! prefix and substring only -- no regular expressions, because a rule someone writes at
//! midnight should fail visibly rather than match everything.

use std::path::PathBuf;

/// Foreground programs that mean "the real one is elsewhere".
pub const REMOTE_SHELLS: &[&str] = &["ssh", "mosh-client", "mosh", "et", "eternal-terminal"];

#[derive(Debug, Clone, PartialEq)]
pub struct Rule {
    pub program: String,
    pub starts_with: Vec<String>,
    pub contains: Vec<String>,
}

/// Claude Code writes its session summary into the title, prefixed with a spinner glyph that
/// rotates while it works. Measured on 2026-08-20: "◐ Project files realignment".
fn built_in() -> Vec<Rule> {
    vec![Rule {
        program: "claude".to_owned(),
        starts_with: ["◐", "◑", "◒", "◓", "✻", "✽", "✳", "✢"]
            .iter()
            .map(|glyph| format!("{glyph} "))
            .collect(),
        contains: vec!["claude code".to_owned()],
    }]
}

fn config_path() -> PathBuf {
    let base = std::env::var("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into())).join(".config")
        });
    base.join("opendeck-focus").join("remote.json")
}

/// Rules from disk, appended to the built-in ones. A file that will not parse is reported and
/// ignored: a typo in a convenience should not cost you focus tracking.
pub fn rules() -> Vec<Rule> {
    let mut all = built_in();
    let path = config_path();
    let Ok(text) = std::fs::read_to_string(&path) else {
        return all;
    };
    match parse(&text) {
        Ok(extra) => all.extend(extra),
        Err(error) => log::warn!("ignoring {}: {error}", path.display()),
    }
    all
}

/// `[{"program": "vim", "starts_with": ["vim "], "contains": [" - VIM"]}]`
pub fn parse(text: &str) -> Result<Vec<Rule>, String> {
    let value: serde_json::Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let array = value.as_array().ok_or("expected a list of rules")?;
    let mut out = Vec::new();
    for entry in array {
        let program = entry
            .get("program")
            .and_then(|v| v.as_str())
            .ok_or("a rule needs a \"program\"")?;
        let list = |field: &str| -> Vec<String> {
            entry
                .get(field)
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|v| v.as_str()).map(str::to_owned).collect())
                .unwrap_or_default()
        };
        let rule = Rule {
            program: program.to_owned(),
            starts_with: list("starts_with"),
            contains: list("contains"),
        };
        if rule.starts_with.is_empty() && rule.contains.is_empty() {
            return Err(format!("rule for {program:?} matches nothing"));
        }
        out.push(rule);
    }
    Ok(out)
}

/// The program a title points at, if any rule recognises it.
///
/// Later rules win, so a user file overrides a built-in for the same title.
pub fn program_from_title(title: &str, rules: &[Rule]) -> Option<String> {
    let trimmed = title.trim();
    if trimmed.is_empty() {
        return None;
    }
    let lower = trimmed.to_lowercase();
    let mut found = None;
    for rule in rules {
        let hit = rule.starts_with.iter().any(|p| trimmed.starts_with(p.as_str()))
            || rule.contains.iter().any(|c| lower.contains(&c.to_lowercase()));
        if hit {
            found = Some(rule.program.clone());
        }
    }
    found
}

pub fn is_remote_shell(program: &str) -> bool {
    REMOTE_SHELLS.contains(&program)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_spinning_claude_is_recognised_through_ssh() {
        let rules = built_in();
        // Real titles, sampled from the session that found this bug.
        assert_eq!(program_from_title("◐ Project files realignment", &rules), Some("claude".into()));
        assert_eq!(program_from_title("◑ Project files realignment", &rules), Some("claude".into()));
    }

    #[test]
    fn an_ordinary_remote_shell_is_left_alone() {
        let rules = built_in();
        assert_eq!(program_from_title("tommaso@nativedev: ~", &rules), None);
        assert_eq!(program_from_title("~/projects", &rules), None);
        assert_eq!(program_from_title("", &rules), None);
        assert_eq!(program_from_title("   ", &rules), None);
    }

    #[test]
    fn a_glyph_without_its_space_does_not_count() {
        // "◐hello" is somebody's prompt, not Claude Code, which always writes "<glyph> <text>".
        assert_eq!(program_from_title("◐hello", &built_in()), None);
    }

    #[test]
    fn user_rules_extend_and_override_the_built_in_ones() {
        let mut rules = built_in();
        rules.extend(parse(r#"[{"program": "vim", "contains": [" - VIM"]}]"#).unwrap());
        assert_eq!(program_from_title("main.rs (~/src) - VIM", &rules), Some("vim".into()));

        rules.extend(parse(r#"[{"program": "kimi", "starts_with": ["◐ "]}]"#).unwrap());
        assert_eq!(program_from_title("◐ something", &rules), Some("kimi".into()),
                   "the later rule wins, so a user file can override a built-in");
    }

    #[test]
    fn a_rule_that_matches_nothing_is_refused_rather_than_matching_everything() {
        assert!(parse(r#"[{"program": "oops"}]"#).is_err());
        assert!(parse(r#"[{"starts_with": ["x"]}]"#).is_err());
        assert!(parse("not json").is_err());
        assert!(parse(r#"{"program": "x"}"#).is_err(), "a bare object is not a list of rules");
    }

    #[test]
    fn matching_is_case_insensitive_for_contains_and_exact_for_prefixes() {
        let rules = parse(r#"[{"program": "claude", "contains": ["Claude Code"]}]"#).unwrap();
        assert_eq!(program_from_title("running claude code now", &rules), Some("claude".into()));
    }
}
