use std::ffi::OsStr;

use ark::console::{BrowserPromptMode, SessionMode};
use ark::repos::DefaultRepos;

pub fn run(connection_path: &OsStr) -> Result<(), String> {
    let connection_path = connection_path
        .to_str()
        .ok_or_else(|| String::from("ark connection path is not UTF-8"))?;

    ark::signals::initialize_signal_block();
    ark::logger::init(None, None);
    ark::traps::register_trap_handlers();
    install_panic_hook();

    let (connection_file, registration_file) = amalthea::kernel::read_connection(connection_path);
    let r_args = vec![
        String::from("--no-save"),
        String::from("--no-restore-data"),
        String::from("--interactive"),
    ];

    ark::start::start_kernel_with_browser_prompt(
        connection_file,
        registration_file,
        r_args,
        None,
        SessionMode::Console,
        BrowserPromptMode::InputRequest,
        true,
        DefaultRepos::Auto,
    );
    Ok(())
}

fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        default_hook(panic_info);
        if ark::console::catching_panics() || tokio::runtime::Handle::try_current().is_ok() {
            return;
        }
        std::process::abort();
    }));
}
