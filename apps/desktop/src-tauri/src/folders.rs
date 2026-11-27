//! Hosted folder sync (ADR 0006): the server never reads this Mac's disk, so
//! the app lists a folder the user picked, sends the listing, and uploads only
//! the files the server asks for. The server re-applies every exclude.
use crate::backend::HttpBackend;
use serde::Serialize;
use serde_json::{json, Value};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// Server limit on one listing.
pub const MAX_FILES: usize = 20_000;
const SNIFF_BYTES: usize = 8192;
/// Never listed. Dot-names cover .ssh, .gnupg, .env*, .git, .venv, .DS_Store.
const SKIP_NAMES: &[&str] = &["node_modules", "__pycache__", "AppData", "Keychains"];
const SECRET_SUFFIXES: &[&str] = &[".pem", ".key", ".p12", ".pfx"];
const SECRET_PREFIXES: &[&str] = &["id_rsa", "id_ed25519"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub path: String,
    pub size: u64,
    pub modified_ns: u64,
}

#[derive(Debug, Default, Serialize, PartialEq, Eq)]
pub struct Report {
    pub listed: usize,
    pub uploaded: usize,
    pub failed: usize,
    pub tombstoned: u64,
    pub rejected: u64,
}

fn skipped(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    name.starts_with('.')
        || SKIP_NAMES.contains(&name)
        || SECRET_SUFFIXES.iter().any(|s| lower.ends_with(s))
        || SECRET_PREFIXES.iter().any(|p| lower.starts_with(p))
}

/// Text and PDF only, like the server's parser; a NUL early on means binary.
// ponytail: reads 8 KiB per file each sync; cache by mtime if huge folders drag.
fn indexable(path: &Path) -> bool {
    if path
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("pdf"))
    {
        return true;
    }
    let mut head = [0u8; SNIFF_BYTES];
    match fs::File::open(path).and_then(|mut f| f.read(&mut head)) {
        Ok(n) => !head[..n].contains(&0),
        Err(_) => false,
    }
}

/// Files under `roots`, never following symlinks (a link could reach ~/.ssh).
pub fn list(roots: &[String]) -> Result<Vec<Entry>, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let mut out = Vec::new();
    let mut stack: Vec<PathBuf> = roots.iter().map(PathBuf::from).collect();
    while let Some(dir) = stack.pop() {
        if home.as_ref().is_some_and(|h| dir == h.join("Library")) {
            continue;
        }
        let Ok(read) = fs::read_dir(&dir) else {
            continue;
        };
        for item in read.flatten() {
            let name = item.file_name().to_string_lossy().into_owned();
            let Ok(meta) = item.path().symlink_metadata() else {
                continue;
            };
            if skipped(&name) || meta.file_type().is_symlink() {
                continue;
            }
            let path = item.path();
            if meta.is_dir() {
                stack.push(path);
            } else if meta.is_file() && indexable(&path) {
                let modified_ns = meta
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                    .map_or(0, |d| d.as_nanos() as u64);
                out.push(Entry {
                    path: path.to_string_lossy().into_owned(),
                    size: meta.len(),
                    modified_ns,
                });
                if out.len() > MAX_FILES {
                    return Err(format!(
                        "more than {MAX_FILES} files; choose a smaller folder"
                    ));
                }
            }
        }
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

/// Roots the user picked in the native dialog. Only these may be synced, so a
/// page script cannot make the app upload a folder the user never chose.
pub fn approved(file: &Path) -> Vec<String> {
    fs::read(file)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

pub fn approve(file: &Path, root: &str) -> Result<(), String> {
    let mut roots = approved(file);
    if !roots.iter().any(|r| r == root) {
        roots.push(root.to_string());
    }
    if let Some(dir) = file.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    fs::write(file, serde_json::to_vec(&roots).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

/// One sync pass for a connector whose folders are `roots`.
pub fn sync(backend: &HttpBackend, connector_id: &str, roots: &[String]) -> Result<Report, String> {
    let entries = list(roots)?;
    let files: Vec<Value> = entries
        .iter()
        .map(|e| json!({"path": e.path, "size_bytes": e.size, "modified_ns": e.modified_ns}))
        .collect();
    let answer = backend
        .folder_manifest(connector_id, json!({ "files": files }))
        .map_err(|e| e.message)?;
    let mut report = Report {
        listed: entries.len(),
        tombstoned: answer["tombstoned"].as_u64().unwrap_or(0),
        rejected: answer["rejected"].as_u64().unwrap_or(0),
        ..Report::default()
    };
    let wanted = answer["upload"].as_array().cloned().unwrap_or_default();
    for path in wanted.iter().filter_map(Value::as_str) {
        // Only upload what we listed: the server cannot name another file.
        let Some(entry) = entries.iter().find(|e| e.path == path) else {
            report.failed += 1;
            continue;
        };
        match fs::read(&entry.path) {
            Ok(bytes) => {
                match backend.folder_upload(connector_id, &entry.path, entry.modified_ns, &bytes) {
                    Ok(()) => report.uploaded += 1,
                    Err(e) if e.message.starts_with("401") => return Err(e.message),
                    Err(_) => report.failed += 1,
                }
            }
            Err(_) => report.failed += 1,
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_text_files_and_skips_secrets_links_and_binaries() {
        let dir = std::env::temp_dir().join(format!("metis-folders-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("sub")).unwrap();
        fs::create_dir_all(dir.join(".ssh")).unwrap();
        fs::create_dir_all(dir.join("node_modules")).unwrap();
        fs::write(dir.join("a.md"), "hello").unwrap();
        fs::write(dir.join("sub/b.txt"), "world").unwrap();
        fs::write(dir.join(".ssh/id_rsa"), "secret").unwrap();
        fs::write(dir.join("server.pem"), "secret").unwrap();
        fs::write(dir.join("node_modules/x.js"), "x").unwrap();
        fs::write(dir.join("img.png"), [0x89, 0x50, 0x00, 0x01]).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(dir.join("a.md"), dir.join("link.md")).unwrap();

        let root = dir.to_string_lossy().into_owned();
        let names: Vec<String> = list(&[root])
            .unwrap()
            .into_iter()
            .map(|e| {
                e.path
                    .trim_start_matches(&*dir.to_string_lossy())
                    .to_string()
            })
            .collect();
        assert_eq!(names, vec!["/a.md", "/sub/b.txt"]);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn approval_list_round_trips() {
        let file = std::env::temp_dir().join(format!("metis-approved-{}.json", std::process::id()));
        let _ = fs::remove_file(&file);
        assert!(approved(&file).is_empty());
        approve(&file, "/Users/a/Notes").unwrap();
        approve(&file, "/Users/a/Notes").unwrap();
        assert_eq!(approved(&file), vec!["/Users/a/Notes".to_string()]);
        fs::remove_file(&file).unwrap();
    }
}
