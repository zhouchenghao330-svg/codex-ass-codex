use serde_json::Value;
use std::{
    env,
    io::{self, BufRead, BufReader, Write},
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::{Duration, Instant},
};
pub struct Bridge {
    child: Child,
    pub events: Receiver<Value>,
    tx: Option<Sender<Value>>,
}
impl Bridge {
    pub fn start(args: &[String]) -> io::Result<Self> {
        let root = env::var("CODEX_ROOM_ROOT").unwrap_or_else(|_| {
            env!("CARGO_MANIFEST_DIR")
                .trim_end_matches("/terminal-ui")
                .to_owned()
        });
        let python = env::var("CODEX_ROOM_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut child = Command::new(python)
            .args(["-u", "-m", "codex_room.backend"])
            .args(args)
            .env("PYTHONPATH", root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::other("service stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::other("service stdout"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| io::Error::other("service stderr"))?;
        let (tx, rx) = mpsc::channel::<Value>();
        let (out_tx, events) = mpsc::channel();
        thread::spawn(move || {
            for message in rx {
                if serde_json::to_writer(&mut stdin, &message).is_err()
                    || stdin.write_all(b"\n").is_err()
                    || stdin.flush().is_err()
                {
                    break;
                }
            }
        });
        let messages = out_tx.clone();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else {
                    break;
                };
                match serde_json::from_str(&line) {
                    Ok(value) => {
                        if messages.send(value).is_err() {
                            return;
                        }
                    }
                    Err(_) => {
                        let _=messages.send(serde_json::json!({"type":"notice","text":"会话服务返回了无法解析的数据"}));
                    }
                }
            }
            let _ = messages
                .send(serde_json::json!({"type":"fatal","text":"会话服务已退出；可退出后重启"}));
        });
        // Drain diagnostics without writing raw stderr into the terminal.
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                if line.is_err() {
                    break;
                }
            }
        });
        Ok(Self {
            child,
            events,
            tx: Some(tx),
        })
    }
    pub fn send(&self, value: Value) {
        if let Some(tx) = &self.tx {
            let _ = tx.send(value);
        }
    }
    pub fn close(&mut self) {
        if self.tx.is_none() {
            return;
        }
        self.send(serde_json::json!({"type":"shutdown"}));
        self.tx.take();
        let start = Instant::now();
        while start.elapsed() < Duration::from_secs(35) {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(20));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
impl Drop for Bridge {
    fn drop(&mut self) {
        self.close();
    }
}
