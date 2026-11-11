//! Desktop shell. Its one job beyond showing the UI is to own the engine process:
//! start it with a fresh session token, tell the UI where it is, and stop it on exit.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent, State};

const READY_PREFIX: &str = "CLIPPER_ENGINE_READY ";
const ERROR_PREFIX: &str = "CLIPPER_ENGINE_ERROR ";

#[derive(Clone, Serialize)]
#[serde(tag = "state", rename_all = "lowercase")]
enum EngineStatus {
    Starting,
    Ready { port: u16, token: String },
    Failed { message: String },
}

struct Engine {
    status: Mutex<EngineStatus>,
    child: Mutex<Option<Child>>,
}

#[derive(Deserialize)]
struct ReadyLine {
    port: u16,
}

/// The UI polls this until the engine is ready, then talks to it directly over loopback.
#[tauri::command]
fn engine_status(engine: State<'_, Engine>) -> EngineStatus {
    engine.status.lock().unwrap().clone()
}

/// 32 random bytes, hex encoded. New on every launch; never written to disk.
fn new_session_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("could not generate a session token: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Debug builds run the engine from the repo's virtualenv. Release builds run the
/// PyInstaller build that the installer places in the app's resources.
fn engine_command(app: &AppHandle) -> Result<Command, String> {
    if cfg!(debug_assertions) {
        let _ = app;
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        let python = if cfg!(windows) {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        if !python.exists() {
            return Err(format!(
                "Python environment not found at {}. Run `uv sync` in the project folder.",
                python.display()
            ));
        }
        let mut command = Command::new(python);
        command.args(["-m", "engine"]).current_dir(root);
        Ok(command)
    } else {
        let name = if cfg!(windows) {
            "clipper-engine.exe"
        } else {
            "clipper-engine"
        };
        let resources = app
            .path()
            .resource_dir()
            .map_err(|e| format!("could not locate app resources: {e}"))?;
        let exe = resources.join("engine").join(name);
        if !exe.exists() {
            return Err(format!(
                "Engine not found at {}. Reinstall Clipper.",
                exe.display()
            ));
        }
        Ok(Command::new(exe))
    }
}

fn spawn_engine(app: &AppHandle, token: &str) -> Result<Child, String> {
    let mut command = engine_command(app)?;
    command
        .arg("--exit-on-stdin-close")
        // Environment, not argv: argv is visible to other processes on the machine.
        .env("CLIPPER_SESSION_TOKEN", token)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // The engine writes its own log file; an unread stderr pipe would block it.
        .stderr(if cfg!(debug_assertions) {
            Stdio::inherit()
        } else {
            Stdio::null()
        });
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
        .spawn()
        .map_err(|e| format!("could not start the engine: {e}"))
}

fn start_engine(app: AppHandle) {
    let set_status = {
        let app = app.clone();
        move |status: EngineStatus| *app.state::<Engine>().status.lock().unwrap() = status
    };
    let fail = |message: String| set_status(EngineStatus::Failed { message });

    let token = match new_session_token() {
        Ok(token) => token,
        Err(message) => return fail(message),
    };
    let mut child = match spawn_engine(&app, &token) {
        Ok(child) => child,
        Err(message) => return fail(message),
    };
    let stdout = child.stdout.take().expect("stdout was piped");
    *app.state::<Engine>().child.lock().unwrap() = Some(child);

    let mut announced = false;
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if announced {
            continue; // keep draining so the engine can never block on a full pipe
        }
        if let Some(json) = line.strip_prefix(READY_PREFIX) {
            match serde_json::from_str::<ReadyLine>(json) {
                Ok(ready) => set_status(EngineStatus::Ready {
                    port: ready.port,
                    token: token.clone(),
                }),
                Err(_) => fail("The engine sent an unreadable startup message.".into()),
            }
            announced = true;
        } else if let Some(reason) = line.strip_prefix(ERROR_PREFIX) {
            fail(format!("The engine could not start: {reason}"));
            announced = true;
        }
    }

    // stdout closed: the engine has exited.
    if !announced {
        fail("The engine exited before it was ready. See the engine log.".into());
    } else if matches!(
        *app.state::<Engine>().status.lock().unwrap(),
        EngineStatus::Ready { .. }
    ) {
        fail("The engine stopped unexpectedly. Restart Clipper.".into());
    }
}

/// Closing stdin asks the engine to shut down cleanly; kill it only if it ignores that.
fn stop_engine(app: &AppHandle) {
    let Some(mut child) = app.state::<Engine>().child.lock().unwrap().take() else {
        return;
    };
    drop(child.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Engine {
            status: Mutex::new(EngineStatus::Starting),
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![engine_status])
        .setup(|app| {
            let handle = app.handle().clone();
            thread::spawn(move || start_engine(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Clipper app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_engine(app);
            }
        });
}
