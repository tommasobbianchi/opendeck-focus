//! Pages of keys for one application, turned with the dial.
//!
//! Fifteen keys is not many for a CAD package: Onshape alone ships more than forty bindings,
//! in groups a person thinks of separately -- sketch tools, constraints, the solid features.
//! So an application can have several pages, and the deck's knob turns them.
//!
//! A page is not a new mechanism. The daemon publishes `google-chrome:onshape` for the first
//! and `google-chrome:onshape#2` for the second, OpenDeck maps each to its own profile exactly
//! as it maps everything else, and every piece downstream -- catalogues, icons, the picker --
//! keeps seeing nothing but an identity string.
//!
//! How many pages exist is not something this daemon should be told twice, so it counts the
//! mappings OpenDeck already has. Turning the dial past the last page comes back to the first,
//! rather than landing on a page nobody wrote.

use std::path::PathBuf;

/// `class#2`, `class#3`, ... The first page is the bare class, so that an application with one
/// page publishes exactly what it always did.
pub fn with_bank(class: &str, bank: usize) -> String {
    if bank == 0 || class.is_empty() {
        class.to_owned()
    } else {
        format!("{class}#{}", bank + 1)
    }
}

fn applications_path() -> PathBuf {
    let base = std::env::var("OPENDECK_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into()))
                .join(".config")
                .join("opendeck")
        });
    base.join("applications.json")
}

/// How many pages this application has, counting the mappings OpenDeck holds. Always at least 1.
pub fn count(class: &str) -> usize {
    let Ok(text) = std::fs::read_to_string(applications_path()) else {
        return 1;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return 1;
    };
    let Some(map) = value.as_object() else {
        return 1;
    };
    count_in(map.keys().map(String::as_str), class)
}

pub fn count_in<'a>(names: impl Iterator<Item = &'a str>, class: &str) -> usize {
    if class.is_empty() {
        return 1;
    }
    let prefix = format!("{class}#");
    let mut highest = 1;
    for name in names {
        if let Some(number) = name.strip_prefix(&prefix)
            && let Ok(parsed) = number.parse::<usize>()
            && parsed > highest
        {
            highest = parsed;
        }
    }
    highest
}

/// The next page, wrapping at the last one that exists.
pub fn advance(bank: usize, pages: usize, step: isize) -> usize {
    if pages <= 1 {
        return 0;
    }
    let pages = pages as isize;
    let next = (bank as isize + step).rem_euclid(pages);
    next as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_first_page_publishes_the_plain_class() {
        assert_eq!(with_bank("google-chrome:onshape", 0), "google-chrome:onshape");
        assert_eq!(with_bank("google-chrome:onshape", 1), "google-chrome:onshape#2");
        assert_eq!(with_bank("google-chrome:onshape", 3), "google-chrome:onshape#4");
    }

    #[test]
    fn an_empty_class_is_never_given_a_page_number() {
        // The empty class is how OpenDeck is told to fall back to its default profile.
        assert_eq!(with_bank("", 2), "");
    }

    #[test]
    fn pages_are_counted_from_the_mappings_that_exist() {
        let names = ["opendeck_default", "google-chrome", "google-chrome:onshape",
                     "google-chrome:onshape#2", "google-chrome:onshape#3", "kitty:claude"];
        assert_eq!(count_in(names.iter().copied(), "google-chrome:onshape"), 3);
        assert_eq!(count_in(names.iter().copied(), "kitty:claude"), 1, "one page is the default");
        assert_eq!(count_in(names.iter().copied(), "nothing"), 1);
    }

    #[test]
    fn a_similar_name_is_not_a_page_of_this_one() {
        let names = ["google-chrome", "google-chrome#2", "google-chrome:onshape"];
        assert_eq!(count_in(names.iter().copied(), "google-chrome:onshape"), 1);
        assert_eq!(count_in(names.iter().copied(), "google-chrome"), 2);
    }

    #[test]
    fn the_dial_wraps_rather_than_running_off_the_end() {
        assert_eq!(advance(0, 3, 1), 1);
        assert_eq!(advance(2, 3, 1), 0, "past the last page is the first");
        assert_eq!(advance(0, 3, -1), 2, "and backwards from the first is the last");
        assert_eq!(advance(2, 1, 1), 0, "one page means there is nowhere to turn to");
    }
}
