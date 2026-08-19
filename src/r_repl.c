#include <errno.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/select.h>

typedef struct _InputHandler InputHandler;
typedef void (*repl_init_fn)(void);
typedef int (*repl_do_one_fn)(void);
typedef void (*before_do_one_fn)(void);
typedef int (*top_level_exec_fn)(void (*)(void *), void *);
typedef void *(*check_activity_fn)(int, int);
typedef void (*run_handlers_fn)(void *, void *);
typedef int (*read_console_fn)(const char *, unsigned char *, int, int);
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

static read_console_fn read_console;
static check_interrupt_fn check_interrupt;
static const volatile int *interrupts_pending;
static struct sigaction original_sigint_action;
static volatile sig_atomic_t interrupt_generation = 0;

static void observe_interrupt(
    int signal,
    siginfo_t *info,
    void *context
) {
    if (interrupt_generation < SIG_ATOMIC_MAX) {
        interrupt_generation += 1;
    }
    if ((original_sigint_action.sa_flags & SA_SIGINFO) != 0) {
        original_sigint_action.sa_sigaction(signal, info, context);
    } else {
        original_sigint_action.sa_handler(signal);
    }
}

int mcp_r_install_interrupt_observer(void) {
    struct sigaction current;
    if (sigaction(SIGINT, NULL, &current) < 0) {
        return errno;
    }
    /* R reinstalls its own handler after some interrupts. */
    if (current.sa_sigaction == observe_interrupt) {
        return 0;
    }
    original_sigint_action = current;
    if (
        (original_sigint_action.sa_flags & SA_SIGINFO) != 0
            ? original_sigint_action.sa_sigaction == NULL
            : original_sigint_action.sa_handler == SIG_DFL ||
                original_sigint_action.sa_handler == SIG_IGN
    ) {
        return EINVAL;
    }

    struct sigaction action = original_sigint_action;
    action.sa_sigaction = observe_interrupt;
    action.sa_flags |= SA_SIGINFO;
    action.sa_flags &= ~SA_RESETHAND;
    if (sigaction(SIGINT, &action, NULL) < 0) {
        return errno;
    }
    return 0;
}

int mcp_r_interrupt_generation(void) {
    return interrupt_generation;
}

void mcp_r_console_configure(
    read_console_fn read,
    check_interrupt_fn check,
    const volatile int *pending
) {
    read_console = read;
    check_interrupt = check;
    interrupts_pending = pending;
}

/*
 * Rust reports cancellation with -1 after its stack has unwound. Check from
 * this C frame because R may jump to its top-level interrupt context.
 */
int mcp_r_read_console(
    const char *prompt,
    unsigned char *buffer,
    int length,
    int add_history
) {
    int status = read_console(prompt, buffer, length, add_history);
    if (status < 0) {
        if (*interrupts_pending != 0) check_interrupt();
        return 0;
    }
    return status;
}

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
