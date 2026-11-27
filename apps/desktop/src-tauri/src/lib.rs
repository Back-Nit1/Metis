//! Metis desktop client: native capture and display, authenticated server processing.
pub mod audio;
pub mod backend;
pub mod folders;
pub mod hosted;
pub mod hud;
pub mod insertion;
pub mod machine;
pub mod native;
pub mod session;
pub mod stack;

use serde::Serialize;
use session::{Emit, Handle, Msg, Snapshot};
use stack::{Stack, StackStatus};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Instant;
use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// Event the HUD listens to; payload is a sanitized [`Snapshot`].
pub const STATE_EVENT: &str = "capture:state";

#[derive(Default)]
struct AppState {
    stack: Arc<Mutex<Option<Stack>>>,
    hosted: Mutex<Option<Arc<hosted::Hosted>>>,
    startup_error: Mutex<Option<String>>,
    capture: Mutex<Option<(Handle, JoinHandle<()>)>>,
}

/// Publishes `capture:state` and drives the HUD window from the same snapshots.
struct TauriEmit {
    app: tauri::AppHandle,
    plan: Arc<Mutex<hud::Plan>>,
    t0: Instant,
}

impl TauriEmit {
    fn new(app: tauri::AppHandle) -> Self {
        Self {
            app,
            plan: Arc::default(),
            t0: Instant::now(),
        }
    }
}

fn apply_visibility(app: &tauri::AppHandle, v: Option<hud::Visibility>) {
    let (Some(v), Some(w)) = (v, app.get_webview_window(hud::LABEL)) else {
        return;
    };
    // `show` on a non-focusable window orders it front without activating the app.
    let _ = match v {
        hud::Visibility::Show => w.show(),
        hud::Visibility::Hide => w.hide(),
    };
}

impl Emit for TauriEmit {
    fn emit(&self, snap: &Snapshot) {
        let _ = self.app.emit(STATE_EVENT, snap);
        let now = self.t0.elapsed().as_millis() as u64;
        let v = self.plan.lock().unwrap().on_state(snap.state, now);
        apply_visibility(&self.app, v);
        if matches!(
            snap.state,
            session::State::Review | session::State::Saved | session::State::Error
        ) {
            // A stale timer is harmless: `on_tick` checks the dwell against the
            // newest terminal state, so it can never cut a newer one short.
            let (app, plan, t0) = (self.app.clone(), self.plan.clone(), self.t0);
            std::thread::spawn(move || {
                std::thread::sleep(hud::DWELL);
                let v = plan
                    .lock()
                    .unwrap()
                    .on_tick(t0.elapsed().as_millis() as u64);
                apply_visibility(&app, v);
            });
        }
    }
}

#[derive(Serialize)]
struct DesktopStatus {
    mode: &'static str,
    version: &'static str,
    /// Sanitised aggregate health; None only if startup itself failed.
    stack: Option<StackStatus>,
    error: Option<String>,
    authenticated: bool,
    recording_allowed: bool,
}

/// Camel-case, exactly the shape `apps/web/src/lib/desktop.ts` parses.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ConnectionDto {
    api_base_url: String,
    bearer: String,
    mode: &'static str,
}

#[tauri::command]
fn health() -> &'static str {
    "ok"
}

#[tauri::command]
fn desktop_status(state: tauri::State<'_, AppState>) -> DesktopStatus {
    let authenticated = state
        .hosted
        .lock()
        .ok()
        .and_then(|h| h.as_ref().map(|h| h.authenticated()))
        .unwrap_or(false);
    DesktopStatus {
        mode: "hosted",
        version: env!("CARGO_PKG_VERSION"),
        stack: state.stack.lock().unwrap().as_ref().map(Stack::status),
        error: state.startup_error.lock().unwrap().clone(),
        authenticated,
        recording_allowed: authenticated,
    }
}

/// Current capture snapshot, for a HUD that mounts after the last event.
#[tauri::command]
fn capture_state(state: tauri::State<'_, AppState>) -> Result<Snapshot, String> {
    match state.capture.lock().unwrap().as_ref() {
        Some((h, _)) => Ok(h.snapshot.lock().unwrap().clone()),
        None => Err("unavailable".into()),
    }
}

/// Explicit cancel: discards a recording, abandons in-flight work, dismisses an error.
#[tauri::command]
fn capture_cancel(state: tauri::State<'_, AppState>) {
    if let Some((h, _)) = state.capture.lock().unwrap().as_ref() {
        h.send(Msg::Cancel);
    }
}

/// Microphone onboarding: opens the mic once so macOS asks for consent.
#[tauri::command]
fn capture_prepare_mic(state: tauri::State<'_, AppState>) {
    if let Some((h, _)) = state.capture.lock().unwrap().as_ref() {
        h.send(Msg::PrepareMic);
    }
}

/// HUD "open in Metis": shows and focuses `main`, then tells it which capture
/// to open. The only input is an optional entity id; anything else is refused.
#[tauri::command]
fn show_main_window(app: tauri::AppHandle, capture_id: Option<String>) -> Result<(), String> {
    if capture_id
        .as_deref()
        .is_some_and(|c| !hud::valid_entity_id(c))
    {
        return Err("invalid capture id".into());
    }
    let main = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let _ = main.unminimize();
    main.show().map_err(|e| e.to_string())?;
    let _ = main.set_focus();
    let _ = app.emit_to(
        "main",
        hud::OPEN_REVIEW_EVENT,
        hud::OpenReview { capture_id },
    );
    Ok(())
}

/// Whether Ollama answers on loopback and has the embedding model. Errors only
/// when the configured endpoint itself is invalid.
#[tauri::command(async)]
fn ollama_status() -> Result<native::OllamaStatus, String> {
    Err("Model services run on the Metis server".into())
}

fn approved_folders_file(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join("approved-folders.json"))
}

/// Native folder dialog; `None` when the user cancels. A picked folder is
/// recorded as approved for `sync_folder`.
#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let dialog = app.clone();
    let picked =
        tauri::async_runtime::spawn_blocking(move || dialog.dialog().file().blocking_pick_folder())
            .await
            .ok()
            .flatten()?;
    let path = picked
        .into_path()
        .ok()?
        .into_os_string()
        .into_string()
        .ok()?;
    folders::approve(&approved_folders_file(&app).ok()?, &path).ok()?;
    Some(path)
}

/// List the connector's folders and upload what the server asks for. Every
/// folder must have been picked in the native dialog on this Mac.
#[tauri::command(async)]
fn sync_folder(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    connector_id: String,
    paths: Vec<String>,
) -> Result<folders::Report, String> {
    let approved = folders::approved(&approved_folders_file(&app)?);
    if paths.is_empty() || !paths.iter().all(|p| approved.contains(p)) {
        return Err("choose the folder with “Choose folder…” on this Mac first".into());
    }
    let hosted = state
        .hosted
        .lock()
        .map_err(|_| "unavailable")?
        .clone()
        .ok_or("unavailable")?;
    let backend = backend::HttpBackend::hosted_bound(hosted)?;
    folders::sync(&backend, &connector_id, &paths)
}

/// Reveal an absolute, existing path in Finder / Explorer / the file manager.
#[tauri::command(async)]
fn reveal_path(path: String) -> Result<(), String> {
    native::reveal_path(&path)
}

/// Rejects exactly `not_ready` while the stack starts, `unavailable` when it cannot.
#[tauri::command]
fn desktop_connection(state: tauri::State<'_, AppState>) -> Result<ConnectionDto, String> {
    let guard = state.hosted.lock().map_err(|_| "unavailable")?;
    let hosted = guard.as_ref().ok_or("unavailable")?;
    Ok(ConnectionDto {
        api_base_url: hosted.base().into(),
        bearer: String::new(),
        mode: "hosted",
    })
}

#[tauri::command]
fn desktop_set_session(
    state: tauri::State<'_, AppState>,
    token: Option<String>,
) -> Result<(), String> {
    let guard = state.hosted.lock().map_err(|_| "unavailable")?;
    let hosted = guard.as_ref().ok_or("unavailable")?;
    if hosted.set_token(token)? {
        if let Some((handle, _)) = state.capture.lock().map_err(|_| "unavailable")?.as_ref() {
            handle.send(Msg::Cancel);
        }
    }
    Ok(())
}

/// Visible startup error instead of a blank window.
fn error_script(msg: &str) -> String {
    let msg = serde_json::to_string(msg).unwrap();
    format!(
        "addEventListener('DOMContentLoaded',()=>{{const d=document.createElement('div');d.setAttribute('role','alert');d.style.cssText='position:fixed;inset:0 0 auto 0;z-index:99999;background:#7f1d1d;color:#fff;padding:12px;font:14px system-ui';d.textContent='Metis desktop: '+{msg};document.body.prepend(d)}})"
    )
}

/// Capture uses Cmd+Option+Space. Dictation uses Cmd+Option+D. Both have
/// separate press/release events and share the same gesture state machine.
fn register_hotkey(app: &tauri::AppHandle, handle: &Handle) {
    let result = app
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .map_err(|e| e.to_string())
        .and_then(|_| {
            let capture = Shortcut::new(Some(Modifiers::SUPER | Modifiers::ALT), Code::Space);
            let dictate = Shortcut::new(Some(Modifiers::SUPER | Modifiers::ALT), Code::KeyD);
            let capture_handle = handle.clone();
            app.global_shortcut()
                .on_shortcut(capture, move |_, _, event| {
                    capture_handle.send(match event.state() {
                        ShortcutState::Pressed => Msg::Down,
                        ShortcutState::Released => Msg::Up,
                    });
                })
                .map_err(|error| error.to_string())?;
            let dictate_handle = handle.clone();
            app.global_shortcut()
                .on_shortcut(dictate, move |_, _, event| {
                    dictate_handle.send(match event.state() {
                        ShortcutState::Pressed => Msg::DictateDown,
                        ShortcutState::Released => Msg::Up,
                    });
                })
                .map_err(|error| error.to_string())
        });
    if let Err(e) = result {
        handle.send(Msg::Fail {
            step: "shortcut",
            message: format!(
                "The Capture or Dictate shortcut could not be registered (another app may use it): {e}"
            ),
        });
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            health,
            desktop_status,
            desktop_connection,
            desktop_set_session,
            capture_state,
            capture_cancel,
            capture_prepare_mic,
            show_main_window,
            ollama_status,
            pick_folder,
            sync_folder,
            reveal_path
        ])
        .setup(|app| {
            let state = app.state::<AppState>();
            let data_dir = app.path().app_data_dir().map_err(|e| e.to_string());
            // No API, worker, model or database starts on this device.
            let hosted = match hosted::Hosted::from_env() {
                Ok(hosted) => Arc::new(hosted),
                Err(error) => {
                    *state
                        .startup_error
                        .lock()
                        .map_err(|_| "startup state unavailable")? = Some(error);
                    return Ok(());
                }
            };
            *state
                .hosted
                .lock()
                .map_err(|_| "startup state unavailable")? = Some(hosted.clone());
            // Hidden until capture leaves Idle; see `hud::Plan`. Not focusable, so
            // showing it never takes the keyboard from the app being dictated into.
            if let Err(e) =
                WebviewWindowBuilder::new(app, hud::LABEL, WebviewUrl::App(hud::ROUTE.into()))
                    .title("Metis HUD")
                    .inner_size(hud::WIDTH, hud::HEIGHT)
                    .resizable(false)
                    .decorations(false)
                    .always_on_top(true)
                    .skip_taskbar(true)
                    .focusable(false)
                    .focused(false)
                    .visible(false)
                    .accept_first_mouse(true)
                    .build()
            {
                eprintln!("metis: HUD window not created: {e}");
            }
            // The controller always exists so failures stay visible; it refuses
            // to record unless the stack reports `recording_allowed`.
            let (handle, join) = session::spawn(
                || Box::new(audio::CpalSource::default()),
                Arc::new(backend::HttpBackend::hosted(hosted)),
                Arc::new(insertion::SystemTextInserter),
                Arc::new(TauriEmit::new(app.handle().clone())),
                data_dir.ok().map(|d| d.join("mic_confirmed")),
            );
            register_hotkey(app.handle(), &handle);
            *state.capture.lock().unwrap() = Some((handle, join));
            Ok(())
        })
        .on_page_load(|w, _| {
            if w.label() != "main" {
                return;
            }
            let state = w.app_handle().state::<AppState>();
            let err = state.startup_error.lock().unwrap().clone();
            if let Some(e) = err {
                let _ = w.eval(error_script(&e));
            }
        })
        .build(tauri::generate_context!())
        .expect("error building Metis desktop")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // Release the microphone and in-flight work before the stack stops.
                if let Some((h, join)) = app.state::<AppState>().capture.lock().unwrap().take() {
                    h.send(Msg::Shutdown);
                    let _ = join.join();
                }
                if let Some(mut s) = app.state::<AppState>().stack.lock().unwrap().take() {
                    s.shutdown();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Documents the exact wire format the webview sees. Deliberately mixed:
    /// `capture:state` and `desktop_connection` are camelCase; `desktop_status`
    /// (and its `StackStatus`/`ServiceStatus`) keeps Rust snake_case; enum
    /// values (`state`, `phase`) are snake_case lowercase. Changing any of these
    /// breaks apps/web, so this test must be updated together with it.
    #[test]
    fn wire_format_is_pinned() {
        let snap = serde_json::to_value(session::Snapshot::default_for_test()).unwrap();
        let mut keys: Vec<_> = snap.as_object().unwrap().keys().cloned().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "adapterMode",
                "captureId",
                "elapsedMs",
                "error",
                "extractionMs",
                "locked",
                "micReady",
                "needsClarification",
                "previewProvisional",
                "previewTranscript",
                "releaseToTranscriptMs",
                "route",
                "state",
                "transcript",
                "truncated"
            ]
        );
        assert_eq!(snap["state"], "idle");

        let status = serde_json::to_value(DesktopStatus {
            mode: "debug",
            version: "0",
            stack: Some(StackStatus {
                state: "ready",
                recording_allowed: true,
                services: vec![],
            }),
            error: None,
            authenticated: false,
            recording_allowed: false,
        })
        .unwrap();
        assert_eq!(
            status,
            json!({"mode":"debug","version":"0","error":null,"authenticated":false,"recording_allowed":false,
                   "stack":{"state":"ready","recording_allowed":true,"services":[]}})
        );

        let conn = serde_json::to_value(ConnectionDto {
            api_base_url: "u".into(),
            bearer: "b".into(),
            mode: "local",
        })
        .unwrap();
        assert_eq!(conn, json!({"apiBaseUrl":"u","bearer":"b","mode":"local"}));
    }
}
