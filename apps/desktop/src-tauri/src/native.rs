//! OS integration for the Sources screen: Ollama probe and reveal-in-file-manager.
//! (The folder picker lives in `lib.rs` because it needs the dialog plugin handle.)
use crate::stack::LocalAi;
use serde::Serialize;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct OllamaStatus {
    pub available: bool,
    pub models: Vec<String>,
    pub embed_model_present: bool,
}

/// GET `/api/tags` on the loopback Ollama endpoint. Unreachable or malformed
/// answers report `available: false`; nothing is guessed.
pub fn ollama_status(ai: &LocalAi) -> OllamaStatus {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_millis(1500)))
        .build()
        .into();
    let models = agent
        .get(format!("{}/api/tags", ai.endpoint))
        .call()
        .ok()
        .and_then(|mut r| r.body_mut().read_json::<serde_json::Value>().ok())
        .and_then(|v| parse_tags(&v));
    match models {
        Some(models) => OllamaStatus {
            embed_model_present: model_present(&models, &ai.embed_model),
            available: true,
            models,
        },
        None => OllamaStatus {
            available: false,
            models: vec![],
            embed_model_present: false,
        },
    }
}

/// Model names from an `/api/tags` body (`{"models":[{"name":..}]}`).
pub fn parse_tags(v: &serde_json::Value) -> Option<Vec<String>> {
    let list = v.get("models")?.as_array()?;
    Some(
        list.iter()
            .filter_map(|m| m.get("name")?.as_str().map(str::to_string))
            .collect(),
    )
}

/// `nomic-embed-text` matches `nomic-embed-text:latest`; a tagged want matches exactly.
pub fn model_present(models: &[String], want: &str) -> bool {
    models
        .iter()
        .any(|m| m == want || (!want.contains(':') && m.strip_prefix(want) == Some(":latest")))
}

/// Absolute, existing path (canonicalised). Never executed or opened, only revealed.
pub fn validate_reveal(path: &str) -> Result<PathBuf, String> {
    let p = PathBuf::from(path);
    if path.is_empty() || path.contains('\0') || !p.is_absolute() {
        return Err("path must be absolute".into());
    }
    let p = p
        .canonicalize()
        .map_err(|_| "path does not exist".to_string())?;
    #[cfg(windows)]
    let p = {
        // explorer.exe does not understand the `\\?\` verbatim prefix.
        let s = p.to_string_lossy();
        if s.contains('"') {
            return Err("path is not supported".into());
        }
        PathBuf::from(s.strip_prefix(r"\\?\").unwrap_or(&s).to_string())
    };
    Ok(p)
}

/// Shows the item selected in Finder / Explorer, or opens its folder on Linux.
pub fn reveal_path(path: &str) -> Result<(), String> {
    let p = validate_reveal(path)?;
    let fail = |e: std::io::Error| format!("could not open the file manager: {e}");
    #[cfg(target_os = "macos")]
    {
        let ok = Command::new("/usr/bin/open")
            .arg("-R")
            .arg(&p)
            .status()
            .map_err(fail)?
            .success();
        ok.then_some(())
            .ok_or("Finder could not reveal the path".into())
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // explorer exits 1 even on success, so only spawn failures count.
        Command::new("explorer.exe")
            .raw_arg(format!("/select,\"{}\"", p.display()))
            .spawn()
            .map(|_| ())
            .map_err(fail)
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        // No portable "select" on Linux: open the containing folder instead.
        let dir = if p.is_dir() {
            p.clone()
        } else {
            p.parent().map(PathBuf::from).unwrap_or(p.clone())
        };
        let ok = Command::new("xdg-open")
            .arg(&dir)
            .status()
            .map_err(fail)?
            .success();
        ok.then_some(())
            .ok_or("the file manager could not open the folder".into())
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = (p, fail, Command::new);
        Err("revealing files is not supported on this OS yet".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn tags_and_model_matching() {
        let v = json!({"models":[{"name":"nomic-embed-text:latest"},{"name":"llama3:8b"},{"x":1}]});
        let m = parse_tags(&v).unwrap();
        assert_eq!(m, ["nomic-embed-text:latest", "llama3:8b"]);
        assert!(model_present(&m, "nomic-embed-text"));
        assert!(model_present(&m, "llama3:8b"));
        assert!(!model_present(&m, "llama3"));
        assert!(!model_present(&m, "nomic-embed"));
        assert!(parse_tags(&json!({"error":"x"})).is_none());
    }

    #[test]
    fn ollama_down_is_unavailable() {
        // Port 1 on loopback: nothing listens there.
        let ai = LocalAi::resolve(|k| {
            (k == "METIS_LOCAL_LLM_ENDPOINT").then(|| "http://127.0.0.1:1".into())
        })
        .unwrap();
        let s = ollama_status(&ai);
        assert!(!s.available && s.models.is_empty() && !s.embed_model_present);
    }

    #[test]
    fn reveal_validation() {
        assert!(validate_reveal("").is_err());
        assert!(validate_reveal("relative/path").is_err());
        assert!(validate_reveal("-R").is_err());
        assert!(validate_reveal("/no/such/metis/path\0x").is_err());
        let missing = std::env::temp_dir().join("metis-definitely-missing-9f3a");
        assert!(validate_reveal(missing.to_str().unwrap()).is_err());
        let here = std::env::temp_dir();
        let ok = validate_reveal(here.to_str().unwrap()).unwrap();
        assert!(ok.is_absolute() && ok.exists());
    }
}
