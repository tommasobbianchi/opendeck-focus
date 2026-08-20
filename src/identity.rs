//! Composite identity for the focused window.
//!
//! A terminal is not one application: `kitty` running `claude` and `kitty` running `vim` must
//! publish different WM_CLASS values to OpenDeck. When the focused window is a terminal, we
//! walk its process tree for the foreground process group and, when that is not a plain shell,
//! publish `class:program`.
//!
//! KNOWN LIMITATION: the walk is anchored on the window PID, so a terminal that serves several
//! windows from one process (gnome-terminal-server, kitty in single-instance mode) yields the
//! same identity for all of its windows. The per-window fix is an app-specific query such as
//! `kitty @ ls`, not this walk.

use std::collections::VecDeque;
use std::path::Path;

pub const TERMINAL_CLASSES: &[&str] = &[
    "kitty",
    "gnome-terminal",
    "gnome-terminal-server",
    "terminal",
    "alacritty",
    "foot",
    "footclient",
    "wezterm",
    "wezterm-gui",
    "konsole",
    "xterm",
    "terminator",
    "tilix",
    "ptyxis",
    "blackbox",
    "contour",
    "rio",
];

pub const SHELLS: &[&str] = &[
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "elvish", "xonsh",
];

pub const RUNTIMES: &[&str] = &[
    "python", "python3", "node", "nodejs", "deno", "bun", "ruby", "perl", "java", "dotnet",
];

const MAX_VISITED: usize = 256;
const MAX_DEPTH: u32 = 8;

/// Case-insensitive; also matches when `wm_class` ends with a `TERMINAL_CLASSES` entry after
/// the last '.', so "org.gnome.Terminal" matches "terminal".
pub fn is_terminal(wm_class: &str) -> bool {
    let lower = wm_class.to_ascii_lowercase();
    let tail: &str = lower.rsplit_once('.').map(|(_, s)| s).unwrap_or(&lower);
    TERMINAL_CLASSES.iter().any(|c| *c == tail || *c == lower.as_str())
}

/// "kitty" + a claude in the foreground -> "kitty:claude"; anything unresolved -> wm_class alone.
///
/// `title` matters only when the foreground program turns out to be an ssh session: the
/// process running at the far end is not in this machine's process tree, and the title is the
/// one thing it can say for itself. See [`crate::remote`].
pub fn resolve(wm_class: &str, pid: u32, title: &str) -> String {
    // A rule written for this application wins outright: a browser is one WM_CLASS whether you
    // are in Gmail, Onshape or YouTube, and only the title tells them apart. Nothing in the
    // process tree can, so there is nothing to weigh this against.
    let rules = crate::titles::rules();
    if let Some(program) = crate::titles::program_from_title(title, Some(wm_class), &rules) {
        return format!("{wm_class}:{program}");
    }
    // pid 0 or 1 is not a real terminal window; walking init would surface an unrelated
    // foreground process anywhere on the system.
    if !is_terminal(wm_class) || pid <= 1 {
        return wm_class.to_owned();
    }
    let program = foreground_program(pid);
    composite(wm_class, through_ssh(program, title))
}

/// Replace a remote shell with what the title says is running inside it, when anything does.
///
/// Falling back to "ssh" is deliberate: a remote shell nobody has written a rule for is still
/// honestly an ssh session, and publishing that is better than publishing the terminal alone.
fn through_ssh(program: Option<String>, title: &str) -> Option<String> {
    let name = program?;
    if !crate::titles::is_remote_shell(&name) {
        return Some(name);
    }
    Some(crate::titles::program_from_title(title, None, &crate::titles::rules()).unwrap_or(name))
}

/// Parse one line of /proc/<pid>/stat -> (pgrp, tpgid). Must survive a comm containing spaces
/// and parentheses. Fields are 1-indexed as in proc(5): pgrp is field 5, tpgid is field 8.
pub fn parse_stat(line: &str) -> Option<(i32, i32)> {
    line.find('(')?;
    let close = line.rfind(')')?;
    let fields: Vec<&str> = line[close + 1..].split_whitespace().collect();
    // fields[0] is state (3), fields[1] ppid (4), fields[2] pgrp (5), fields[5] tpgid (8).
    if fields.len() < 6 {
        return None;
    }
    let pgrp = fields[2].parse::<i32>().ok()?;
    let tpgid = fields[5].parse::<i32>().ok()?;
    Some((pgrp, tpgid))
}

/// comm plus the /proc cmdline (NUL-separated, as read from disk) -> the name to publish.
pub fn program_name(comm: &str, cmdline: &[u8]) -> Option<String> {
    let comm = comm.trim();
    if comm.is_empty() {
        return None;
    }
    if !RUNTIMES.iter().any(|r| *r == comm) {
        return Some(comm.to_owned());
    }
    let arg = cmdline.split(|b| *b == 0).nth(1)?;
    if arg.is_empty() {
        return None;
    }
    let arg = std::str::from_utf8(arg).ok()?;
    let base = Path::new(arg).file_name()?.to_str()?;
    Some(base.rsplit_once('.').map(|(stem, _)| stem).unwrap_or(base).to_owned())
}

/// Breadth-first walk of the descendants of `pid`, returning the name of the foreground process
/// group (tpgid > 0 && pgrp == tpgid), preferring the deepest such process.
fn foreground_program(root: u32) -> Option<String> {
    let mut queue: VecDeque<(u32, u32)> = VecDeque::new();
    queue.push_back((root, 0));
    let mut visited: Vec<u32> = vec![root];
    let mut best: Option<(u32, Option<String>)> = None;

    while let Some((pid, depth)) = queue.pop_front() {
        for child in read_children(pid) {
            if visited.contains(&child) || visited.len() >= MAX_VISITED || depth + 1 > MAX_DEPTH {
                continue;
            }
            visited.push(child);

            if let Some((pgrp, tpgid)) = read_stat(child)
                && tpgid > 0
                && pgrp == tpgid
            {
                let name = read_name(child);
                if best.as_ref().map(|(d, _)| depth + 1 > *d).unwrap_or(true) {
                    best = Some((depth + 1, name));
                }
            }

            queue.push_back((child, depth + 1));
        }
    }

    best.and_then(|(_, name)| name)
}

fn read_children(pid: u32) -> Vec<u32> {
    let mut out = Vec::new();
    let task_dir = format!("/proc/{pid}/task");
    let Ok(entries) = std::fs::read_dir(&task_dir) else {
        return out;
    };
    for entry in entries.flatten() {
        if let Ok(content) = std::fs::read_to_string(entry.path().join("children")) {
            for token in content.split_whitespace() {
                if let Ok(p) = token.parse::<u32>() {
                    out.push(p);
                }
            }
        }
    }
    out
}

fn read_stat(pid: u32) -> Option<(i32, i32)> {
    let content = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    parse_stat(&content)
}

fn read_name(pid: u32) -> Option<String> {
    let comm = std::fs::read_to_string(format!("/proc/{pid}/comm")).ok()?;
    let cmdline = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    program_name(&comm, &cmdline)
}

/// Sanitise `name`: keep only ASCII alphanumerics, '-', '_' and '.', truncate to 32 chars.
fn sanitise(name: String) -> Option<String> {
    let cleaned: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_' || *c == '.')
        .take(32)
        .collect();
    if cleaned.is_empty() {
        None
    } else {
        Some(cleaned)
    }
}

fn is_shell(name: &str) -> bool {
    let stripped = name.strip_prefix('-').unwrap_or(name);
    let lower = stripped.to_ascii_lowercase();
    SHELLS.iter().any(|s| *s == lower.as_str())
}

/// Turn a resolved foreground name into the published class, applying the shell filter.
fn composite(wm_class: &str, name: Option<String>) -> String {
    match name.and_then(sanitise) {
        Some(name) if is_shell(&name) => wm_class.to_owned(),
        Some(name) => format!("{wm_class}:{name}"),
        None => wm_class.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_ssh_session_is_named_by_what_the_title_says_runs_inside_it() {
        // The process tree only ever shows ssh: the real program is on another machine.
        assert_eq!(
            through_ssh(Some("ssh".into()), "◐ Project files realignment"),
            Some("claude".into())
        );
    }

    #[test]
    fn an_unrecognised_remote_session_stays_honestly_ssh() {
        assert_eq!(through_ssh(Some("ssh".into()), "tommaso@nativedev: ~"), Some("ssh".into()));
        assert_eq!(through_ssh(Some("ssh".into()), ""), Some("ssh".into()));
    }

    #[test]
    fn a_local_program_ignores_the_title_completely() {
        // A local vim whose title happens to spin is still vim.
        assert_eq!(through_ssh(Some("vim".into()), "◐ Project files"), Some("vim".into()));
        assert_eq!(through_ssh(None, "◐ anything"), None);
    }

    #[test]
    fn parse_stat_plain() {
        let line = "1234 (bash) S 1 1200 1200 34816 1300 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0";
        assert_eq!(parse_stat(line), Some((1200, 1300)));
    }

    #[test]
    fn parse_stat_comm_with_spaces_and_parens() {
        let line = "1234 (my (weird) proc) S 1 1200 1200 34816 1300";
        assert_eq!(parse_stat(line), Some((1200, 1300)));
    }

    #[test]
    fn parse_stat_malformed() {
        assert_eq!(parse_stat("not a stat line"), None);
        assert_eq!(parse_stat("1234 (unclosed S 1"), None);
        assert_eq!(parse_stat("1234 (bash) S"), None);
    }

    #[test]
    fn terminal_detection() {
        assert!(is_terminal("kitty"));
        assert!(is_terminal("org.gnome.Terminal"));
        assert!(is_terminal("Alacritty"));
        assert!(!is_terminal("google-chrome"));
        assert!(!is_terminal(""));
    }

    #[test]
    fn program_name_plain_passes_through() {
        assert_eq!(program_name("vim", b""), Some("vim".to_owned()));
    }

    #[test]
    fn program_name_runtime_uses_script_basename() {
        assert_eq!(
            program_name("python3", b"python3\0/home/x/foo.py\0--flag\0"),
            Some("foo".to_owned())
        );
    }

    #[test]
    fn program_name_runtime_without_arg_is_none() {
        assert_eq!(program_name("python3", b"python3\0"), None);
        assert_eq!(program_name("python3", b""), None);
    }

    #[test]
    fn program_name_blank_comm_is_none() {
        assert_eq!(program_name("", b""), None);
        assert_eq!(program_name("   ", b"python3\0foo.py\0"), None);
    }

    #[test]
    fn resolve_non_terminal_returns_class() {
        assert_eq!(resolve("google-chrome", 1234, ""), "google-chrome");
    }

    #[test]
    fn resolve_zero_pid_returns_class() {
        assert_eq!(resolve("kitty", 0, ""), "kitty");
        assert_eq!(resolve("kitty", 1, ""), "kitty");
    }

    #[test]
    fn shell_filter_keeps_bare_class() {
        assert_eq!(composite("kitty", Some("bash".to_owned())), "kitty");
        assert_eq!(composite("kitty", Some("-zsh".to_owned())), "kitty");
        assert_eq!(composite("kitty", Some("claude".to_owned())), "kitty:claude");
        assert_eq!(composite("kitty", None), "kitty");
    }

    #[test]
    fn sanitise_drops_invalid_chars() {
        assert_eq!(sanitise("claude-code".to_owned()), Some("claude-code".to_owned()));
        assert_eq!(sanitise("a/b?c".to_owned()), Some("abc".to_owned()));
        assert_eq!(sanitise("///".to_owned()), None);
    }
}
