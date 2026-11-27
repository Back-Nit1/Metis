//! The Rust side of the capture API flow:
//! `POST /v1/captures` -> upload -> complete -> poll, and authenticated dictation.
use crate::hosted::Hosted;
use crate::stack::Stack;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ApiError {
    /// Transport failure or 502/503: safe to retry with the same idempotency keys.
    pub retryable: bool,
    pub message: String,
}

impl ApiError {
    fn new(retryable: bool, message: impl Into<String>) -> Self {
        Self {
            retryable,
            message: message.into(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct CreateReq {
    pub client_capture_id: String,
    pub timezone: String,
    pub captured_at: String,
    pub expected_bytes: usize,
    pub duration_seconds: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct UploadTarget {
    pub url: String,
    #[serde(default = "put")]
    pub method: String,
    #[serde(default)]
    pub headers: HashMap<String, String>,
}

fn put() -> String {
    "PUT".into()
}

#[derive(Clone, Debug, Deserialize)]
pub struct Created {
    pub capture_id: String,
    pub revision: u64,
    pub upload: Option<UploadTarget>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct DictationSession {
    pub session_id: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct DictationPartial {
    pub text: String,
    pub provisional: bool,
    pub at_seconds: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct DictationChunk {
    pub seconds: f64,
    pub partial: Option<DictationPartial>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct DictationResult {
    pub text: String,
    pub raw_text: String,
    pub no_speech: bool,
    pub speech: Value,
}

pub trait Backend: Send + Sync {
    /// Bind an operation to its current authenticated session, when applicable.
    fn bind(&self) -> Result<Option<Arc<dyn Backend>>, String> {
        Ok(None)
    }
    /// Ok only when the stack reports `recording_allowed`; otherwise why not.
    fn ready(&self) -> Result<(), String>;
    fn create(&self, req: &CreateReq) -> Result<Created, ApiError>;
    fn upload(&self, target: &UploadTarget, wav: &[u8]) -> Result<(), ApiError>;
    fn complete(&self, id: &str, revision: u64, bytes: usize, key: &str) -> Result<(), ApiError>;
    fn get(&self, id: &str) -> Result<Value, ApiError>;
    /// Best-effort removal of an abandoned capture.
    fn delete(&self, id: &str);
    fn dictation_create(&self, _app: &str) -> Result<DictationSession, ApiError> {
        Err(ApiError::new(false, "dictation is unavailable"))
    }
    fn dictation_audio(&self, _id: &str, _pcm: &[u8]) -> Result<DictationChunk, ApiError> {
        Err(ApiError::new(false, "dictation is unavailable"))
    }
    fn dictation_finish(&self, _id: &str) -> Result<DictationResult, ApiError> {
        Err(ApiError::new(false, "dictation is unavailable"))
    }
    fn dictation_cancel(&self, _id: &str) {}
}

/// Native HTTP client for either a hosted account or an explicit local test stack.
pub struct HttpBackend {
    endpoint: Runtime,
    agent: ureq::Agent,
}

#[derive(Clone)]
enum Runtime {
    Local(Arc<Mutex<Option<Stack>>>),
    Hosted {
        hosted: Arc<Hosted>,
        generation: Option<u64>,
    },
}

struct Endpoint<'a> {
    base: &'a str,
    bearer: &'a str,
}

impl HttpBackend {
    /// Retained for the isolated local-stack integration tests.
    pub fn new(stack: Arc<Mutex<Option<Stack>>>) -> Self {
        Self::with_runtime(Runtime::Local(stack))
    }

    pub fn hosted(hosted: Arc<Hosted>) -> Self {
        Self::with_runtime(Runtime::Hosted {
            hosted,
            generation: None,
        })
    }

    /// Bound to the current session: a sign-out or account switch stops it.
    pub fn hosted_bound(hosted: Arc<Hosted>) -> Result<Self, String> {
        let generation = hosted.generation()?;
        Ok(Self::with_runtime(Runtime::Hosted {
            hosted,
            generation: Some(generation),
        }))
    }

    /// `POST /v1/connectors/{id}/manifest`: which listed files the server needs.
    pub fn folder_manifest(&self, connector_id: &str, listing: Value) -> Result<Value, ApiError> {
        self.send(
            "POST",
            &format!("/v1/connectors/{connector_id}/manifest"),
            None,
            &HashMap::new(),
            Body::Json(listing),
        )
    }

    /// `PUT /v1/connectors/{id}/files?path=..&modified_ns=..` with the raw bytes.
    pub fn folder_upload(
        &self,
        connector_id: &str,
        path: &str,
        modified_ns: u64,
        bytes: &[u8],
    ) -> Result<(), ApiError> {
        let query = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("path", path)
            .append_pair("modified_ns", &modified_ns.to_string())
            .finish();
        self.send(
            "PUT",
            &format!("/v1/connectors/{connector_id}/files?{query}"),
            None,
            &HashMap::new(),
            Body::Bytes(bytes, "application/octet-stream"),
        )
        .map(|_| ())
    }

    fn with_runtime(endpoint: Runtime) -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .max_redirects(0)
            .timeout_global(Some(Duration::from_secs(30)))
            .build()
            .into();
        Self { endpoint, agent }
    }

    fn conn(&self) -> Result<(String, String), ApiError> {
        match &self.endpoint {
            Runtime::Local(stack) => match stack
                .lock()
                .map_err(|_| ApiError::new(false, "local API unavailable"))?
                .as_ref()
            {
                Some(s) => s
                    .connection()
                    .map(|c| (c.api_base_url, c.bearer))
                    .map_err(|e| ApiError::new(e == "not_ready", format!("local API {e}"))),
                None => Err(ApiError::new(false, "local API unavailable")),
            },
            Runtime::Hosted { hosted, generation } => {
                let generation = generation
                    .ok_or_else(|| ApiError::new(false, "voice capture has no session"))?;
                let token = hosted
                    .token(generation)
                    .map_err(|e| ApiError::new(false, e))?;
                Ok((hosted.base().to_string(), token))
            }
        }
    }

    fn speech_conn(&self) -> Result<(String, String), ApiError> {
        match &self.endpoint {
            Runtime::Hosted { .. } => self.conn(),
            Runtime::Local(stack) => match stack
                .lock()
                .map_err(|_| ApiError::new(false, "local speech unavailable"))?
                .as_ref()
            {
                Some(s) => s
                    .speech_connection()
                    .map(|c| (c.base_url, c.bearer))
                    .map_err(|e| ApiError::new(e == "not_ready", format!("local speech {e}"))),
                None => Err(ApiError::new(false, "local speech unavailable")),
            },
        }
    }

    fn still_bound(&self) -> Result<(), ApiError> {
        if let Runtime::Hosted {
            hosted,
            generation: Some(generation),
        } = &self.endpoint
        {
            hosted
                .token(*generation)
                .map_err(|e| ApiError::new(false, e))?;
        }
        Ok(())
    }

    /// One request; maps HTTP status to ApiError carrying only the server's
    /// `code`/`message` fields, never headers or bodies we did not parse.
    fn send(
        &self,
        method: &str,
        path_or_url: &str,
        key: Option<&str>,
        extra: &HashMap<String, String>,
        body: Body<'_>,
    ) -> Result<Value, ApiError> {
        let (base, bearer) = self.conn()?;
        self.send_at(
            Endpoint {
                base: &base,
                bearer: &bearer,
            },
            method,
            path_or_url,
            key,
            extra,
            body,
        )
    }

    fn send_speech(&self, method: &str, path: &str, body: Body<'_>) -> Result<Value, ApiError> {
        let (base, bearer) = self.speech_conn()?;
        self.send_at(
            Endpoint {
                base: &base,
                bearer: &bearer,
            },
            method,
            path,
            None,
            &HashMap::new(),
            body,
        )
    }

    fn send_at(
        &self,
        endpoint: Endpoint<'_>,
        method: &str,
        path_or_url: &str,
        key: Option<&str>,
        extra: &HashMap<String, String>,
        body: Body<'_>,
    ) -> Result<Value, ApiError> {
        let base = url::Url::parse(endpoint.base).map_err(bad)?;
        let url = base.join(path_or_url).map_err(bad)?;
        // Reject origin confusion, userinfo, and redirects before attaching the token.
        if url.origin() != base.origin()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(ApiError::new(
                false,
                "refusing an upload target outside the server origin",
            ));
        }
        let mut req = ureq::http::Request::builder()
            .method(method)
            .uri(url.as_str())
            .header("Authorization", format!("Bearer {}", endpoint.bearer));
        if let Some(k) = key {
            req = req.header("Idempotency-Key", k);
        }
        for (k, v) in extra {
            if k.eq_ignore_ascii_case("authorization") || k.eq_ignore_ascii_case("host") {
                return Err(ApiError::new(
                    false,
                    "invalid server-supplied upload header",
                ));
            }
            req = req.header(k.as_str(), v.as_str());
        }
        let resp = match body {
            Body::None => self.agent.run(req.body(()).map_err(bad)?),
            Body::Json(v) => self.agent.run(
                req.header("Content-Type", "application/json")
                    .body(serde_json::to_vec(&v).map_err(bad)?)
                    .map_err(bad)?,
            ),
            Body::Bytes(b, ct) => self.agent.run(
                req.header("Content-Type", ct)
                    .body(b.to_vec())
                    .map_err(bad)?,
            ),
        };
        let mut resp = resp.map_err(|e| ApiError::new(true, format!("connection failed: {e}")))?;
        self.still_bound()?;
        let status = resp.status().as_u16();
        let text = resp.body_mut().read_to_string().unwrap_or_default();
        if (200..300).contains(&status) {
            return Ok(serde_json::from_str(&text).unwrap_or(Value::Null));
        }
        let parsed: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        let code = parsed["code"].as_str().unwrap_or("error");
        let msg = parsed["message"].as_str().unwrap_or("request failed");
        Err(ApiError::new(
            matches!(status, 502 | 503),
            format!("{status} {code}: {}", clip(msg, 200)),
        ))
    }
}

enum Body<'a> {
    None,
    Json(Value),
    Bytes(&'a [u8], &'a str),
}

fn bad(e: impl std::fmt::Display) -> ApiError {
    ApiError::new(false, e.to_string())
}

pub fn clip(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

impl Backend for HttpBackend {
    fn bind(&self) -> Result<Option<Arc<dyn Backend>>, String> {
        match &self.endpoint {
            Runtime::Hosted { hosted, .. } => {
                Ok(Some(Arc::new(Self::with_runtime(Runtime::Hosted {
                    hosted: hosted.clone(),
                    generation: Some(hosted.generation()?),
                }))))
            }
            Runtime::Local(_) => Ok(None),
        }
    }

    fn ready(&self) -> Result<(), String> {
        match &self.endpoint {
            Runtime::Hosted { hosted, generation } => match generation {
                Some(generation) => hosted.token(*generation).map(|_| ()),
                None => hosted.generation().map(|_| ()),
            },
            Runtime::Local(stack) => {
                let guard = stack
                    .lock()
                    .map_err(|_| "startup_failed: local stack unavailable")?;
                let Some(stack) = guard.as_ref() else {
                    return Err("startup_failed: the local stack did not start".into());
                };
                let st = stack.status();
                if st.recording_allowed {
                    Ok(())
                } else {
                    Err(format!("not_ready: stack is {}", st.state))
                }
            }
        }
    }

    fn create(&self, r: &CreateReq) -> Result<Created, ApiError> {
        let v = self.send(
            "POST",
            "/v1/captures",
            Some(&format!("capture:{}", r.client_capture_id)),
            &HashMap::new(),
            Body::Json(json!({
                "kind": "audio",
                "capture_timezone": r.timezone,
                "captured_at": r.captured_at,
                "content_type": "audio/wav",
                "expected_bytes": r.expected_bytes,
                "duration_seconds": r.duration_seconds,
                "client_capture_id": r.client_capture_id,
            })),
        )?;
        serde_json::from_value(v).map_err(bad)
    }

    fn upload(&self, t: &UploadTarget, wav: &[u8]) -> Result<(), ApiError> {
        self.send(
            &t.method,
            &t.url,
            None,
            &t.headers,
            Body::Bytes(wav, "audio/wav"),
        )
        .map(|_| ())
    }

    fn complete(&self, id: &str, revision: u64, bytes: usize, key: &str) -> Result<(), ApiError> {
        self.send(
            "POST",
            &format!("/v1/captures/{id}/complete"),
            Some(&format!("complete:{key}")),
            &HashMap::new(),
            Body::Json(json!({"expected_revision": revision, "actual_bytes": bytes})),
        )
        .map(|_| ())
    }

    fn get(&self, id: &str) -> Result<Value, ApiError> {
        self.send(
            "GET",
            &format!("/v1/captures/{id}"),
            None,
            &HashMap::new(),
            Body::None,
        )
    }

    fn delete(&self, id: &str) {
        let _ = self.send(
            "DELETE",
            &format!("/v1/captures/{id}"),
            None,
            &HashMap::new(),
            Body::None,
        );
    }

    fn dictation_create(&self, app: &str) -> Result<DictationSession, ApiError> {
        let preferences = self.send("GET", "/v1/preferences", None, &HashMap::new(), Body::None)?;
        let flow = preferences
            .get("flow")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let value = self.send_speech(
            "POST",
            "/v1/flow/sessions",
            Body::Json(json!({"app": app, "settings": flow})),
        )?;
        serde_json::from_value(value).map_err(bad)
    }

    fn dictation_audio(&self, id: &str, pcm: &[u8]) -> Result<DictationChunk, ApiError> {
        let value = self.send_speech(
            "POST",
            &format!("/v1/flow/sessions/{id}/audio"),
            Body::Bytes(pcm, "application/octet-stream"),
        )?;
        serde_json::from_value(value).map_err(bad)
    }

    fn dictation_finish(&self, id: &str) -> Result<DictationResult, ApiError> {
        let value = self.send_speech(
            "POST",
            &format!("/v1/flow/sessions/{id}/finish"),
            Body::Json(json!({})),
        )?;
        serde_json::from_value(value).map_err(bad)
    }

    fn dictation_cancel(&self, id: &str) {
        let _ = self.send_speech("DELETE", &format!("/v1/flow/sessions/{id}"), Body::None);
    }
}
