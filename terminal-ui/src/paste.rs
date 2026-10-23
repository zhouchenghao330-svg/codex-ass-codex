//! Synchronous, local path classification keeps Paste → Enter ordered without
//! waiting for a service round trip. No clipboard polling or image decoding.
use std::path::{Path, PathBuf};
pub fn images(text: &str, cwd: &Path) -> Vec<String> {
    let raw = text.trim();
    if let Some(path) = resolve(raw.trim_matches(['\'', '"']), cwd) {
        return vec![path];
    }
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut chars = raw.chars().peekable();
    while let Some(c) = chars.next() {
        if Some(c) == quote {
            quote = None;
        } else if quote.is_none() && (c == '\'' || c == '"') {
            quote = Some(c);
        } else if c == '\\' && !cfg!(windows) && quote != Some('\'') {
            if let Some(next) = chars.next() {
                word.push(next);
            }
        } else if c.is_whitespace() && quote.is_none() {
            if !word.is_empty() {
                words.push(std::mem::take(&mut word));
            }
        } else {
            word.push(c);
        }
    }
    if quote.is_some() {
        return vec![];
    }
    if !word.is_empty() {
        words.push(word);
    }
    if words.is_empty() {
        return vec![];
    }
    words
        .iter()
        .map(|w| resolve(w, cwd))
        .collect::<Option<Vec<_>>>()
        .unwrap_or_default()
}
fn resolve(raw: &str, cwd: &Path) -> Option<String> {
    let decoded;
    let mut raw = raw;
    if let Some(value) = raw.strip_prefix("file://") {
        let value = if let Some(local) = value.strip_prefix("localhost/") {
            format!("/{local}")
        } else if value.starts_with('/') {
            value.to_owned()
        } else {
            return None;
        };
        let bytes = value.as_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            if bytes[i] == b'%' && i + 2 < bytes.len() {
                let pair = std::str::from_utf8(&bytes[i + 1..i + 3]).ok()?;
                out.push(u8::from_str_radix(pair, 16).ok()?);
                i += 3;
            } else {
                out.push(bytes[i]);
                i += 1;
            }
        }
        decoded = String::from_utf8(out).ok()?;
        raw = &decoded;
        if cfg!(windows) && raw.as_bytes().get(2) == Some(&b':') {
            raw = &raw[1..];
        }
    }
    let ext = Path::new(raw).extension()?.to_str()?.to_lowercase();
    if !["png", "jpg", "jpeg", "webp", "gif"].contains(&ext.as_str()) {
        return None;
    }
    let path = if let Some(rest) = raw.strip_prefix("~/") {
        PathBuf::from(std::env::var_os(if cfg!(windows) {
            "USERPROFILE"
        } else {
            "HOME"
        })?)
        .join(rest)
    } else {
        let p = PathBuf::from(raw);
        if p.is_absolute() { p } else { cwd.join(p) }
    };
    if path.is_file() {
        Some(path.canonicalize().ok()?.to_string_lossy().into_owned())
    } else {
        None
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn image_paths_are_atomic_but_prose_is_plain_text() {
        let dir = std::env::temp_dir().join(format!("codex-room-paste-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("图 片.png"), []).unwrap();
        assert_eq!(images("'图 片.png'", &dir).len(), 1);
        assert_eq!(images("图 片.png", &dir).len(), 1);
        assert_eq!(images("\"图 片.png\" '图 片.png'", &dir).len(), 2);
        assert!(images("看这个 图 片.png", &dir).is_empty());
        assert!(images("不存在.png", &dir).is_empty());
        // A Windows file URI is file:///C:/..., not file://C:\....
        let url_path = dir.join("图 片.png").to_string_lossy().replace('\\', "/");
        let uri = format!(
            "file://{}{}",
            if url_path.starts_with('/') { "" } else { "/" },
            url_path.replace(' ', "%20")
        );
        assert_eq!(images(&uri, &dir).len(), 1);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
