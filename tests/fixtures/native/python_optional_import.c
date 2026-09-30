#define Py_LIMITED_API 0x03090000
#include <Python.h>

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "mcp_console_test_native_optional",
    .m_size = -1,
};

PyMODINIT_FUNC PyInit_mcp_console_test_native_optional(void) {
    PyObject *dependency = PyImport_ImportModule(
        "mcp_console_test_native_optional_dependency");
    int fallback = dependency == NULL;
    if (fallback) {
        if (!PyErr_ExceptionMatches(PyExc_ImportError)) {
            return NULL;
        }
        PyErr_Clear();
    } else {
        Py_DECREF(dependency);
    }

    PyObject *result = PyModule_Create(&module);
    if (result == NULL) {
        return NULL;
    }
    if (PyModule_AddIntConstant(result, "fallback", fallback) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
