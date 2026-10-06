//! Requirement keys retain hidden-provider capability; their prose follows visibility.
use serde_json::{Map, Value};

use crate::cell::Languages;

pub(super) fn configure(properties: &mut Map<String, Value>, languages: Languages, builtin: bool) {
    let mut description = String::from(
        "Inspect or manage retained package and database-extension requirements. \
action=get returns a read-only snapshot, including runtime constraints and separate infrastructure. \
It cannot accompany code, stdin, control, or payload fields. The complete manifest is in \
structuredContent.requirements even when it exceeds the text preview limit. \
action=add is the default; action=set replaces the whole declaration without injecting defaults; \
action=reset restores startup defaults. Changed replacements require control=\"restart\" with a \
live worker. Empty set means no optional requirements; bare {} is invalid.\n\n\
Requirements alone perform standalone preparation. With one cell, they are preconditions of \
that cell. With control=\"restart\", they are part of the restart transaction, with or without a \
cell. Only add can accompany interrupt, and only when a cell follows. When the provider cannot \
prepare during interrupt, the call is rejected before signaling or queuing input; prepare \
separately or use restart. \
Preparation does not import, attach, or load dependencies. On a code-bearing call without \
control, preparation completes before same-call nonempty stdin is queued. Standalone \
preparation cannot queue nonempty stdin. With restart, failure leaves the current worker \
unchanged and sends neither stdin nor code. With add, interrupt, and a following cell, signal \
delivery and stdin enqueue happen before requirements are validated or prepared and are not \
rolled back if that later work fails. ",
    );
    if builtin && languages.r {
        description.push_str(
            "Ordinary CRAN packages used by the built-in R worker need not be declared here; \
use requirements.r to stage packages ahead of evaluation or provide explicit ir references. ",
        );
    }
    if builtin && languages.python {
        description.push_str(
            "In the built-in managed Python environment, missing imports normally resolve at \
runtime. Use requirements.python to stage a distribution before the cell, provide a version, \
extra, or marker, or correct automatic inference. Python source is not pre-scanned. ",
        );
    }
    if languages.sql {
        description.push_str(
            "SQL does not trigger package discovery. Provider preparation may require fields \
whose direct code language is hidden; follow the missing-provider diagnostic. ",
        );
    }
    description.push_str(
        "A cell is not run if explicit preparation fails or further changes require restart. \
Resolution uses the execution host's resolver policy and may download packages or extensions \
or execute installation or build code. Use only trusted requirements.",
    );
    let requirements = &mut properties["requirements"];
    requirements["description"] = description.into();
    let fields = requirements["properties"]
        .as_object_mut()
        .expect("requirements schema properties");
    if !languages.r || !builtin {
        fields["r"]["description"] = HIDDEN_HOST_PACKAGES.into();
    }
    if !languages.python || !builtin {
        fields["python"]["description"] = HIDDEN_REGISTRY_PACKAGES.into();
    }
    if !languages.python {
        fields["python_version"]["description"] = HIDDEN_VERSION.into();
        fields["exclude_newer"]["description"] = HIDDEN_CUTOFF.into();
        fields["action"]["description"] = ACTION.into();
    }
}

const HIDDEN_HOST_PACKAGES: &str = "Single-line ir package references for host-provider preparation, \
for example DBI or duckdb. Use this field when a provider diagnostic requests it, for standalone \
preparation, preparation before a cell, or a restart transaction. An idle worker with a compatible \
package adapter can add requirements without losing live state. Local package sources are rejected \
by the managed resolution contract.";

const HIDDEN_REGISTRY_PACKAGES: &str = "Named PEP 508 registry requirements for host-provider \
preparation, for example duckdb>=1. Use this field when a provider diagnostic requests it, for \
standalone preparation, preparation before a cell, or a restart transaction. Paths, file URLs, \
editable requirements, direct references, local archives, and local projects are rejected. \
Preparation does not import the package. A server-managed worker may activate compatible \
additions without losing state. A user-selected environment disables managed registry requirements; \
a custom worker does not support them.";

const HIDDEN_VERSION: &str = "Host-provider runtime version constraints, preserved by get and \
replaced as a whole by set. Add appends constraints; changing constraints with a live worker \
requires control=\"restart\".";

const HIDDEN_CUTOFF: &str = "Registry package publication cutoff accepted by uv, for example \
\"2026-01-01\". set clears an omitted or null cutoff; add preserves an omitted cutoff and cannot \
replace an existing one.";

const ACTION: &str = "get inspects the committed declaration without starting a worker or \
consuming output. add (default) accumulates requirements. set replaces all lists and runtime \
constraints; omitted fields are empty, including when only action is supplied. reset restores \
startup defaults. get and reset reject payload fields. Changed set/reset with a live worker \
require control=\"restart\"; the complete candidate resolves before the old worker is retired. \
add accepts up to 64 entries per language per call; set accepts the complete accumulated manifest.";
