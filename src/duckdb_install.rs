//! Optional lexical recognition of complete DuckDB INSTALL-only cells.
//!
//! This has no execution or authorization capabilities. Unknown grammar declines
//! the entire cell, including any successfully recognized prefix. See
//! `docs/DUCKDB_INSTALL_PREFLIGHT.md` for the deliberately narrow contract.

use std::ops::Range;

pub const MAX_SOURCE_BYTES: usize = 1024 * 1024;
pub const MAX_INSTALLS: usize = 1024;

#[derive(Debug, PartialEq, Eq)]
pub enum Preflight<'a> {
    Recognized(Plan<'a>),
    Passthrough(Decline),
}

#[derive(Debug, PartialEq, Eq)]
pub enum Decline {
    UncertainGrammar,
    UnsupportedInstall,
    IncompleteInput,
    ResourceLimit,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Plan<'a> {
    pub source: &'a str,
    pub installs: Vec<Install<'a>>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Install<'a> {
    pub span: Range<usize>,
    pub name: Name<'a>,
    pub repository: Repository<'a>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Name<'a> {
    pub value: &'a str,
    pub span: Range<usize>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Repository<'a> {
    Default,
    Alias(Name<'a>),
}

/// Recognize a complete restricted cell without interpreting ordinary SQL.
///
/// All spans are byte offsets into the unchanged source. A recognized plan is
/// lexical evidence only, not DuckDB parse/bind success or resolver authorization.
pub fn recognize(source: &str) -> Preflight<'_> {
    match analyze(source) {
        Ok(plan) => Preflight::Recognized(plan),
        Err(reason) => Preflight::Passthrough(reason),
    }
}

fn analyze(source: &str) -> Result<Plan<'_>, Decline> {
    if source.len() > MAX_SOURCE_BYTES {
        return Err(Decline::ResourceLimit);
    }
    if source.contains('\0') {
        return Err(Decline::IncompleteInput);
    }
    let mut scanner = Scanner { source, offset: 0 };
    let mut installs = Vec::new();
    while let Some(token) = scanner.next()? {
        if token.kind == Kind::Semicolon {
            continue;
        }
        if token.keyword(source, "FORCE") {
            return Err(Decline::UnsupportedInstall);
        }
        if !token.keyword(source, "INSTALL") {
            // Without understanding a grammar's bodies, a semicolon cannot
            // establish where a later INSTALL belongs. Never scan past it.
            return Err(Decline::UncertainGrammar);
        }
        let start = token.span.start;
        let name = scanner.required_name(false)?;
        let mut end = name.span.end;
        let mut next = scanner.next()?;
        let repository = if next
            .as_ref()
            .is_some_and(|token| token.keyword(source, "FROM"))
        {
            let alias = scanner.required_name(true)?;
            end = alias.span.end;
            next = scanner.next()?;
            Repository::Alias(alias)
        } else {
            Repository::Default
        };
        if next.is_some_and(|token| token.kind != Kind::Semicolon) {
            return Err(Decline::UnsupportedInstall);
        }
        if installs.len() == MAX_INSTALLS {
            return Err(Decline::ResourceLimit);
        }
        installs.push(Install {
            span: start..end,
            name,
            repository,
        });
    }
    Ok(Plan { source, installs })
}

#[derive(PartialEq, Eq)]
enum Kind {
    Word,
    Quoted(u8),
    Semicolon,
    Other,
}

struct Token {
    kind: Kind,
    span: Range<usize>,
}

impl Token {
    fn keyword(&self, source: &str, keyword: &str) -> bool {
        self.kind == Kind::Word && source[self.span.clone()].eq_ignore_ascii_case(keyword)
    }
}

struct Scanner<'a> {
    source: &'a str,
    offset: usize,
}

impl<'a> Scanner<'a> {
    fn required_name(&mut self, repository: bool) -> Result<Name<'a>, Decline> {
        let token = self.next()?.ok_or(Decline::IncompleteInput)?;
        let value = match token.kind {
            Kind::Semicolon => return Err(Decline::IncompleteInput),
            Kind::Word => &self.source[token.span.clone()],
            Kind::Quoted(quote) if !repository || quote == b'"' => {
                &self.source[token.span.start + 1..token.span.end - 1]
            }
            _ => return Err(Decline::UnsupportedInstall),
        };
        // Keep raw case and spelling. In particular, repository aliases are
        // case-sensitive in DuckDB. Single-quoted repositories denote paths.
        let bytes = value.as_bytes();
        if !bytes
            .first()
            .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
            || !bytes
                .iter()
                .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
            || token.keyword(self.source, "FROM")
            || token.keyword(self.source, "INSTALL")
        {
            return Err(Decline::UnsupportedInstall);
        }
        Ok(Name {
            value,
            span: token.span,
        })
    }

    fn next(&mut self) -> Result<Option<Token>, Decline> {
        self.trivia()?;
        let bytes = self.source.as_bytes();
        let Some(&byte) = bytes.get(self.offset) else {
            return Ok(None);
        };
        let start = self.offset;
        self.offset += 1;
        let kind = match byte {
            b';' => Kind::Semicolon,
            b'\'' | b'"' => {
                loop {
                    let Some(&next) = bytes.get(self.offset) else {
                        return Err(Decline::IncompleteInput);
                    };
                    self.offset += 1;
                    if next == byte {
                        if bytes.get(self.offset) == Some(&byte) {
                            self.offset += 1;
                        } else {
                            break;
                        }
                    }
                }
                Kind::Quoted(byte)
            }
            byte if byte.is_ascii_alphanumeric() || byte == b'_' => {
                while bytes
                    .get(self.offset)
                    .is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
                {
                    self.offset += 1;
                }
                Kind::Word
            }
            _ => Kind::Other,
        };
        Ok(Some(Token {
            kind,
            span: start..self.offset,
        }))
    }

    fn trivia(&mut self) -> Result<(), Decline> {
        let bytes = self.source.as_bytes();
        while let Some(&byte) = bytes.get(self.offset) {
            if matches!(byte, b' ' | b'\t' | b'\r' | b'\n' | 0x0c) {
                self.offset += 1;
            } else if bytes[self.offset..].starts_with(b"--") {
                self.offset += 2;
                while bytes
                    .get(self.offset)
                    .is_some_and(|byte| !matches!(byte, b'\r' | b'\n'))
                {
                    self.offset += 1;
                }
            } else if bytes[self.offset..].starts_with(b"/*") {
                self.offset += 2;
                let mut depth = 1;
                while depth != 0 {
                    if self.offset == bytes.len() {
                        return Err(Decline::IncompleteInput);
                    }
                    let rest = &bytes[self.offset..];
                    if rest.starts_with(b"/*") {
                        depth += 1;
                        self.offset += 2;
                    } else if rest.starts_with(b"*/") {
                        depth -= 1;
                        self.offset += 2;
                    } else {
                        self.offset += 1;
                    }
                }
            } else {
                break;
            }
        }
        Ok(())
    }
}
