use super::super::process::{ProcessIdentity, process_info};
use super::super::process_tracker::{DescendantTracker, EventWait};
use super::super::process_tree::PROCESS_REAP_EVENT;
use super::protocol;
use crate::sandbox::platform;
use std::fs;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;

const READY: u8 = 1;

pub(super) fn run() -> Result<(), String> {
    let mut stream = inherited_control();
    let protocol::Initialization {
        owner_pid,
        root_pid,
        cleanup_timeout,
        separate_process_group,
        temporary_directory,
    } = protocol::read(&mut stream)?;

    // SAFETY: getppid(2) has no pointer or lifetime preconditions.
    let parent_pid = unsafe { libc::getppid() };
    if parent_pid != owner_pid {
        return Err(format!(
            "sandbox manager owner changed before commitment: expected {owner_pid}, found {parent_pid}"
        ));
    }
    let owner = process_info(owner_pid)?
        .filter(|info| !info.is_zombie)
        .ok_or_else(|| format!("sandbox manager owner {owner_pid} exited before startup"))?
        .identity;
    let root_info = process_info(root_pid)?
        .ok_or_else(|| format!("sandbox root {root_pid} exited before manager startup"))?;
    if root_info.parent_pid != owner_pid {
        return Err(format!(
            "sandbox root {root_pid} is not a child of manager owner {owner_pid}"
        ));
    }
    let root = root_info.identity;

    let tracker =
        DescendantTracker::start(root_pid).map_err(|failure| failure.retire(cleanup_timeout))?;
    let temporary_directory = match AdoptedTemporaryDirectory::adopt(temporary_directory, owner_pid)
    {
        Ok(directory) => directory,
        Err(error) => {
            return with_cleanup(
                error,
                tracker,
                root,
                separate_process_group,
                cleanup_timeout,
            );
        }
    };
    if let Err(error) = register_owner_exit(&tracker, owner) {
        return finish_startup_failure(
            error,
            tracker,
            root,
            separate_process_group,
            temporary_directory,
            cleanup_timeout,
        );
    }
    if let Err(error) = stream.write_all(&[READY]) {
        return finish_startup_failure(
            format!("failed to report sandbox manager readiness: {error}"),
            tracker,
            root,
            separate_process_group,
            temporary_directory,
            cleanup_timeout,
        );
    }
    drop(stream);

    let result = supervise_owner(
        tracker,
        owner,
        root,
        separate_process_group,
        cleanup_timeout,
    );
    if result.is_err() {
        temporary_directory.preserve();
    }
    result
}

fn supervise_owner(
    mut tracker: DescendantTracker,
    owner: ProcessIdentity,
    root: ProcessIdentity,
    separate_process_group: bool,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    loop {
        match identity_is_live(owner) {
            Ok(false) => {
                return finish_tracker(
                    tracker,
                    false,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
            Ok(true) => {}
            Err(error) => {
                return with_cleanup(
                    error,
                    tracker,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
        }
        match tracker.root_has_exited() {
            Ok(true) => {
                return finish_tracker(
                    tracker,
                    true,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
            Ok(false) => {}
            Err(error) => {
                return with_cleanup(
                    error,
                    tracker,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
        }

        match tracker.wait_for_events(None) {
            Ok(EventWait::Events | EventWait::RootExited) => {}
            Ok(EventWait::TimedOut) => {
                return with_cleanup(
                    "sandbox manager process wait unexpectedly timed out".to_string(),
                    tracker,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
            Err(error) => {
                return with_cleanup(
                    error,
                    tracker,
                    root,
                    separate_process_group,
                    cleanup_timeout,
                );
            }
        }
    }
}

fn register_owner_exit(tracker: &DescendantTracker, owner: ProcessIdentity) -> Result<(), String> {
    let event = libc::kevent {
        ident: owner.pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: libc::NOTE_EXIT | PROCESS_REAP_EVENT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    loop {
        // SAFETY: the kqueue descriptor is live, `event` is initialized, and
        // this submission supplies no output buffer.
        let result = unsafe {
            libc::kevent(
                tracker.kqueue.as_raw_fd(),
                &event,
                1,
                std::ptr::null_mut(),
                0,
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to observe sandbox manager owner: {error}"));
        }
    }
}

fn identity_is_live(identity: ProcessIdentity) -> Result<bool, String> {
    Ok(
        process_info(identity.pid)?
            .is_some_and(|info| info.identity == identity && !info.is_zombie),
    )
}

fn with_cleanup(
    error: String,
    tracker: DescendantTracker,
    root: ProcessIdentity,
    separate_process_group: bool,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    match finish_tracker(
        tracker,
        false,
        root,
        separate_process_group,
        cleanup_timeout,
    ) {
        Ok(()) => Err(error),
        Err(cleanup_error) => Err(format!("{error}; additionally, {cleanup_error}")),
    }
}

fn finish_tracker(
    tracker: DescendantTracker,
    root_exited: bool,
    root: ProcessIdentity,
    separate_process_group: bool,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let mut error = tracker.terminate(root_exited, cleanup_timeout).err();
    if separate_process_group
        && let Err(group_error) = platform::kill_process_group(root.pid as u32)
    {
        error = Some(super::with_prior_error(
            error,
            format!("failed to stop sandbox process group: {group_error}"),
        ));
    }
    error.map_or(Ok(()), Err)
}

fn finish_startup_failure(
    error: String,
    tracker: DescendantTracker,
    root: ProcessIdentity,
    separate_process_group: bool,
    temporary_directory: AdoptedTemporaryDirectory,
    cleanup_timeout: Duration,
) -> Result<(), String> {
    let result = with_cleanup(
        error,
        tracker,
        root,
        separate_process_group,
        cleanup_timeout,
    );
    if resu[š\×Ù\œŠ
HÂˆ[\Ü˜\WÙ\™XİÜKœ™\Ù\™J
NÂˆBˆ™\İ[ŸB‚™›ˆ[š\š]YØÛÛ›Û

HOˆ[š^İ™X[HÂˆËÈĞQ‘UNˆHY[ˆX[˜YÙ\ˆ[HÚ[\È][˜ÚYÚ]]ÈİÛ™YÛÛ›ÛˆËÈİ™X[HÛˆ™[™Ù\È›İİ\Ú\ÙH\ÙHİ[™\™[œ]‚ˆ[œØY™HÈ[š^İ™X[N™œ›ÛWÜ˜]×Ù™
X˜Î”ÕS—Ñ’SS“ÊHBŸB‚œİXİYÜY[\Ü˜\Q\™XİÜJ]YŠNÂ‚š[\YÜY[\Ü˜\Q\™XİÜHÂˆ›ˆYÜ
]ˆ]Y‹İÛ™\—ÜYˆX˜ÎœYİ
HOˆ™\İ[Ù[‹İš[™ÏˆÂˆ]]H]˜Ø[›ÛšXØ[^™J
K›X\Ù\œŠ\œ›ÜŸÂˆ›Ü›X]Jˆ™˜Z[YÈ™\ÛÛ™HØ[™›Ş[\Ü˜\H\™XİÜHßNˆÙ\œ›ÜŸH‹ˆ]™\Ü^J
Bˆ
BˆJOÎÂˆ]^XİYÜ™Yš^H›Ü›X]J›XÜXÛÛœÛÛK]\^ÛİÛ™\—ÜYKHŠNÂˆ]˜[YÛ˜[YHH]ˆ™š[WÛ˜[YJ
Bˆ˜[™İ[Š˜[Y_˜[YK×ÜİŠ
JBˆš\×ÜÛÛYWØ[™
˜[Y_˜[YKœİ\×İÚ]
	™^XİYÜ™Yš^
JNÂˆ]^XİYÜ\™[Hİ™[[\Ù\Š
K˜Ø[›ÛšXØ[^™J
K›X\Ù\œŠ\œ›ÜŸÂˆ›Ü›X]J™˜Z[YÈ™\ÛÛ™HHŞ\İ[H[\Ü˜\H\™XİÜNˆÙ\œ›ÜŸHŠBˆJOÎÂˆYˆ]˜[YÛ˜[YH]œ\™[

HOHÛÛYJ^XİYÜ\™[˜\×Ü]

JH\]š\×Ù\Š
HÂˆ™]\›ˆ\œŠœØ[™›Ş[\Ü˜\H\™XİÜH\È[˜[YİÛ™\œÚ\‹×Üİš[™Ê
JNÂˆBˆÚÊÙ[Š]
JBˆB‚ˆ›ˆ™\Ù\™JÙ[ŠHÂˆİ›Y[N™›Ü™Ù]
Ù[ŠNÂˆBŸB‚š[\›Ü›ÜˆYÜY[\Ü˜\Q\™XİÜHÂˆ›ˆ›Ü
	›]]Ù[ŠHÂˆ]ÈHœÎœ™[[İ™WÙ\—Ø[
	œÙ[‹Œ
NÂˆBŸB