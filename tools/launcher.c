// HIL Link.app 的原生启动器：CoreBluetooth 要求"当前可执行文件所在 bundle"带 NSBluetoothAlwaysUsageDescription，
// 所以不能 exec 出 .venv 里的 python，而是由这个二进制加载 libpython 在本进程里跑 hil.link_server。
// 编译时由 make_link_app.sh 通过 -D 注入路径：LIBPYTHON PYHOME SITEPKG ROOT LOGFILE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char** argv) {
    setenv("PYTHONHOME", PYHOME, 1);
    setenv("PYTHONPATH", SITEPKG, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("LANG", "en_US.UTF-8", 0);
    chdir(ROOT);
    if (argc <= 1) {  // 由 LaunchServices 启动：日志进文件
        freopen(LOGFILE, "a", stdout);
        freopen(LOGFILE, "a", stderr);
    }
    void* h = dlopen(LIBPYTHON, RTLD_NOW | RTLD_GLOBAL);
    if (!h) { fprintf(stderr, "dlopen %s failed: %s\n", LIBPYTHON, dlerror()); return 1; }
    int (*py_main)(int, char**) = (int (*)(int, char**))dlsym(h, "Py_BytesMain");
    if (!py_main) { fprintf(stderr, "Py_BytesMain not found\n"); return 1; }
    char* args_default[] = {"python", "-m", "hil.link_server", "--transport", "ble", NULL};
    if (argc > 1) {  // 命令行直接运行时把参数透传给 python（用于自检）
        char** args = calloc(argc + 1, sizeof(char*));
        args[0] = "python";
        for (int i = 1; i < argc; i++) args[i] = argv[i];
        return py_main(argc, args);
    }
    return py_main(5, args_default);
}
