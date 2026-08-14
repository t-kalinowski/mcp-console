#include <signal.h>
#include <stddef.h>

typedef void (*repl_init_fn)(void);
typedef int (*repl_do_one_fn)(void);
typedef void (*before_do_one_fn)(void);
typedef int (*top_level_exec_fn)(void (*)(void *), void *);
typedef void *(*check_activity_fn)(int, int);
typedef void (*run_handlers_fn)(void *, void *);

struct event_handlers {
    check_activity_fn check_activity;
    run_handlers_fn run_handlers;
    void *input_handlers;
};

/*
 * R errors jump to the context installed by R_ReplDLLinit(). Keep that
 * context and every R_ReplDLLdo1() call beneath this C boundary so the jump
 * never crosses a live Rust frame.
 */
static volatile sig_atomic_t returned_normally = 1;

/*
 * R's jump can make R_ReplDLLinit() return after this helper call instead of
 * its original call site. Set the marker only when do_one returns normally,
 * and keep the helper as a distinct C frame in optimized builds.
 */
__attribute__((noinline))
static int call_do_one(repl_do_one_fn do_one) {
    int status = do_one();
    returned_normally = 1;
    return status;
}

static void run_ready_handlers(void *data) {
    struct event_handlers *handlers = data;
    void *ready = handlers->check_activity(0, 1);
    if (ready != NULL) {
        handlers->run_handlers(handlers->input_handlers, ready);
    }
}

int mcp_r_run_ready_handlers(
    top_level_exec_fn top_level_exec,
    check_activity_fn check_activity,
    run_handlers_fn run_handlers,
    void *input_handlers
) {
    struct event_handlers handlers = {
        check_activity,
        run_handlers,
        input_handlers,
    };
    return top_level_exec(run_ready_handlers, &handlers);
}

int mcp_r_repl_run_cell(
    repl_init_fn init,
    repl_do_one_fn do_one,
    before_do_one_fn before_do_one
) {
    int last_status = 1;

    returned_normally = 1;
    init();
    /* After a top-level jump, init() returns to this call site a second time. */
    if (!returned_normally) {
        returned_normally = 1;
        return 0;
    }

    for (;;) {
        before_do_one();
        returned_normally = 0;
        int status = call_do_one(do_one);
        if (!returned_normally) {
            returned_normally = 1;
            return 0;
        }
        if (status < 0) {
            return last_status;
        }
        last_status = status;
    }
}
