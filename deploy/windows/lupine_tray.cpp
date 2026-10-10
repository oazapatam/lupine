// Tray supervisor for the Windows LUPINE server.
//
// Launches lupine_driver_server.exe (installed next to this executable) with
// no console window, keeps its output in %LOCALAPPDATA%\Lupine\server.log and
// exposes start/stop/restart from a notification-area icon. Settings live in
// %LOCALAPPDATA%\Lupine\server.ini:
//
//   [server]
//   Port=14833
//   LogLevel=debug        ; none | error | debug
//
// The server is placed in a kill-on-close job object, so it cannot outlive
// the tray process.

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
// clang-format off
#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
// clang-format on

#include <string>

namespace {

constexpr UINT WM_TRAY = WM_APP + 1;
constexpr UINT WM_SERVER_EXITED = WM_APP + 2;
constexpr UINT kTrayId = 1;

enum Command : UINT {
  ID_STATUS = 100,
  ID_START,
  ID_STOP,
  ID_RESTART,
  ID_OPEN_LOG,
  ID_EDIT_SETTINGS,
  ID_AUTOSTART,
  ID_EXIT,
};

enum class State { Stopped, Running, Failed };

constexpr wchar_t kRunKey[] = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
constexpr wchar_t kRunValue[] = L"LupineServer";

HWND g_hwnd = nullptr;
HICON g_icons[3] = {};
HANDLE g_job = nullptr;
HANDLE g_process = nullptr;
HANDLE g_watcher = nullptr;
State g_state = State::Stopped;
DWORD g_exit_code = 0;
int g_port = 14833;
std::wstring g_server_path;
std::wstring g_data_dir;
std::wstring g_log_path;
std::wstring g_ini_path;

// Draws a filled circle on a transparent 32x32 icon.
HICON make_icon(COLORREF color) {
  const int size = 32;
  HDC screen = GetDC(nullptr);
  HDC dc = CreateCompatibleDC(screen);
  BITMAPINFO bi = {};
  bi.bmiHeader.biSize = sizeof(bi.bmiHeader);
  bi.bmiHeader.biWidth = size;
  bi.bmiHeader.biHeight = -size;
  bi.bmiHeader.biPlanes = 1;
  bi.bmiHeader.biBitCount = 32;
  bi.bmiHeader.biCompression = BI_RGB;
  void *bits = nullptr;
  HBITMAP color_bmp =
      CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
  HBITMAP mask_bmp = CreateBitmap(size, size, 1, 1, nullptr);

  auto *px = static_cast<DWORD *>(bits);
  const float c = (size - 1) / 2.0f;
  const float r = size / 2.0f - 2.0f;
  const BYTE red = GetRValue(color), green = GetGValue(color),
             blue = GetBValue(color);
  for (int y = 0; y < size; ++y) {
    for (int x = 0; x < size; ++x) {
      float dx = x - c, dy = y - c;
      float d2 = dx * dx + dy * dy;
      DWORD argb = 0;
      if (d2 <= r * r) {
        // Lighter rim to read on both dark and light taskbars.
        bool rim = d2 >= (r - 2.5f) * (r - 2.5f);
        BYTE rr = rim ? 255 : red, gg = rim ? 255 : green, bb = rim ? 255 : blue;
        argb = (0xFFu << 24) | (rr << 16) | (gg << 8) | bb;
      }
      px[y * size + x] = argb;
    }
  }
  ICONINFO ii = {};
  ii.fIcon = TRUE;
  ii.hbmColor = color_bmp;
  ii.hbmMask = mask_bmp;
  HICON icon = CreateIconIndirect(&ii);
  DeleteObject(color_bmp);
  DeleteObject(mask_bmp);
  DeleteDC(dc);
  ReleaseDC(nullptr, screen);
  return icon;
}

std::wstring dirname(const std::wstring &path) {
  size_t pos = path.find_last_of(L"\\/");
  return pos == std::wstring::npos ? L"." : path.substr(0, pos);
}

void load_settings() {
  g_port = static_cast<int>(
      GetPrivateProfileIntW(L"server", L"Port", 14833, g_ini_path.c_str()));
  if (g_port < 1 || g_port > 65535) {
    g_port = 14833;
  }
}

void ensure_default_ini() {
  if (GetFileAttributesW(g_ini_path.c_str()) != INVALID_FILE_ATTRIBUTES) {
    return;
  }
  WritePrivateProfileStringW(L"server", L"Port", L"14833", g_ini_path.c_str());
  WritePrivateProfileStringW(L"server", L"LogLevel", L"error",
                             g_ini_path.c_str());
}

void update_icon(bool add = false) {
  NOTIFYICONDATAW nid = {};
  nid.cbSize = sizeof(nid);
  nid.hWnd = g_hwnd;
  nid.uID = kTrayId;
  nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE;
  nid.uCallbackMessage = WM_TRAY;
  nid.hIcon = g_icons[static_cast<int>(g_state)];
  const wchar_t *text = L"stopped";
  if (g_state == State::Running) {
    text = L"running";
  } else if (g_state == State::Failed) {
    text = L"stopped (error)";
  }
  wchar_t tip[128];
  if (g_state == State::Running) {
    wsprintfW(tip, L"LUPINE server - %s on port %d", text, g_port);
  } else {
    wsprintfW(tip, L"LUPINE server - %s", text);
  }
  wcsncpy_s(nid.szTip, tip, _TRUNCATE);
  Shell_NotifyIconW(add ? NIM_ADD : NIM_MODIFY, &nid);
}

void balloon(const wchar_t *title, const wchar_t *text) {
  NOTIFYICONDATAW nid = {};
  nid.cbSize = sizeof(nid);
  nid.hWnd = g_hwnd;
  nid.uID = kTrayId;
  nid.uFlags = NIF_INFO;
  nid.dwInfoFlags = NIIF_INFO;
  wcsncpy_s(nid.szInfoTitle, title, _TRUNCATE);
  wcsncpy_s(nid.szInfo, text, _TRUNCATE);
  Shell_NotifyIconW(NIM_MODIFY, &nid);
}

// Bumped per launch so an exit notification from a server that was already
// replaced (restart) cannot be mistaken for the current one, even if the OS
// reuses the process handle value.
LONG g_generation = 0;

struct WatchArgs {
  HANDLE process;
  LONG generation;
};

DWORD WINAPI watch_process(LPVOID param) {
  WatchArgs args = *static_cast<WatchArgs *>(param);
  delete static_cast<WatchArgs *>(param);
  WaitForSingleObject(args.process, INFINITE);
  PostMessageW(g_hwnd, WM_SERVER_EXITED, static_cast<WPARAM>(args.generation),
               0);
  return 0;
}

bool server_running() { return g_process != nullptr; }

void start_server() {
  if (server_running()) {
    return;
  }
  load_settings();

  wchar_t port[16];
  wsprintfW(port, L"%d", g_port);
  SetEnvironmentVariableW(L"LUPINE_PORT", port);
  wchar_t level[32] = {};
  GetPrivateProfileStringW(L"server", L"LogLevel", L"error", level, 32,
                           g_ini_path.c_str());
  SetEnvironmentVariableW(L"LUPINE_LOG_LEVEL", level);

  SECURITY_ATTRIBUTES sa = {sizeof(sa), nullptr, TRUE};
  HANDLE log = CreateFileW(g_log_path.c_str(), FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
  if (log == INVALID_HANDLE_VALUE) {
    log = nullptr;
  }

  STARTUPINFOW si = {};
  si.cb = sizeof(si);
  if (log) {
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = nullptr;
    si.hStdOutput = log;
    si.hStdError = log;
  }
  PROCESS_INFORMATION pi = {};
  std::wstring cmd = L"\"" + g_server_path + L"\"";
  BOOL ok = CreateProcessW(g_server_path.c_str(), &cmd[0], nullptr, nullptr,
                           TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr,
                           g_data_dir.c_str(), &si, &pi);
  if (log) {
    CloseHandle(log);
  }
  if (!ok) {
    g_state = State::Failed;
    g_exit_code = GetLastError();
    update_icon();
    balloon(L"LUPINE server", L"Could not start lupine_driver_server.exe.");
    return;
  }
  if (g_job) {
    AssignProcessToJobObject(g_job, pi.hProcess);
  }
  ResumeThread(pi.hThread);
  CloseHandle(pi.hThread);
  g_process = pi.hProcess;
  g_state = State::Running;
  ++g_generation;
  g_watcher = CreateThread(nullptr, 0, watch_process,
                           new WatchArgs{g_process, g_generation}, 0, nullptr);
  update_icon();
}

void stop_server() {
  if (!server_running()) {
    return;
  }
  HANDLE process = g_process;
  // The watcher posts WM_SERVER_EXITED when this lands; clear first so that
  // message is recognised as an intentional stop rather than a crash.
  g_process = nullptr;
  g_state = State::Stopped;
  ++g_generation;
  TerminateProcess(process, 0);
  if (g_watcher) {
    WaitForSingleObject(g_watcher, 5000);
    CloseHandle(g_watcher);
    g_watcher = nullptr;
  }
  CloseHandle(process);
  update_icon();
}

bool autostart_enabled() {
  HKEY key;
  if (RegOpenKeyExW(HKEY_CURRENT_USER, kRunKey, 0, KEY_READ, &key) !=
      ERROR_SUCCESS) {
    return false;
  }
  bool present =
      RegQueryValueExW(key, kRunValue, nullptr, nullptr, nullptr, nullptr) ==
      ERROR_SUCCESS;
  RegCloseKey(key);
  return present;
}

void set_autostart(bool enable) {
  HKEY key;
  if (RegOpenKeyExW(HKEY_CURRENT_USER, kRunKey, 0, KEY_SET_VALUE, &key) !=
      ERROR_SUCCESS) {
    return;
  }
  if (enable) {
    wchar_t self[MAX_PATH];
    GetModuleFileNameW(nullptr, self, MAX_PATH);
    std::wstring value = L"\"" + std::wstring(self) + L"\"";
    RegSetValueExW(key, kRunValue, 0, REG_SZ,
                   reinterpret_cast<const BYTE *>(value.c_str()),
                   static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)));
  } else {
    RegDeleteValueW(key, kRunValue);
  }
  RegCloseKey(key);
}

void show_menu() {
  HMENU menu = CreatePopupMenu();
  wchar_t status[96];
  if (g_state == State::Running) {
    wsprintfW(status, L"Server running on port %d", g_port);
  } else if (g_state == State::Failed) {
    wsprintfW(status, L"Server stopped (exit code %lu)", g_exit_code);
  } else {
    wcscpy_s(status, L"Server stopped");
  }
  AppendMenuW(menu, MF_STRING | MF_DISABLED, ID_STATUS, status);
  AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(menu, MF_STRING | (server_running() ? MF_GRAYED : 0), ID_START,
              L"Start");
  AppendMenuW(menu, MF_STRING | (server_running() ? 0 : MF_GRAYED), ID_STOP,
              L"Stop");
  AppendMenuW(menu, MF_STRING, ID_RESTART, L"Restart");
  AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(menu, MF_STRING, ID_OPEN_LOG, L"Open log");
  AppendMenuW(menu, MF_STRING, ID_EDIT_SETTINGS, L"Edit settings...");
  AppendMenuW(menu, MF_STRING | (autostart_enabled() ? MF_CHECKED : 0),
              ID_AUTOSTART, L"Start with Windows");
  AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(menu, MF_STRING, ID_EXIT, L"Exit");

  POINT pt;
  GetCursorPos(&pt);
  // Required so the menu dismisses when the user clicks elsewhere.
  SetForegroundWindow(g_hwnd);
  TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, pt.x, pt.y, 0,
                 g_hwnd, nullptr);
  PostMessageW(g_hwnd, WM_NULL, 0, 0);
  DestroyMenu(menu);
}

LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  static UINT taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");
  if (msg == taskbar_created) {
    update_icon(true);
    return 0;
  }
  switch (msg) {
  case WM_TRAY:
    if (lp == WM_RBUTTONUP || lp == WM_LBUTTONUP || lp == WM_CONTEXTMENU) {
      show_menu();
    }
    return 0;
  case WM_SERVER_EXITED: {
    if (static_cast<LONG>(wp) != g_generation || !g_process) {
      return 0; // stopped on purpose; stop_server() already cleaned up
    }
    DWORD code = 0;
    GetExitCodeProcess(g_process, &code);
    g_exit_code = code;
    CloseHandle(g_process);
    if (g_watcher) {
      CloseHandle(g_watcher);
      g_watcher = nullptr;
    }
    g_process = nullptr;
    g_state = code == 0 ? State::Stopped : State::Failed;
    update_icon();
    if (code != 0) {
      balloon(L"LUPINE server stopped",
              L"The server exited unexpectedly. Open the log for details.");
    }
    return 0;
  }
  case WM_COMMAND:
    switch (LOWORD(wp)) {
    case ID_START:
      start_server();
      break;
    case ID_STOP:
      stop_server();
      break;
    case ID_RESTART:
      stop_server();
      start_server();
      break;
    case ID_OPEN_LOG:
      ShellExecuteW(hwnd, L"open", L"notepad.exe",
                    (L"\"" + g_log_path + L"\"").c_str(), nullptr, SW_SHOW);
      break;
    case ID_EDIT_SETTINGS:
      ShellExecuteW(hwnd, L"open", L"notepad.exe",
                    (L"\"" + g_ini_path + L"\"").c_str(), nullptr, SW_SHOW);
      break;
    case ID_AUTOSTART:
      set_autostart(!autostart_enabled());
      break;
    case ID_EXIT:
      DestroyWindow(hwnd);
      break;
    }
    return 0;
  case WM_DESTROY: {
    stop_server();
    NOTIFYICONDATAW nid = {};
    nid.cbSize = sizeof(nid);
    nid.hWnd = hwnd;
    nid.uID = kTrayId;
    Shell_NotifyIconW(NIM_DELETE, &nid);
    PostQuitMessage(0);
    return 0;
  }
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
  HANDLE single = CreateMutexW(nullptr, TRUE, L"Local\\LupineServerTray");
  if (GetLastError() == ERROR_ALREADY_EXISTS) {
    return 0;
  }

  wchar_t self[MAX_PATH];
  GetModuleFileNameW(nullptr, self, MAX_PATH);
  g_server_path = dirname(self) + L"\\lupine_driver_server.exe";

  wchar_t *local = nullptr;
  if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr,
                                     &local))) {
    g_data_dir = std::wstring(local) + L"\\Lupine";
    CoTaskMemFree(local);
  } else {
    g_data_dir = dirname(self);
  }
  CreateDirectoryW(g_data_dir.c_str(), nullptr);
  g_log_path = g_data_dir + L"\\server.log";
  g_ini_path = g_data_dir + L"\\server.ini";
  ensure_default_ini();
  load_settings();

  if (GetFileAttributesW(g_server_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
    MessageBoxW(nullptr,
                L"lupine_driver_server.exe was not found next to the tray "
                L"application.",
                L"LUPINE server", MB_ICONERROR);
    return 1;
  }

  g_job = CreateJobObjectW(nullptr, nullptr);
  if (g_job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = {};
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(g_job, JobObjectExtendedLimitInformation, &info,
                            sizeof(info));
  }

  g_icons[static_cast<int>(State::Stopped)] = make_icon(RGB(128, 128, 128));
  g_icons[static_cast<int>(State::Running)] = make_icon(RGB(46, 160, 67));
  g_icons[static_cast<int>(State::Failed)] = make_icon(RGB(207, 34, 46));

  WNDCLASSW wc = {};
  wc.lpfnWndProc = wnd_proc;
  wc.hInstance = instance;
  wc.lpszClassName = L"LupineServerTray";
  RegisterClassW(&wc);
  g_hwnd = CreateWindowExW(0, wc.lpszClassName, L"LUPINE server", 0, 0, 0, 0, 0,
                           HWND_MESSAGE, nullptr, instance, nullptr);

  update_icon(true);
  start_server();

  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
  for (HICON icon : g_icons) {
    DestroyIcon(icon);
  }
  if (single) {
    CloseHandle(single);
  }
  return 0;
}
