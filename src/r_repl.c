#include <signal.h>
#include <stddef.h>
#include <sys/select.h>

typedef struct _InputHandler InputHandler;
typedef void (*repl_init_fn)(void);
typedef int (*repl_do_one_fn)(void);
typedef void (*before_do_one_fn)(void);
typedef int (*top_level_exec_fn)(void (*)(void *), void *);
typedef void *(*check_activity_fn)(int, int);
typedef void (*run_handlers_fn)(void *, void *);
typedef void (*check_interrupt_fn)(void);
typedef InputHandler *(*add_input_handler_fn)(
    InputHandler *, int, void (*)(void *), int
);
typedef int (*remove_input_handler_fn)(InputHandler **, InputHandler *);

struct event_handlers {
    check_activity_fn check_activity;
    run_handlers_fn run_handlers;
    void *input_handlers;
};

struct event_wait {
    add_input_handler_fn add_input_handler;
    check_activity_fn check_activity;
    InputHandler **input_handlers;
    InputHandler *sideband_handler;
    int sideband_fd;
    int wait_usec;
    int sideband_ready;
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
static int call_do_one(
    repl_do_one_fn do_one,
    check_interrupt_fn check_interrupt,
    const volatile int *interrupts_pending
) {
    if (*interrupts_pending != 0) check_interrupt();
    int status = do_one();
    if (*interrupts_pending != 0) check_interrupt();
    returned_normally = 1;
    return status;
}

static void run_ready_handlers(void *data) {
    struct event_handlers *handlers = data;
    void *ready = handlers->check_activity(0, 1);
    /* NULL intentionally runs R's polled-event hooks. */
    handlers->run_handlers(handlers->input_handlers, ready);
}

static void wait_for_activity(void *data) {
    struct event_wait *wait = data;
    wait->sideband_handler = wait->add_input_handler(
        *wait->input_handlers, wait->sideband_fd, NULL, 0
    );
    fd_set *ready = wait->check_activity(wait->wait_usec, 1);
    wait->sideband_ready = ready != NULL && FD_ISSET(wait->sideband_fd, ready);
}

void mcp_r_run_ready_handlers(
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
    /* Contain handler long jumps without promoting them to worker failures. */
    (void) top_level_exec(run_ready_handlers, &handlers);
}

int mcp_r_wait_for_activity(
    top_level_exec_fn top_level_exec,
    add_input_handler_fn add_input_handler,
    remove_input_handler_fn remove_input_handler,
    check_activity_fn check_activity,
    InputHandler **input_handlers,
    int sideband_fd,
    int wait_usec
) {
    if (sideband_fd < 0 || sideband_fd >= FD_SETSIZE) {
        return -1;
    }
    struct event_wait wait = {
        add_input_handler,
        check_activity,
        input_handlers,
        NULL,
        sideband_fd,
        wait_usec > 0 ? wait_usec : -1,
        0,
    };
    int completed = top_level_exec(wait_for_activity, &wait);
    int removed = wait.sideband_handler == NULL ||
        remove_input_handler(input_handlers, wait.sideband_handler);
    if (!removed) {
        return -1;
    }
    /* R_ToplevelExec contains interrupt and handler-error unwinds. */
    return completed ? wait.sideband_ready : 0;
}

int mcp_r_repl_run_cell(
    repl_init_fn init,
    repl_do_one_fn do_one,
    before_do_one_fn before_do_one,
    check_interrupt_fn check_interrupt,
    const volatile int *interrupts_pending
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
        int status = call_do_one(do_one, check_interrupt, interrupts_pending);
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
