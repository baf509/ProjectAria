use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    AppHandle, Manager,
};
use tauri_plugin_global_shortcut::GlobalShortcutExt;
use std::fs;
use std::io::Write;

fn log_to_file(msg: &str) {
    if let Some(home) = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
    {
        let log_path = std::path::PathBuf::from(home).join("aria-widget.log");
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&log_path) {
            let _ = writeln!(f, "{}", msg);
        }
    }
}

/// Reposition the window to the bottom-right of the current monitor.
/// The window "grows upward" — its bottom edge stays anchored.
#[tauri::command]
fn reposition_window(window: tauri::WebviewWindow, height: f64) {
    let monitor = match window.current_monitor() {
        Ok(Some(m)) => m,
        _ => return,
    };

    let scale = monitor.scale_factor();
    let monitor_size = monitor.size();
    let monitor_pos = monitor.position();

    // Work in logical pixels
    let screen_w = monitor_size.width as f64 / scale;
    let screen_h = monitor_size.height as f64 / scale;
    let origin_x = monitor_pos.x as f64 / scale;
    let origin_y = monitor_pos.y as f64 / scale;

    let win_w: f64 = 400.0;
    let margin: f64 = 20.0;
    let taskbar_margin: f64 = 48.0;

    // Clamp height
    let max_h = screen_h * 0.85;
    let min_h: f64 = 300.0;
    let clamped_h = height.clamp(min_h, max_h);

    // Bottom-right, above taskbar
    let x = origin_x + screen_w - win_w - margin;
    let y = origin_y + screen_h - clamped_h - taskbar_margin;

    let _ = window.set_size(tauri::LogicalSize::new(win_w, clamped_h));
    let _ = window.set_position(tauri::LogicalPosition::new(x, y));
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    log_to_file("=== ARIA Widget starting ===");

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::default().build())
        .invoke_handler(tauri::generate_handler![reposition_window])
        .setup(|app| {
            log_to_file("setup: entered");

            // Build tray menu
            let show_i = MenuItem::with_id(app, "show", "Show ARIA", true, None::<&str>)?;
            let new_i = MenuItem::with_id(app, "new_chat", "New Chat", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &new_i, &quit_i])?;
            log_to_file("setup: menu created");

            // Build tray icon (embedded at compile time for reliability across platforms)
            let icon = match Image::from_bytes(include_bytes!("../icons/icon.png")) {
                Ok(i) => {
                    log_to_file("setup: icon loaded OK");
                    i
                }
                Err(e) => {
                    log_to_file(&format!("setup: icon load FAILED: {e}"));
                    return Err(e.into());
                }
            };

            let _tray = match TrayIconBuilder::new()
                .icon(icon)
                .menu(&menu)
                .tooltip("ARIA - AI Assistant")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "new_chat" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                            let _ = window.eval("window.__ariaNewChat?.()");
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { .. } = event {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            // Always show on tray click — don't toggle, because on
                            // Windows the focus race between tray and window causes
                            // the window to immediately disappear after showing.
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)
            {
                Ok(t) => {
                    log_to_file("setup: tray built OK");
                    t
                }
                Err(e) => {
                    log_to_file(&format!("setup: tray build FAILED: {e}"));
                    return Err(e.into());
                }
            };

            // Register global shortcut (Ctrl+Shift+Space)
            use tauri_plugin_global_shortcut::ShortcutState;
            match app.global_shortcut().on_shortcut("ctrl+shift+space", move |app: &AppHandle, _shortcut, event| {
                if event.state == ShortcutState::Pressed {
                    if let Some(window) = app.get_webview_window("main") {
                        if window.is_visible().unwrap_or(false) {
                            let _ = window.eval("window.__ariaAnimateHide?.()");
                        } else {
                            let _ = window.show();
                            let _ = window.set_focus();
                            let _ = window.eval("window.__ariaAnimateShow?.()");
                        }
                    }
                }
            }) {
                Ok(_) => log_to_file("setup: global shortcut registered OK"),
                Err(e) => log_to_file(&format!("setup: global shortcut FAILED: {e}")),
            }

            log_to_file("setup: complete");
            Ok(())
        });

    log_to_file("run: calling .run()");
    match builder.run(tauri::generate_context!()) {
        Ok(_) => log_to_file("run: exited normally"),
        Err(e) => log_to_file(&format!("run: FAILED: {e}")),
    }
}
