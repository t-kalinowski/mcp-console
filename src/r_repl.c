#include <signal.h>

typedef void (*repl_init_fn)(void);
typedef int (*repl_do_one_fn)(void);
typedef void (*before_do_one_fn)(void);

/*
 * R errors jump to the context installed by R_ReplDLLinit(). Keep that
 * context and every R_ReplDLLdo1() call under this one C frame so the jump
 * never crosses a live Rust frame.
 */
static volatile sig_atomic_t inside_do_one = 0;

int mcp_r_repl_run_cell(
    repl_init_fn init,
    repl_do_one_fn do_one,
    before_do_one_fn before_do_one
) {
    int last_status = 1;

    inside_do_one = 0;
    init();
    /* After a top-level jump, init() returns to this call site a second time. */
    if (inside_do_one) {
        inside_do_one = 0;
        return 0;
    }

    for (;;) {
        before_do_one();
        inside_do_one = 1;
        int status = do_one();
        inside_do_one = 0;

        if (status < 0) {
            return last_status;
        }
        last_status = status;
    }
}
