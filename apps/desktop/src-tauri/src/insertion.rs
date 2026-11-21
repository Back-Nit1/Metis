//! Focused-application text insertion.
//!
//! Dictation is finalized before this boundary. On macOS a fixed AppleScript
//! checks Accessibility access and refuses secure text fields, then performs a
//! paste while preserving and restoring the previous clipboard value. User
//! text is passed as an argv value, never interpolated into executable code.

use std::process::Command;

/// Inserts finalized dictation into the application that currently owns focus.
pub trait TextInserter: Send + Sync {
    /// Name of the frontmost application, used only to choose a cleanup style.
    fn focused_app(&self) -> Result<String, String>;
    /// Inserts finalized text. Implementations must refuse secure fields.
    fn insert(&self, text: &str) -> Result<(), String>;
}

/// Native operating-system insertion boundary.
#[derive(Default)]
pub struct SystemTextInserter;

#[cfg(target_os = "macos")]
const FOCUSED_APP_SCRIPT: &str = r#"
tell application "System Events"
    if UI elements enabled is false then error "accessibility-disabled"
    set targetProcess to first application process whose frontmost is true
    return name of targetProcess
end tell
"#;

#[cfg(target_os = "macos")]
const INSERT_SCRIPT: &str = r#"
on run argv
    set replacement to item 1 of argv
    tell application "System Events"
        if UI elements enabled is false then error "accessibility-disabled"
        set targetProcess to first application process whose frontmost is true
        set focusedElement to value of attribute "AXFocusedUIElement" of targetProcess
        set elementRole to value of attribute "AXRole" of focusedElement
        set elementSubrole to ""
        try
            set elementSubrole to value of attribute "AXSubrole" of focusedElement
        end try
        if elementRole is "AXSecureTextField" or elementSubrole is "AXSecureTextField" then error "secure-field"
    end tell
    set savedClipboard to the clipboard
    set the clipboard to replacement
    try
        tell application "System Events" to key code 9 using command down
        delay 0.15
    on error
        set the clipboard to savedClipboard
        error "paste-failed"
    end try
    set the clipboard to savedClipboard
end run
"#;

#[cfg(target_os = "macos")]
fn osascript(script: &str, args: &[&str]) -> Result<String, String> {
    let output = Command::new("/usr/bin/osascript")
        .arg("-e")
        .arg(script)
        .args(args)
        .output()
        .map_err(|_| "macOS Accessibility service could not be started".to_string())?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let message = if stderr.contains("accessibility-disabled") {
            "allow Metis in System Settings > Privacy & Security > Accessibility"
        } else if stderr.contains("secure-field") {
            "dictation is disabled in secure text fields"
        } else {
            "the focused application refused text insertion"
        };
        return Err(message.into());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

impl TextInserter for SystemTextInserter {
    fn focused_app(&self) -> Result<String, String> {
        #[cfg(target_os = "macos")]
        {
            let name = osascript(FOCUSED_APP_SCRIPT, &[])?;
            if name.is_empty() {
                return Err("no focused application is available".into());
            }
            Ok(name)
        }
        #[cfg(not(target_os = "macos"))]
        {
            Err("focused-app dictation is currently supported on macOS only".into())
        }
    }

    fn insert(&self, text: &str) -> Result<(), String> {
        if text.is_empty() {
            return Ok(());
        }
        #[cfg(target_os = "macos")]
        {
            osascript(INSERT_SCRIPT, &[text]).map(|_| ())
        }
        #[cfg(not(target_os = "macos"))]
        {
            Err("focused-app dictation is currently supported on macOS only".into())
        }
    }
}
