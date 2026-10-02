//! Resolver descendants belong to a Job before any resolver code can run.
use std::io;
use std::mem::{size_of, zeroed};
use std::ops::{Deref, DerefMut};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Child as Process, Command, ExitStatus};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::System::Diagnostics::ToolHelp::*;
use windows_sys::Win32::System::IO::*;
use windows_sys::Win32::System::JobObjects::*;
use windows_sys::Win32::System::Threading::*;

pub(crate) struct Child {
    process: Process,
    job: OwnedHandle,
    completion: OwnedHandle,
}

impl Deref for Child {
    type Target = Process;
    fn deref(&self) -> &Process {
        &self.process
    }
}
impl DerefMut for Child {
    fn deref_mut(&mut self) -> &mut Process {
        &mut self.process
    }
}

pub(crate) fn spawn(command: &mut Command) -> io::Result<Child> {
    // SAFETY: every successful handle creation is immediately adopted. No
    // resolver thread runs until assignment to this kill-on-close Job succeeds.
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = OwnedHandle::from_raw_handle(job);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        let port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, std::ptr::null_mut(), 0, 1);
        if port.is_null() {
            return Err(io::Error::last_os_error());
        }
        let completion = OwnedHandle::from_raw_handle(port);
        let association = JOBOBJECT_ASSOCIATE_COMPLETION_PORT {
            CompletionKey: std::ptr::null_mut(),
            CompletionPort: completion.as_raw_handle(),
        };
        if SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectAssociateCompletionPortInformation,
            (&association as *const JOBOBJECT_ASSOCIATE_COMPLETION_PORT).cast(),
            size_of_val(&association) as u32,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
        let mut process = command.spawn()?;
        let start = (|| {
            if AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) == 0 {
                return Err(io::Error::last_os_error());
            }
            resume_initial_thread(process.id())
        })();
        if let Err(error) = start {
            let _ = process.kill();
            let _ = process.wait();
            return Err(error);
        }
        Ok(Child {
            process,
            job,
            completion,
        })
    }
}

fn resume_initial_thread(pid: u32) -> io::Result<()> {
    // std::process::Child retains the process handle but closes the primary
    // thread handle. A suspended new process has exactly one thread; its PID
    // remains owned by Child throughout this snapshot and resume.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let snapshot = OwnedHandle::from_raw_handle(snapshot);
        let mut entry: THREADENTRY32 = zeroed();
        entry.dwSize = size_of::<THREADENTRY32>() as u32;
        let mut found = Thread32First(snapshot.as_raw_handle(), &mut entry);
        while found != 0 {
            if entry.th32OwnerProcessID == pid {
                let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if thread.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let thread = OwnedHandle::from_raw_handle(thread);
                if ResumeThread(thread.as_raw_handle()) == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                return Ok(());
            }
            found = Thread32Next(snapshot.as_raw_handle(), &mut entry);
        }
        Err(io::Error::other("resolver primary thread was not found"))
    }
}

impl Child {
    pub(crate) fn terminate(&self) -> io::Result<()> {
        if unsafe { TerminateJobObject(self.job.as_raw_handle(), 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    pub(crate) fn retire(&mut self) -> io::Result<ExitStatus> {
        self.terminate()?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            // The accounting query is authoritative, including when all exits
            // preceded retirement. Completion-port messages wake subsequent
            // queries; there is no interval polling or PID-based tree walk.
            let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { zeroed() };
            if unsafe {
                QueryInformationJobObject(
                    self.job.as_raw_handle(),
                    JobObjectBasicAccountingInformation,
                    (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    size_of_val(&accounting) as u32,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            if accounting.ActiveProcesses == 0 {
                return self.process.wait();
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "resolver Job retirement was not confirmed",
                ));
            }
            let mut message = 0;
            let mut key = 0;
            let mut overlapped = std::ptr::null_mut();
            if unsafe {
                GetQueuedCompletionStatus(
                    self.completion.as_raw_handle(),
                    &mut message,
                    &mut key,
                    &mut overlapped,
                    remaining.as_millis() as u32,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
        }
    }
}
