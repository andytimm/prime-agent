//! Local observation snapshots shared by the Agents view and session sidebar.
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;

use serde::Deserialize;
use uuid::Uuid;

const MAX_SNAPSHOT_BYTES: u64 = 128 * 1024;
const MAX_OWNERS: usize = 128;
const MAX_DIR_ENTRIES: usize = 64;
const MAX_JOBS_PER_OWNER: usize = 12;

pub(crate) type Snapshots = BTreeMap<String, Vec<Snapshot>>;

#[derive(Debug, PartialEq, Deserialize)]
pub(crate) struct Snapshot {
    version: u8,
    pub(crate) id: String,
    pub(crate) owner_session_id: String,
    pub(crate) name: String,
    pub(crate) provider: String,
    pub(crate) status: String,
    pub(crate) activity: Option<String>,
    pub(crate) updated_at: f64,
    pub(crate) messages: Vec<Message>,
}

#[derive(Debug, PartialEq, Deserialize)]
pub(crate) struct Message {
    pub(crate) direction: Direction,
    pub(crate) text: String,
    pub(crate) status: String,
}

#[derive(Debug, PartialEq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Direction {
    ToExternal,
    FromExternal,
}

#[derive(Default, Debug, PartialEq, Eq)]
pub(crate) struct DockCounts {
    pub(crate) running: usize,
    pub(crate) idle: usize,
    pub(crate) inactive: usize,
}

pub(crate) fn read_dock_counts(root: &Path, owner: &str, now: f64) -> DockCounts {
    let mut counts = DockCounts::default();
    if !canonical_uuid(owner) || !real_dir(root) {
        return counts;
    }
    let Some(entries) = owner_entries(root, owner) else {
        return counts;
    };
    // Unlike the Agents preview, every valid snapshot contributes to the dock.
    for row in entries.filter_map(|entry| read_entry(entry.ok()?, owner)) {
        match row.status.as_str() {
            "starting" | "running" if now - row.updated_at <= 45.0 => counts.running += 1,
            "idle" | "blocked" | "quota_blocked" => counts.idle += 1,
            _ => counts.inactive += 1,
        }
    }
    counts
}

fn canonical_uuid(id: &str) -> bool {
    Uuid::parse_str(id).is_ok_and(|uuid| uuid.to_string() == id)
}

fn clean(text: &str, limit: usize, multiline: bool) -> String {
    clean_chars(text, multiline).take(limit).collect()
}

fn clean_chars(text: &str, multiline: bool) -> impl Iterator<Item = char> + '_ {
    text.chars().filter_map(move |ch| match ch {
        '\n' if multiline => Some(ch),
        '\n' | '\t' => Some(' '),
        // Terminal controls, bidi format controls, and non-ASCII line separators.
        '\u{061c}'
        | '\u{200e}'
        | '\u{200f}'
        | '\u{202a}'..='\u{202e}'
        | '\u{2066}'..='\u{2069}' => None,
        '\u{2028}' | '\u{2029}' => Some(' '),
        ch if ch.is_control() => None,
        ch => Some(ch),
    })
}

fn real_dir(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_dir())
}

fn read_snapshot(path: &Path, owner: &str, id: &str) -> Option<Snapshot> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.is_file() || metadata.len() > MAX_SNAPSHOT_BYTES {
        return None;
    }
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(MAX_SNAPSHOT_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() as u64 > MAX_SNAPSHOT_BYTES {
        return None;
    }
    let mut row: Snapshot = serde_json::from_slice(&bytes).ok()?;
    if row.version != 1 || row.owner_session_id != owner || row.id != id || row.updated_at < 0.0 {
        return None;
    }
    row.name = clean(&row.name, 100, false);
    row.provider = clean(&row.provider, 48, false);
    if row.name.trim().is_empty() || row.provider.trim().is_empty() {
        return None;
    }
    row.status = clean(&row.status, 40, false);
    row.activity = row.activity.map(|text| clean(&text, 180, false));
    if row.messages.len() > 8 {
        row.messages.drain(..row.messages.len() - 8);
    }
    for message in &mut row.messages {
        const MESSAGE_CHAR_LIMIT: usize = 1600;
        message.text = {
            let mut chars = clean_chars(&message.text, /*multiline*/ true);
            let mut text: String = chars.by_ref().take(MESSAGE_CHAR_LIMIT).collect();
            if chars.next().is_some() {
                text.pop();
                text.push('…');
            }
            text
        };
        message.status = clean(&message.status, 32, false);
    }
    Some(row)
}

fn owner_entries(root: &Path, owner: &str) -> Option<fs::ReadDir> {
    let dir = root.join(owner);
    if !real_dir(&dir) {
        return None;
    }
    fs::read_dir(dir).ok()
}

fn read_entry(entry: fs::DirEntry, owner: &str) -> Option<Snapshot> {
    let path = entry.path();
    if path.extension()?.to_str()? != "json" {
        return None;
    }
    let id = path.file_stem()?.to_str()?;
    if !canonical_uuid(id) {
        return None;
    }
    read_snapshot(&path, owner, id)
}

pub(crate) fn read_snapshots(root: &Path, owners: Vec<String>) -> Snapshots {
    let mut snapshots = Snapshots::new();
    if !real_dir(root) {
        return snapshots;
    }
    for owner in owners
        .into_iter()
        .filter(|id| canonical_uuid(id))
        .take(MAX_OWNERS)
    {
        let Some(entries) = owner_entries(root, &owner) else {
            continue;
        };
        let mut jobs: Vec<_> = entries
            .take(MAX_DIR_ENTRIES)
            .filter_map(|entry| read_entry(entry.ok()?, &owner))
            .collect();
        jobs.sort_by(|a, b| {
            b.updated_at
                .total_cmp(&a.updated_at)
                .then_with(|| a.id.cmp(&b.id))
        });
        jobs.truncate(MAX_JOBS_PER_OWNER);
        if !jobs.is_empty() {
            snapshots.insert(owner, jobs);
        }
    }
    snapshots
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    const OWNER: &str = "00000000-0000-4000-8000-000000000001";

    fn write_snapshot(
        dir: &Path,
        id: u32,
        status: &str,
        updated_at: f64,
        messages: Value,
    ) -> String {
        let id = format!("00000000-0000-4000-8000-{id:012x}");
        let snapshot = json!({
            "version": 1,
            "id": id,
            "owner_session_id": OWNER,
            "name": "Claude",
            "provider": "claude-code",
            "status": status,
            "updated_at": updated_at,
            "messages": messages,
        });
        fs::write(dir.join(format!("{id}.json")), snapshot.to_string()).unwrap();
        id
    }

    #[test]
    fn dock_counts_live_jobs_outside_the_agents_preview_and_discovery_cap() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join(OWNER);
        fs::create_dir(&dir).unwrap();
        let oldest_live = write_snapshot(&dir, 0, "starting", 55.0, json!([]));
        for id in 1..=64 {
            write_snapshot(&dir, id, "running", 70.0, json!([]));
        }
        write_snapshot(&dir, 65, "running", 54.0, json!([]));
        write_snapshot(&dir, 66, "blocked", 99.0, json!([]));
        write_snapshot(&dir, 67, "quota_blocked", 99.0, json!([]));
        write_snapshot(&dir, 68, "completed", 100.0, json!([]));

        let preview = read_snapshots(root.path(), vec![OWNER.to_string()]);
        assert_eq!(preview[OWNER].len(), 12);
        assert!(preview[OWNER].iter().all(|row| row.id != oldest_live));
        assert_eq!(
            read_dock_counts(root.path(), OWNER, 100.0),
            DockCounts {
                running: 65,
                idle: 2,
                inactive: 2,
            }
        );
    }

    #[test]
    fn direct_message_truncation_marks_only_text_beyond_the_sanitized_char_limit() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join(OWNER);
        fs::create_dir(&dir).unwrap();
        let exact = "界".repeat(1600);
        let id = write_snapshot(
            &dir,
            1,
            "idle",
            100.0,
            json!([
                {"direction": "from_external", "status": "answered", "text": format!("{exact}\u{1b}")},
                {"direction": "from_external", "status": "answered", "text": format!("{exact}z")},
            ]),
        );
        let snapshot = read_snapshot(&dir.join(format!("{id}.json")), OWNER, &id).unwrap();
        assert_eq!(snapshot.messages[0].text, exact);
        assert_eq!(snapshot.messages[1].text, format!("{}…", "界".repeat(1599)));
    }
}
