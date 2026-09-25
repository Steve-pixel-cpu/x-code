// pick_folder 经 window.xcodePickFolder() 从 http://127.0.0.1 页面调用——
// Tauri v2 把页面 origin 视为 remote, 自定义命令默认对 remote 拒绝
// （"not allowed by ACL"）。
//
// 注意必须用 AppManifest 而不是 InlinedPlugin: 应用命令按裸名
// ("pick_folder") 在 allowed_commands 里解析, 只有 __app-acl__（AppManifest）
// 生成的权限才映射到裸名; InlinedPlugin 生成的键是
// "plugin:<name>|<command>", 应用命令永远匹配不上。
// capability 里引用无前缀的 "allow-pick-folder" 即解析到 __app-acl__。
fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&[
                "pick_folder",
                "notify_desktop",
                "minimize_main",
                "toggle_maximize_main",
                "close_main",
                "start_drag_main",
                "open_pet_window",
                "close_pet",
                "start_drag_pet",
                "set_pet_click_through",
                "move_pet_window",
                "resize_pet_window",
            ])),
    )
    .expect("failed to run tauri-build");
}
