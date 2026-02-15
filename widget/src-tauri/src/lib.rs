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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    log_to_file("=== ARIA Widget starting ===");

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::default().build())
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
                            if window.is_visible().unwrap_or(false) {
                                let _ = window.hide();
                            } else {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
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

            // Register global shortcut (Ctrl+Space)
            use tauri_plugin_global_shortcut::ShortcutState;
            match app.global_shortcut().on_shortcut("ctrl+space", move |app: &AppHandle, _shortcut, event| {
                if event.state == ShortcutState::Pressed {
                    if let Some(window) = app.get_webview_window("main") {
                        if window.is_visible().unwrap_or(false) {
                            let _ = window.hide();
                        } else {
                            let _ = window.show();
                            let _ = window.set_focus();
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
