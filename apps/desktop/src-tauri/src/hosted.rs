//! Hosted server configuration and the current native user's session.
use std::sync::Mutex;

use url::Url;

const DEFAULT_SERVER: &str = "https://34-30-254-78.sslip.io";

#[derive(Default)]
struct Credentials {
    token: Option<String>,
    generation: u64,
}

/// One configured HTTPS origin and an in-memory session shared with native capture.
pub struct Hosted {
    base: String,
    credentials: Mutex<Credentials>,
}

impl Hosted {
    /// Reads a runtime override, then a build-time override, then the public default.
    pub fn from_env() -> Result<Self, String> {
        let server = std::env::var("METIS_SERVER_URL").unwrap_or_else(|_| {
            option_env!("METIS_SERVER_URL")
                .unwrap_or(DEFAULT_SERVER)
                .to_string()
        });
        Self::new(&server)
    }

    /// Accept only an HTTPS origin; paths, userinfo and URL parameters are invalid.
    pub fn new(server: &str) -> Result<Self, String> {
        let url = Url::parse(server).map_err(|_| "METIS_SERVER_URL must be an HTTPS origin")?;
        if url.scheme() != "https"
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.path() != "/"
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(
                "METIS_SERVER_URL must be an HTTPS origin without credentials or a path".into(),
            );
        }
        Ok(Self {
            base: url.origin().ascii_serialization(),
            credentials: Mutex::new(Credentials::default()),
        })
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    /// Replace the session token. A changed token invalidates ongoing operations.
    pub fn set_token(&self, token: Option<String>) -> Result<bool, String> {
        if token.as_ref().is_some_and(|value| {
            value.is_empty()
                || value.len() > 8192
                || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
        }) {
            return Err("invalid session token".into());
        }
        let mut credentials = self.credentials.lock().map_err(|_| "session unavailable")?;
        if credentials.token == token {
            return Ok(false);
        }
        credentials.token = token;
        credentials.generation = credentials.generation.wrapping_add(1);
        Ok(true)
    }

    pub fn authenticated(&self) -> bool {
        self.credentials.lock().is_ok_and(|c| c.token.is_some())
    }

    pub fn generation(&self) -> Result<u64, String> {
        let credentials = self.credentials.lock().map_err(|_| "session unavailable")?;
        credentials
            .token
            .as_ref()
            .ok_or("sign in to use voice capture")?;
        Ok(credentials.generation)
    }

    pub fn token(&self, generation: u64) -> Result<String, String> {
        let credentials = self.credentials.lock().map_err(|_| "session unavailable")?;
        if credentials.generation != generation {
            return Err("session changed; start voice capture again".into());
        }
        credentials
            .token
            .clone()
            .ok_or_else(|| "sign in to use voice capture".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_hosted_origin() {
        assert_eq!(
            Hosted::new("https://example.com:8443/").unwrap().base(),
            "https://example.com:8443"
        );
        for invalid in [
            "",
            "http://example.com",
            "https://user@example.com",
            "https://example.com/path",
            "https://example.com/?x=1",
            "https://example.com/#fragment",
        ] {
            assert!(Hosted::new(invalid).is_err(), "accepted {invalid}");
        }
    }

    #[test]
    fn token_changes_invalidate_bound_recordings() {
        let hosted = Hosted::new(DEFAULT_SERVER).unwrap();
        assert!(!hosted.authenticated());
        hosted.set_token(Some("first".into())).unwrap();
        let first = hosted.generation().unwrap();
        assert_eq!(hosted.token(first).unwrap(), "first");
        assert!(!hosted.set_token(Some("first".into())).unwrap());
        hosted.set_token(None).unwrap();
        hosted.set_token(Some("second".into())).unwrap();
        assert!(hosted.token(first).is_err());
        assert_eq!(
            hosted.token(hosted.generation().unwrap()).unwrap(),
            "second"
        );
    }
}
