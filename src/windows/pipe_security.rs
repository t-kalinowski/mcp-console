//! Named-pipe defaults grant only read access to restricted tokens. Scope both
//! endpoints to the current logon session, which native sandbox tokens retain.

use std::io;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use windows_sys::Win32::Foundation::GENERIC_ALL;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

pub(super) struct PipeSecurity {
    pub(super) descriptor: SECURITY_DESCRIPTOR,
    _acl: Vec<usize>,
}

impl PipeSecurity {
    pub(super) fn new() -> io::Result<Self> {
        let mut token = std::ptr::null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = unsafe { OwnedHandle::from_raw_handle(token) };
        let mut length = 0;
        unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenLogonSid,
                std::ptr::null_mut(),
                0,
                &mut length,
            );
        }
        // Word storage keeps the variable-length TOKEN_GROUPS and ACL aligned.
        let mut groups = vec![0usize; (length as usize).div_ceil(std::mem::size_of::<usize>())];
        if unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenLogonSid,
                groups.as_mut_ptr().cast(),
                length,
                &mut length,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let groups_ref = unsafe { &*groups.as_ptr().cast::<TOKEN_GROUPS>() };
        if groups_ref.GroupCount != 1 {
            return Err(io::Error::other(
                "a logon SID is required for private pipes",
            ));
        }
        let sid = groups_ref.Groups[0].Sid;
        let acl_size = std::mem::size_of::<ACL>() + std::mem::size_of::<ACCESS_ALLOWED_ACE>()
            - std::mem::size_of::<u32>()
            + unsafe { GetLengthSid(sid) } as usize;
        let mut acl = vec![0usize; acl_size.div_ceil(std::mem::size_of::<usize>())];
        let acl_pointer = acl.as_mut_ptr().cast::<ACL>();
        let mut descriptor = unsafe { std::mem::zeroed::<SECURITY_DESCRIPTOR>() };
        // AddAccessAllowedAce copies the SID; only the ACL must outlive the
        // pointers in the descriptor through CreateNamedPipeW.
        if unsafe {
            InitializeAcl(acl_pointer, acl_size as u32, ACL_REVISION) == 0
                || AddAccessAllowedAce(acl_pointer, ACL_REVISION, GENERIC_ALL, sid) == 0
                || InitializeSecurityDescriptor(
                    (&raw mut descriptor).cast(),
                    1, // SECURITY_DESCRIPTOR_REVISION
                ) == 0
                || SetSecurityDescriptorDacl((&raw mut descriptor).cast(), 1, acl_pointer, 0) == 0
        } {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            descriptor,
            _acl: acl,
        })
    }
}
