/*
 * EOSSDK-Win64-Shipping.dll 私有服务器 stub
 * ==========================================
 * hng.exe/player.dll 通过 LoadLibrary+GetProcAddress 动态加载 EOS SDK 并调用
 * EOS_Connect_Login 完成 Epic Online Services 认证。私服无法从 Epic 后端换取
 * 有效 OpenID token，因此用本 stub 让 EOS 登录"直接成功"并返回伪 ProductUserId，
 * 使游戏进程通过认证继续走 warserver 握手。其余 EOS 功能（AntiCheat/Reports/
 * Logging）全部 no-op 返回成功。
 *
 * 编译（zig 已由 pip 安装）：
 *   zig cc -shared -O2 -o EOSSDK-Win64-Shipping.dll eos_stub.c
 *
 * 部署：备份原 dll 后替换（游戏进程需未运行）。
 */
#include <stdint.h>
#include <string.h>
#include <windows.h>

#define EOS_Success 0

/* 调试日志：stub 是否被加载、哪些函数被调用（写游戏目录 eos_stub.log） */
static void s_log(const char* msg) {
    HANDLE h = CreateFileA("eos_stub.log", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD w = 0;
        char buf[256];
        int n = wsprintfA(buf, "%s\n", msg);
        WriteFile(h, buf, (DWORD)n, &w, NULL);
        CloseHandle(h);
    }
}

/* ---------------- DLL 入口 ---------------- */
BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) s_log("== EOS stub DLL loaded ==");
    return TRUE;
}

typedef void* EOS_HPlatform;
typedef void* EOS_HConnect;
typedef void* EOS_HAntiCheatClient;
typedef void* EOS_HAntiCheatServer;
typedef void* EOS_HReports;
typedef void* EOS_ProductUserId;
typedef uint64_t EOS_NotificationId;

/* 伪 handle / PUID：非空即可 */
static int g_platform, g_connect, g_anticheat_client, g_anticheat_server, g_reports, g_puid;

/* ---------------- EOS_Connect_Login / CreateUser 回调 ---------------- */
typedef struct {
    void* ClientData;
    int32_t ResultCode;
    EOS_ProductUserId LocalUserId;
    const void* ContinuanceToken;
} ConnectLoginCallbackInfo;

typedef void(__cdecl* ConnectLoginCallback)(const ConnectLoginCallbackInfo*);

typedef struct {
    void* ClientData;
    ConnectLoginCallback Callback;
} PendingLogin;

static PendingLogin g_pending[16];
static int g_pending_n = 0;

static void fire_pending_login(int idx) {
    s_log("fire_pending_login: ENTER");
    ConnectLoginCallbackInfo info;
    memset(&info, 0, sizeof(info));
    info.ClientData = g_pending[idx].ClientData;
    info.ResultCode = EOS_Success;
    info.LocalUserId = &g_puid;   /* 伪 PUID */
    info.ContinuanceToken = NULL;
    /* 日志：记录 callback 地址和 info 字段布局 */
    {
        char buf[256];
        int n = wsprintfA(buf, "fire_pending_login: idx=%d cb=%p ClientData=%p LocalUserId=%p",
                          idx, (void*)g_pending[idx].Callback,
                          g_pending[idx].ClientData, (void*)info.LocalUserId);
        HANDLE h = CreateFileA("eos_stub.log", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            DWORD w = 0;
            WriteFile(h, buf, (DWORD)n, &w, NULL);
            WriteFile(h, "\n", 1, &w, NULL);
            CloseHandle(h);
        }
    }
    s_log("fire_pending_login: calling callback");
    g_pending[idx].Callback(&info);
    s_log("fire_pending_login: callback returned OK");
}

/* ---------------- 导出函数（x64 ABI，参数经寄存器传递） ---------------- */

__declspec(dllexport) int32_t EOS_Initialize(const void* Options) {
    s_log("EOS_Initialize");
    return EOS_Success;
}

__declspec(dllexport) void EOS_Logging_SetCallback(const void* Callback) {
    s_log("EOS_Logging_SetCallback");
}

__declspec(dllexport) int32_t EOS_Logging_SetLogLevel(int32_t LogCategory, int32_t LogLevel) {
    s_log("EOS_Logging_SetLogLevel");
    return EOS_Success;
}

__declspec(dllexport) EOS_HPlatform EOS_Platform_Create(const void* Options) {
    s_log("EOS_Platform_Create");
    return &g_platform;
}

__declspec(dllexport) void EOS_Platform_Release(EOS_HPlatform Handle) {
    s_log("EOS_Platform_Release");
}

__declspec(dllexport) void EOS_Platform_Tick(EOS_HPlatform Handle) {
    s_log("EOS_Platform_Tick");
    /* 主循环 tick：派发挂起的登录回调（模拟 EOS 异步回调） */
    if (g_pending_n > 0) {
        char buf[128];
        int n = wsprintfA(buf, "EOS_Platform_Tick: dispatching %d pending login(s)", g_pending_n);
        HANDLE h = CreateFileA("eos_stub.log", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            DWORD w = 0;
            WriteFile(h, buf, (DWORD)n, &w, NULL);
            WriteFile(h, "\n", 1, &w, NULL);
            CloseHandle(h);
        }
    }
    for (int i = 0; i < g_pending_n; i++) {
        fire_pending_login(i);
    }
    g_pending_n = 0;
}

__declspec(dllexport) EOS_HConnect EOS_Platform_GetConnectInterface(EOS_HPlatform Handle) {
    s_log("EOS_Platform_GetConnectInterface");
    return &g_connect;
}

__declspec(dllexport) EOS_HAntiCheatClient EOS_Platform_GetAntiCheatClientInterface(EOS_HPlatform Handle) {
    return &g_anticheat_client;
}

__declspec(dllexport) EOS_HAntiCheatServer EOS_Platform_GetAntiCheatServerInterface(EOS_HPlatform Handle) {
    return &g_anticheat_server;
}

__declspec(dllexport) EOS_HReports EOS_Platform_GetReportsInterface(EOS_HPlatform Handle) {
    return &g_reports;
}

__declspec(dllexport) int32_t EOS_Connect_Login(EOS_HConnect Handle, const void* Options,
                                                void* ClientData, ConnectLoginCallback Delegate) {
    s_log("EOS_Connect_Login");
    {
        char buf[256];
        int n = wsprintfA(buf, "EOS_Connect_Login: Handle=%p Options=%p ClientData=%p Delegate=%p g_pending_n=%d->%d",
                          Handle, Options, ClientData, (void*)Delegate, g_pending_n, g_pending_n + 1);
        HANDLE h = CreateFileA("eos_stub.log", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            DWORD w = 0;
            WriteFile(h, buf, (DWORD)n, &w, NULL);
            WriteFile(h, "\n", 1, &w, NULL);
            CloseHandle(h);
        }
    }
    if (g_pending_n < 16 && Delegate) {
        g_pending[g_pending_n].ClientData = ClientData;
        g_pending[g_pending_n].Callback = Delegate;
        g_pending_n++;
    }
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_Connect_CreateUser(EOS_HConnect Handle, const void* Options,
                                                     void* ClientData, ConnectLoginCallback Delegate) {
    if (g_pending_n < 16 && Delegate) {
        g_pending[g_pending_n].ClientData = ClientData;
        g_pending[g_pending_n].Callback = Delegate;
        g_pending_n++;
    }
    return EOS_Success;
}

__declspec(dllexport) EOS_ProductUserId EOS_ProductUserId_FromString(const char* ProductUserIdString) {
    return &g_puid;
}

__declspec(dllexport) int32_t EOS_ProductUserId_ToString(EOS_ProductUserId AccountId,
                                                         char* OutBuffer, int32_t* InOutBufferLength) {
    /* ★ 客户端 callback sub_180373390 调用时 [InOutBufferLength]=0x21=33，
       s_fake 必须 ≤ 32 字符（+ \0 = 33 字节）才能装下，否则返回 EOS_InvalidParameters
       → callback 走错误路径 → 不发 GetMissionDetailsRequest → 24s 超时 → 10005 */
    static const char s_fake[] = "0002a5f6b7c8d9e0f1a2b3c4d5e6f7a8";  /* 32 字符 */
    size_t need = strlen(s_fake) + 1;  /* 33 */
    int32_t in_len = InOutBufferLength ? *InOutBufferLength : -1;
    if (OutBuffer && InOutBufferLength && *InOutBufferLength >= (int32_t)need) {
        strcpy(OutBuffer, s_fake);
        *InOutBufferLength = (int32_t)(need - 1);
        s_log("EOS_ProductUserId_ToString: OK AccountId写入32字符");
        return EOS_Success;
    }
    if (InOutBufferLength) *InOutBufferLength = (int32_t)need;
    s_log("EOS_ProductUserId_ToString: FAIL buffer不足");
    return -1; /* EOS_InvalidParameters */
}

__declspec(dllexport) const char* EOS_EResult_ToString(int32_t Result) {
    s_log("EOS_EResult_ToString");
    return "EOS_Success";
}

/* ---------------- AntiCheat（全部 no-op 成功） ---------------- */

__declspec(dllexport) int32_t EOS_AntiCheatClient_BeginSession(EOS_HAntiCheatClient Handle, const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_AntiCheatClient_EndSession(EOS_HAntiCheatClient Handle, const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) EOS_NotificationId EOS_AntiCheatClient_AddNotifyMessageToServer(
    EOS_HAntiCheatClient Handle, const void* Options, void* ClientData, const void* NotificationHandler) {
    return 1;
}

__declspec(dllexport) void EOS_AntiCheatClient_RemoveNotifyMessageToServer(EOS_HAntiCheatClient Handle,
                                                                           EOS_NotificationId NotificationId) {
}

__declspec(dllexport) int32_t EOS_AntiCheatClient_ReceiveMessageFromServer(EOS_HAntiCheatClient Handle,
                                                                           const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_AntiCheatServer_BeginSession(EOS_HAntiCheatServer Handle, const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_AntiCheatServer_RegisterClient(EOS_HAntiCheatServer Handle, const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_AntiCheatServer_UnregisterClient(EOS_HAntiCheatServer Handle, const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) int32_t EOS_AntiCheatServer_ReceiveMessageFromClient(EOS_HAntiCheatServer Handle,
                                                                           const void* Options) {
    return EOS_Success;
}

__declspec(dllexport) EOS_NotificationId EOS_AntiCheatServer_AddNotifyClientActionRequired(
    EOS_HAntiCheatServer Handle, const void* Options, void* ClientData, const void* NotificationHandler) {
    return 1;
}

__declspec(dllexport) EOS_NotificationId EOS_AntiCheatServer_AddNotifyMessageToClient(
    EOS_HAntiCheatServer Handle, const void* Options, void* ClientData, const void* NotificationHandler) {
    return 1;
}

/* ---------------- Reports ---------------- */

__declspec(dllexport) int32_t EOS_Reports_SendPlayerBehaviorReport(
    EOS_HReports Handle, const void* Options, void* ClientData, const void* CompletionDelegate) {
    return EOS_Success;
}
