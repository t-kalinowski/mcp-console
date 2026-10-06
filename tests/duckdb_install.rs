use mcp_console::duckdb_install::{
    Decline, MAX_INSTALLS, MAX_SOURCE_BYTES, Preflight, Repository, recognize,
};

#[test]
fn recognizes_default_and_explicit_aliases_with_exact_utf8_spans() {
    let source = "-- 🦆 INSTALL hidden\n /* outer /* nested */ end */ INSTALL json;\nINSTALL 'httpfs' FROM \"core\"; -- tail λ\n";
    let Preflight::Recognized(plan) = recognize(source) else {
        panic!("complete supported cell should produce a plan");
    };
    assert_eq!(plan.source, source);
    assert_eq!(plan.installs.len(), 2);
    assert_eq!(plan.installs[0].name.value, "json");
    assert_eq!(plan.installs[0].repository, Repository::Default);
    assert_eq!(&source[plan.installs[0].span.clone()], "INSTALL json");
    assert_eq!(&source[plan.installs[0].name.span.clone()], "json");
    assert_eq!(plan.installs[1].name.value, "httpfs");
    let Repository::Alias(alias) = &plan.installs[1].repository else {
        panic!("explicit FROM must remain explicit");
    };
    assert_eq!(alias.value, "core");
    assert_eq!(&source[alias.span.clone()], "\"core\"");
    assert_eq!(&source[plan.installs[1].name.span.clone()], "'httpfs'");
    assert_eq!(
        &source[plan.installs[1].span.clone()],
        "INSTALL 'httpfs' FROM \"core\""
    );
    let mut roundtrip = String::new();
    let mut end = 0;
    for install in &plan.installs {
        roundtrip.push_str(&source[end..install.span.start]);
        roundtrip.push_str(&source[install.span.clone()]);
        end = install.span.end;
    }
    roundtrip.push_str(&source[end..]);
    assert_eq!(roundtrip.as_bytes(), source.as_bytes());
}

#[test]
fn preserves_name_case_and_explicit_repository_spelling() {
    for source in [
        "iNsTaLl JSON FROM CORE",
        "INSTALL \"JSON\" FROM \"CORE\";",
        "INSTALL 'JSON' FROM CORE",
    ] {
        let Preflight::Recognized(plan) = recognize(source) else {
            panic!("{source:?}");
        };
        assert_eq!(plan.installs[0].name.value, "JSON");
        let Repository::Alias(alias) = &plan.installs[0].repository else {
            panic!("{source:?}");
        };
        assert_eq!(alias.value, "CORE");
    }
}

#[test]
fn permits_trivia_empty_statements_and_comments_between_install_tokens() {
    let source = ";; \t\r\nINSTALL/*λ/*🦆*/x*/json -- FROM bogus\r FROM\ncore;;;";
    let Preflight::Recognized(plan) = recognize(source) else {
        panic!("comments and SQL whitespace are trivia");
    };
    assert_eq!(plan.installs.len(), 1);
    assert_eq!(plan.installs[0].name.value, "json");
    assert_eq!(
        &source[plan.installs[0].span.clone()],
        "INSTALL/*λ/*🦆*/x*/json -- FROM bogus\r FROM\ncore"
    );
}

#[test]
fn never_extracts_requests_from_ordinary_or_unknown_grammar() {
    for source in [
        "SELECT 'INSTALL json;'; INSTALL httpfs;",
        "SELECT 'escaped '' ; INSTALL json;'; INSTALL httpfs;",
        r"SELECT E'escaped \' ; INSTALL json;'; INSTALL httpfs;",
        "SELECT $$body; INSTALL json;$$; INSTALL httpfs;",
        "SELECT $tag$body; INSTALL json;$tag$; INSTALL httpfs;",
        "SELECT \"identifier; INSTALL json;\"; INSTALL httpfs;",
        "BEGIN ATOMIC SELECT 1; INSTALL json; END; INSTALL httpfs;",
        "CREATE MACRO m() AS $$ INSTALL json; $$; INSTALL httpfs;",
        "CREATE PROCEDURE p() BEGIN SELECT 1; INSTALL json; END;",
        "EXTENSION SYNTAX body; INSTALL json; END;",
        "BEGIN; INSTALL json; COMMIT;",
        "SELECT 1; INSTALL json;",
        "INSTALL json; SELECT 1;",
        "INSTALL json; SELECT $$unfinished; INSTALL httpfs;",
        "\"INSTALL\" json;",
    ] {
        assert_eq!(
            recognize(source),
            Preflight::Passthrough(Decline::UncertainGrammar),
            "{source:?}"
        );
    }
}

#[test]
fn distinguishes_ordinary_force_statements_from_force_install() {
    for source in [
        "FORCE CHECKPOINT",
        "force checkpoint; INSTALL json;",
        "FORCE",
        "FORCE; INSTALL json;",
        "FORCE INSTALLATION json",
        "FORCE \"INSTALL\" json",
        "INSTALL json; FORCE CHECKPOINT",
    ] {
        assert_eq!(
            recognize(source),
            Preflight::Passthrough(Decline::UncertainGrammar),
            "{source:?}"
        );
    }
    for source in [
        "FORCE INSTALL json",
        "force /* nested /* 🦆 */ comment */ install json",
        "INSTALL json; FORCE INSTALL httpfs",
    ] {
        assert_eq!(
            recognize(source),
            Preflight::Passthrough(Decline::UnsupportedInstall),
            "{source:?}"
        );
    }
}

#[test]
fn classifies_unsupported_install_forms_without_a_partial_plan() {
    for source in [
        "INSTALL json VERSION '1.0'",
        "INSTALL json FROM 'core'",
        "INSTALL json FROM 'https://example.org'",
        "INSTALL '../json.duckdb_extension'",
        "INSTALL \"jso\"\"n\"",
        "INSTALL 'jso''n'",
        "INSTALL $tag$json$tag$",
        "INSTALL 'jsön'",
        "INSTALL jsön",
        "INSTALL json FROM 'co\\re'",
        "INSTALL json FROM core trailing",
        "INSTALL json INSTALL httpfs",
        "INSTALL json; INSTALL httpfs FROM 'core'",
        "INSTALL FROM core",
        "INSTALL (json)",
    ] {
        assert_eq!(
            recognize(source),
            Preflight::Passthrough(Decline::UnsupportedInstall),
            "{source:?}"
        );
    }
}

#[test]
fn discards_a_successful_prefix_when_the_remaining_input_is_incomplete() {
    for source in [
        "INSTALL",
        "INSTALL;",
        "INSTALL json FROM",
        "INSTALL json FROM;",
        "INSTALL json; /* unfinished",
        "INSTALL json; /* outer /* nested */",
        "INSTALL json; INSTALL 'unfinished",
        "INSTALL json; INSTALL \"unfinished",
        "INSTALL json; INSTALL httpfs FROM",
        "INSTALL json;\0",
    ] {
        assert_eq!(
            recognize(source),
            Preflight::Passthrough(Decline::IncompleteInput),
            "{source:?}"
        );
    }
}

#[test]
fn comments_alone_never_create_install_requests() {
    for source in [
        "",
        "; ;",
        "-- INSTALL json;",
        "/* INSTALL json; /* INSTALL httpfs; */ */",
    ] {
        let Preflight::Recognized(plan) = recognize(source) else {
            panic!("{source:?}");
        };
        assert!(plan.installs.is_empty());
        assert_eq!(plan.source, source);
    }
}

#[test]
fn bounds_input_size_and_plan_size_without_returning_prefixes() {
    let source = " ".repeat(MAX_SOURCE_BYTES);
    assert!(matches!(recognize(&source), Preflight::Recognized(_)));
    assert_eq!(
        recognize(&(source + " ")),
        Preflight::Passthrough(Decline::ResourceLimit)
    );
    let source = "INSTALL json;".repeat(MAX_INSTALLS);
    let Preflight::Recognized(plan) = recognize(&source) else {
        panic!("the documented maximum is supported");
    };
    assert_eq!(plan.installs.len(), MAX_INSTALLS);
    assert_eq!(
        recognize(&(source + "INSTALL json;")),
        Preflight::Passthrough(Decline::ResourceLimit)
    );
}

#[test]
fn handles_deep_comments_iteratively() {
    let source = format!(
        "{}🦆{} INSTALL json",
        "/*".repeat(10_000),
        "*/".repeat(10_000)
    );
    let Preflight::Recognized(plan) = recognize(&source) else {
        panic!("bounded nested comments do not need recursive parsing");
    };
    assert_eq!(plan.installs.len(), 1);
    assert_eq!(&source[plan.installs[0].span.clone()], "INSTALL json");
}
