/*
 * hid_bridge — ESP32-S3 当 Mac ↔ iPhone 的 HID 桥。
 *
 *   Mac ──USB 串口(115200, 文本行)──▶ ESP32-S3 ──BLE HID(鼠标+键盘+Consumer)──▶ iPhone
 *
 * 协议定义在 ../../hil/protocol.py（唯一定义处），Python 参考实现在 hil/link.py 的 FirmwareModel。
 * 每行命令回一行 "OK ..." 或 "ERR ..."；以 '#' 开头的行是日志，Mac 端忽略。
 *
 * HID 空闲（0.3.0 起）：iOS 只要连着一个蓝牙键盘就把所有 App 的屏幕键盘藏起来，所以板子不能常年挂在 iPhone 上。
 *   hididle 秒（默认 120）没有需要 HID 的命令 → 断开 iPhone（不清绑定）→ 不再广播，直到收到 WAKE 或需要 HID 的命令
 *   （来自串口或已连着的 Mac 指令通道）→ 恢复广播，iPhone 通常 1~3 s 自动连回来；没连回来就让用户到 iPhone 设置→蓝牙 点一下 HIL-Bridge。
 *   上电时正常广播。空闲且 Mac 也没连着时板子对外没有广播，新起的守护进程连不上：给板子断电重启（或按 EN 键）即可。
 *
 * 后端：
 *   HID_BACKEND=1  BLE HID（默认，推荐：iPhone 的 USB 口留给 Mac 录屏）。命令走 Serial（板子的 USB 口，Hardware CDC）。
 *   HID_BACKEND=2  USB HID（TinyUSB）：板子的 native USB 口插 iPhone/iPad；命令走 Serial0（UART 桥接口 "COM"）。
 *
 * 依赖：arduino-esp32 3.x，NimBLE-Arduino 2.x（BLE 后端）。刷写步骤见 ../FLASH.md。
 */
#include <Arduino.h>
#include <Preferences.h>

#ifndef HID_BACKEND
#define HID_BACKEND 1
#endif

#if HID_BACKEND == 1
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#define CMD_SERIAL Serial
#define BACKEND_NAME "ble"
#else
#include "USB.h"
#include "USBHIDConsumerControl.h"
#include "USBHIDKeyboard.h"
#include "USBHIDMouse.h"
#define CMD_SERIAL Serial0
#define BACKEND_NAME "usb"
#endif

#define FW_VERSION "0.3.1"
#define DEFAULT_NAME "HIL-Bridge"
#define BAUD 115200

// ------------------------------------------------------------------ 配置（NVS 持久化）
struct Config {
  char name[24];
  uint16_t mvdelay;    // 拆分后的鼠标报告之间 ms
  uint16_t typedelay;  // 键盘按下/抬起之间 ms
  uint16_t appearance; // BLE 外观：0x03C2 鼠标 / 0x03C1 键盘 / 0x03C0 通用 HID
  char token[17];      // BLE 指令通道的口令（Mac 连上后要先 AUTH <token>）
  char hidpeer[20];    // 允许接收 HID 报告的对端地址（第一次订阅时自动记住 = iPhone），防止 Mac 接管
  uint16_t hididle;    // 空闲多少秒后主动断开 iPhone 的 HID 连接（0 = 永不，即一直连着）。
                       // 为什么：iOS 只要连着一个蓝牙键盘就把所有 App 的屏幕键盘藏起来；板子常年通电 = 手机永远弹不出键盘。
} cfg;
Preferences prefs;

static void loadConfig() {
  prefs.begin("hil", true);
  String n = prefs.getString("name", DEFAULT_NAME);
  strncpy(cfg.name, n.c_str(), sizeof(cfg.name) - 1);
  cfg.name[sizeof(cfg.name) - 1] = 0;
  cfg.mvdelay = prefs.getUShort("mvdelay", 25);  // ≥ BLE 连接间隔，报告才不会在协议栈里排队（实测 8 会排队）
  cfg.typedelay = prefs.getUShort("typedelay", 12);
  cfg.appearance = prefs.getUShort("appear", 0);  // 0 = 通用外观（见 hidBegin 里的说明）
  cfg.hididle = prefs.getUShort("hididle", 120);
  String t = prefs.getString("token", "");
  prefs.end();
  if (t.length() < 4) {  // 第一次启动：随机生成口令并存下来
    char buf[17];
    snprintf(buf, sizeof(buf), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    t = String(buf).substring(0, 12);
    prefs.begin("hil", false); prefs.putString("token", t); prefs.end();
  }
  strncpy(cfg.token, t.c_str(), sizeof(cfg.token) - 1); cfg.token[sizeof(cfg.token) - 1] = 0;
  prefs.begin("hil", true);
  String hp = prefs.getString("hidpeer", "");
  prefs.end();
  strncpy(cfg.hidpeer, hp.c_str(), sizeof(cfg.hidpeer) - 1); cfg.hidpeer[sizeof(cfg.hidpeer) - 1] = 0;
}
static void saveConfig() {
  prefs.begin("hil", false);
  prefs.putString("name", cfg.name);
  prefs.putUShort("mvdelay", cfg.mvdelay);
  prefs.putUShort("typedelay", cfg.typedelay);
  prefs.putUShort("appear", cfg.appearance);
  prefs.putString("token", cfg.token);
  prefs.putString("hidpeer", cfg.hidpeer);
  prefs.putUShort("hididle", cfg.hididle);
  prefs.end();
}

// ------------------------------------------------------------------ HID 状态
static uint8_t g_mods = 0;
static uint8_t g_keys[6] = {0};
static uint8_t g_buttons = 0;
static volatile bool g_connected = false;  // 链路已连接
static volatile bool g_ready = false;      // 可以发报告（BLE：已加密）
static volatile bool g_evt_conn = false, g_evt_disc = false;  // 回调里置位，loop 里打日志
static uint32_t g_txfail = 0;  // notify() 返回 false 的次数（排队满/未订阅）

// ---- HID 空闲/唤醒（只对 BLE 后端有意义）----
// 空闲：断开 iPhone，并停止广播，直到被 WAKE / 需要 HID 的命令唤醒。醒着（有活动）：正常广播，iPhone 按绑定自动连回来。
static bool g_hidAwake = true;
static uint32_t g_lastActMs = 0;        // 最近一次活动（需要 HID 的命令 / WAKE）
static volatile bool g_evt_adv = false; // 连接数变了 / 状态变了：loop() 里重新决定广播（回调里不碰广播，避免两个任务抢）
static volatile bool g_evt_idle = false, g_evt_wake = false;
struct LogMsg { char s[96]; };
static QueueHandle_t g_logQueue = nullptr;   // 回调任务里产生的日志行，loop() 里打到串口（诊断用）
static void qlog(const char* fmt, ...) {
  if (!g_logQueue) return;
  LogMsg m; va_list ap; va_start(ap, fmt); vsnprintf(m.s, sizeof(m.s), fmt, ap); va_end(ap);
  xQueueSend(g_logQueue, &m, 0);
}

// ---- 回复路由：命令来自串口还是 BLE 指令通道 ----
enum Sink { SINK_SERIAL = 0, SINK_BLE = 1 };
static Sink g_sink = SINK_SERIAL;
static uint16_t g_sinkConn = 0xFFFF;

// ------------------------------------------------------------------ BLE 后端
#if HID_BACKEND == 1
// 报告 ID
#define RID_KBD 1
#define RID_MOUSE 2
#define RID_CONSUMER 3

static const uint8_t REPORT_MAP[] = {
  // ---- 键盘 (ID 1) ----
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, RID_KBD,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,  // 修饰键
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,                                                              // 保留
  0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,                          // LED 输出
  0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,  // 6 个键
  0xC0,
  // ---- 鼠标 (ID 2) ----
  0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, RID_MOUSE, 0x09, 0x01, 0xA1, 0x00,
  0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,  // 3 键
  0x95, 0x01, 0x75, 0x05, 0x81, 0x03,                                                              // 填充
  0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,  // X Y 滚轮
  0xC0, 0xC0,
  // ---- Consumer (ID 3) ----
  0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, RID_CONSUMER,
  0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
  0xC0,
};

static NimBLEServer* g_server = nullptr;
static NimBLEHIDDevice* g_hid = nullptr;
static NimBLECharacteristic *g_inKbd = nullptr, *g_inMouse = nullptr, *g_inConsumer = nullptr;

// ---- 指令通道：Nordic UART 风格的服务（Mac 用 CoreBluetooth 连，不绑定、不加密，靠 AUTH 口令）----
#define NUS_SERVICE "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_RX      "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"   // Mac → 板子：写命令行
#define NUS_TX      "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"   // 板子 → Mac：通知回复
static NimBLECharacteristic *g_nusRx = nullptr, *g_nusTx = nullptr;
static uint16_t g_hidConn = BLE_HS_CONN_HANDLE_NONE;   // 订阅了鼠标输入报告的连接 = iPhone
static uint16_t g_cmdConn = BLE_HS_CONN_HANDLE_NONE;   // 已通过 AUTH 的指令连接 = Mac
struct BleMsg { uint16_t conn; char line[300]; };
static QueueHandle_t g_bleQueue = nullptr;               // BLE 收到的整行命令，交给 loop() 执行（回调里不能 delay）
static char g_bleBuf[300]; static size_t g_bleLen = 0;
static uint32_t g_bleRxLines = 0, g_bleTxReplies = 0, g_bleWrites = 0;  // 对账用计数

static const char* g_advMode = "off";   // 当前广播：pub / off（满了，或空闲中）

static bool hidAlwaysOn() { return cfg.hididle == 0 || cfg.appearance != 0; }  // appearance≠0 = 正在给新 iPhone 配对，别睡

// 按当前状态决定广播。只在 loop() 任务里调用。
static void updateAdvertising() {
  if (!g_server) return;
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->stop();
  int n = g_server->getConnectedCount();
  bool ok = false;
  if (n >= 2) { g_advMode = "off"; }                              // iPhone + Mac 都在，满了
  else if (!g_hidAwake && !hidAlwaysOn()) { g_advMode = "off"; }  // 空闲中：不广播，iPhone 就不会连回来把键盘藏掉
  else { ok = adv->start(); g_advMode = "pub"; }
  CMD_SERIAL.printf("# adv %s ok=%d conns=%d\n", g_advMode, ok ? 1 : 0, n);
}
static void hidGoIdle() {
  if (!g_hidAwake) return;
  g_hidAwake = false; g_evt_idle = true;
  if (g_hidConn != BLE_HS_CONN_HANDLE_NONE) g_server->disconnect(g_hidConn);   // 保留绑定，只断连接；onDisconnect 会清状态
  g_evt_adv = true;
}
static void hidWake() {   // 恢复广播 + 重置计时；iPhone 通常 1~3 s 自动连回来，没连就让用户到设置→蓝牙点一下
  g_lastActMs = millis();
  if (g_hidAwake) return;
  g_hidAwake = true; g_evt_wake = true; g_evt_adv = true;
}
static const char* hidStateStr() { return g_ready ? "on" : (g_hidAwake || hidAlwaysOn()) ? "waking" : "idle"; }

class MouseReportCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic* c, NimBLEConnInfo& ci, uint16_t subValue) override {
    std::string addr = ci.getIdAddress().toString();
    qlog("# sub h=%d id=%s v=%d", ci.getConnHandle(), addr.c_str(), subValue);
    if (subValue & 1) {
      if (cfg.hidpeer[0] == 0) { strncpy(cfg.hidpeer, addr.c_str(), sizeof(cfg.hidpeer) - 1); saveConfig(); }  // 第一次：记住它
      if (strcasecmp(addr.c_str(), cfg.hidpeer) != 0) return;  // 不是记住的那台（比如 Mac 自己来配对）：不当 HID 目标
      g_hidConn = ci.getConnHandle(); g_ready = true; g_evt_conn = true;
      g_hidAwake = true; g_lastActMs = millis();   // 用户手工点连也算醒来，hididle 从现在起算
    } else if (ci.getConnHandle() == g_hidConn) { g_hidConn = BLE_HS_CONN_HANDLE_NONE; g_ready = false; }
  }
};

class NusRxCB : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo& ci) override {
    std::string v = c->getValue();
    g_bleWrites++;
    for (char ch : v) {
      if (ch == '\n') {
        g_bleBuf[g_bleLen] = 0; g_bleRxLines++;
        BleMsg m; m.conn = ci.getConnHandle(); strncpy(m.line, g_bleBuf, sizeof(m.line) - 1); m.line[sizeof(m.line) - 1] = 0;
        xQueueSend(g_bleQueue, &m, 0);
        g_bleLen = 0;
      } else if (g_bleLen < sizeof(g_bleBuf) - 1) {
        g_bleBuf[g_bleLen++] = ch;
      } else {
        g_bleLen = 0;
      }
    }
  }
};

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& ci) override {
    g_connected = true;
    std::string id = ci.getIdAddress().toString();
    bool isHid = cfg.hidpeer[0] == 0 || strcasecmp(id.c_str(), cfg.hidpeer) == 0;
    qlog("# conn h=%d peer=%s id=%s hid=%d itvl=%d", ci.getConnHandle(), ci.getAddress().toString().c_str(), id.c_str(), isHid ? 1 : 0, ci.getConnInterval());
    // 请求短连接间隔（11.25~15 ms），鼠标才跟手；对方不接受就用它自己的
    s->updateConnParams(ci.getConnHandle(), 9, 12, 0, 400);
    g_evt_adv = true;   // 还有空位就继续广播（由 loop 里的 updateAdvertising 决定用哪个地址）
  }
  void onConnParamsUpdate(NimBLEConnInfo& ci) override {
    qlog("# params h=%d itvl=%d lat=%d to=%d", ci.getConnHandle(), ci.getConnInterval(), ci.getConnLatency(), ci.getConnTimeout());
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& ci, int reason) override {
    uint16_t h = ci.getConnHandle();
    qlog("# disc h=%d peer=%s reason=0x%x", h, ci.getAddress().toString().c_str(), reason);
    if (h == g_hidConn) { g_hidConn = BLE_HS_CONN_HANDLE_NONE; g_ready = false; g_evt_disc = true; }
    if (h == g_cmdConn) { g_cmdConn = BLE_HS_CONN_HANDLE_NONE; }
    g_connected = s->getConnectedCount() > 1;  // 回调时本连接还计在内
    g_evt_adv = true;
  }
  void onAuthenticationComplete(NimBLEConnInfo& ci) override {
    // 不再因为未加密就断开：Mac 的指令连接本来就不加密；iPhone 的 HID 由 iOS 自己要求加密
    qlog("# auth h=%d enc=%d bonded=%d", ci.getConnHandle(), ci.isEncrypted() ? 1 : 0, ci.isBonded() ? 1 : 0);
  }
};

static void hidBegin() {
  g_bleQueue = xQueueCreate(8, sizeof(BleMsg));
  g_logQueue = xQueueCreate(16, sizeof(LogMsg));
  NimBLEDevice::init(cfg.name);
  NimBLEDevice::setMTU(247);                                      // 回复一行可达 ~80 字节，别卡在 20 字节
  NimBLEDevice::setSecurityAuth(true, false, true);              // 绑定 + Secure Connections，Just Works（只有 iPhone 的 HID 会触发）
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  g_server = NimBLEDevice::createServer();
  g_server->setCallbacks(new ServerCB());
  g_server->advertiseOnDisconnect(false);   // 广播由 updateAdvertising() 按醒/睡状态决定，不让库自己重开
  g_hid = new NimBLEHIDDevice(g_server);
  g_hid->setManufacturer("hil");
  g_hid->setPnp(0x02, 0xE502, 0xA111, 0x0210);
  g_hid->setHidInfo(0x00, 0x02);
  g_hid->setReportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
  g_inKbd = g_hid->getInputReport(RID_KBD);
  g_hid->getOutputReport(RID_KBD);  // LED 输出报告（iOS 可能写）
  g_inMouse = g_hid->getInputReport(RID_MOUSE);
  g_inMouse->setCallbacks(new MouseReportCB());
  g_inConsumer = g_hid->getInputReport(RID_CONSUMER);
  g_hid->setBatteryLevel(100);
  // 指令通道（不要求加密：Mac 不和板子绑定，否则 macOS 会把它当鼠标键盘接管）
  NimBLEService* nus = g_server->createService(NUS_SERVICE);
  g_nusRx = nus->createCharacteristic(NUS_RX, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  g_nusRx->setCallbacks(new NusRxCB());
  g_nusTx = nus->createCharacteristic(NUS_TX, NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ);
  g_server->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  if (cfg.appearance) adv->setAppearance(cfg.appearance);   // 默认 0：不声明 HID 外观，macOS 就不会自作主张来配对
  if (cfg.appearance) adv->addServiceUUID(g_hid->getHidService()->getUUID());  // 第一次给 iPhone 配对时可临时 CFG appearance 0x3C2
  else adv->addServiceUUID(NUS_SERVICE);
  adv->setName(cfg.name);
  adv->enableScanResponse(true);
  g_hidAwake = true; g_lastActMs = millis();   // 上电正常广播：若 iPhone 还在找它（上次是断电/链路丢失）会连上，hididle 后再断
  updateAdvertising();
}
static inline bool hidReady() { return g_ready; }
static void hidMouse(int8_t dx, int8_t dy, int8_t wheel) {
  uint8_t r[4] = {g_buttons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
  if (!g_inMouse->notify(r, sizeof(r), g_hidConn)) g_txfail++;   // 只发给 iPhone 那条连接
}
static void hidKeyboard() {
  uint8_t r[8] = {g_mods, 0, g_keys[0], g_keys[1], g_keys[2], g_keys[3], g_keys[4], g_keys[5]};
  if (!g_inKbd->notify(r, sizeof(r), g_hidConn)) g_txfail++;
}
static void hidConsumer(uint16_t usage) {
  uint8_t r[2] = {(uint8_t)(usage & 0xFF), (uint8_t)(usage >> 8)};
  g_inConsumer->notify(r, 2, g_hidConn);
  delay(20);
  r[0] = r[1] = 0;
  g_inConsumer->notify(r, 2, g_hidConn);
}
// 回复经 BLE 指令通道发出：按 MTU 分片
static void bleReply(const char* s) {
  if (g_nusTx == nullptr || g_sinkConn == BLE_HS_CONN_HANDLE_NONE) return;
  size_t mtu = 23;
  mtu = g_server->getPeerInfoByHandle(g_sinkConn).getMTU();
  size_t chunk = (mtu > 3) ? mtu - 3 : 20;
  std::string out(s); out += "\n";
  g_bleTxReplies++;
  for (size_t i = 0; i < out.size(); i += chunk) {
    g_nusTx->notify((const uint8_t*)out.data() + i, std::min(chunk, out.size() - i), g_sinkConn);
  }
}

// ------------------------------------------------------------------ USB 后端
#else
static USBHIDMouse Mouse;
static USBHIDKeyboard Keyboard;
static USBHIDConsumerControl Consumer;
static uint8_t g_usbButtons = 0;

static void usbEvent(void* arg, esp_event_base_t base, int32_t id, void* data) {
  if (base == ARDUINO_USB_EVENTS) {
    if (id == ARDUINO_USB_STARTED_EVENT || id == ARDUINO_USB_RESUME_EVENT) { g_connected = g_ready = true; g_evt_conn = true; }
    if (id == ARDUINO_USB_STOPPED_EVENT || id == ARDUINO_USB_SUSPEND_EVENT) { g_connected = g_ready = false; g_evt_disc = true; }
  }
}
static void hidBegin() {
  USB.onEvent(usbEvent);
  USB.productName(cfg.name);
  USB.manufacturerName("hil");
  Mouse.begin();
  Keyboard.begin();
  Consumer.begin();
  USB.begin();
}
static inline bool hidReady() { return g_ready; }
static void hidMouse(int8_t dx, int8_t dy, int8_t wheel) {
  if (g_buttons != g_usbButtons) { Mouse.buttons(g_buttons); g_usbButtons = g_buttons; }
  if (dx || dy || wheel) Mouse.move(dx, dy, wheel, 0);
}
static void hidKeyboard() {
  KeyReport kr;
  kr.modifiers = g_mods;
  kr.reserved = 0;
  memcpy(kr.keys, g_keys, 6);
  Keyboard.sendReport(&kr);
}
static void hidConsumer(uint16_t usage) {
  Consumer.press(usage);
  delay(20);
  Consumer.release();
}
static void bleReply(const char* s) {}
static bool hidAlwaysOn() { return true; }
static void hidGoIdle() {}
static void hidWake() { g_lastActMs = millis(); }
static void updateAdvertising() {}
static const char* hidStateStr() { return g_ready ? "on" : "waking"; }
#endif

// ------------------------------------------------------------------ 键名表（和 protocol.py 一致）
struct KeyName { const char* name; uint8_t usage; };
static const KeyName KEY_NAMES[] = {
  {"ENTER", 0x28}, {"RETURN", 0x28}, {"ESC", 0x29}, {"ESCAPE", 0x29}, {"BACKSPACE", 0x2A}, {"BS", 0x2A}, {"TAB", 0x2B},
  {"SPACE", 0x2C}, {"MINUS", 0x2D}, {"EQUAL", 0x2E}, {"LBRACKET", 0x2F}, {"RBRACKET", 0x30}, {"BACKSLASH", 0x31},
  {"SEMICOLON", 0x33}, {"QUOTE", 0x34}, {"GRAVE", 0x35}, {"COMMA", 0x36}, {"PERIOD", 0x37}, {"SLASH", 0x38},
  {"CAPSLOCK", 0x39}, {"F1", 0x3A}, {"F2", 0x3B}, {"F3", 0x3C}, {"F4", 0x3D}, {"F5", 0x3E}, {"F6", 0x3F},
  {"F7", 0x40}, {"F8", 0x41}, {"F9", 0x42}, {"F10", 0x43}, {"F11", 0x44}, {"F12", 0x45},
  {"PRINTSCREEN", 0x46}, {"SCROLLLOCK", 0x47}, {"PAUSE", 0x48}, {"INSERT", 0x49}, {"HOME", 0x4A}, {"PAGEUP", 0x4B},
  {"DELETE", 0x4C}, {"DEL", 0x4C}, {"END", 0x4D}, {"PAGEDOWN", 0x4E}, {"RIGHT", 0x4F}, {"LEFT", 0x50}, {"DOWN", 0x51}, {"UP", 0x52},
};
struct ModName { const char* name; uint8_t bit; };
static const ModName MOD_NAMES[] = {
  {"CTRL", 0x01}, {"CONTROL", 0x01}, {"SHIFT", 0x02}, {"ALT", 0x04}, {"OPTION", 0x04}, {"GUI", 0x08}, {"CMD", 0x08},
  {"WIN", 0x08}, {"META", 0x08}, {"RCTRL", 0x10}, {"RSHIFT", 0x20}, {"RALT", 0x40}, {"RGUI", 0x80},
};
struct ConsumerName { const char* name; uint16_t usage; };
static const ConsumerName CONSUMER_NAMES[] = {
  {"HOME", 0x0223}, {"BACK", 0x0224}, {"VOLUP", 0x00E9}, {"VOLDN", 0x00EA}, {"MUTE", 0x00E2},
  {"PLAY", 0x00CD}, {"NEXT", 0x00B5}, {"PREV", 0x00B6}, {"BRIGHTUP", 0x006F}, {"BRIGHTDN", 0x0070},
};

// ASCII → (需要 shift, usage)。返回 false = 不可输入。US 布局。
static bool asciiToUsage(char c, uint8_t& mods, uint8_t& usage) {
  mods = 0;
  if (c >= 'a' && c <= 'z') { usage = 0x04 + (c - 'a'); return true; }
  if (c >= 'A' && c <= 'Z') { usage = 0x04 + (c - 'A'); mods = 0x02; return true; }
  if (c >= '1' && c <= '9') { usage = 0x1E + (c - '1'); return true; }
  if (c == '0') { usage = 0x27; return true; }
  struct CU { char c; uint8_t usage; bool shift; };
  static const CU table[] = {  // 显式配对，别再用转义字符串（曾因一个反斜杠整表错位）
    {'-', 0x2D, false}, {'=', 0x2E, false}, {'[', 0x2F, false}, {']', 0x30, false}, {'\\', 0x31, false},
    {';', 0x33, false}, {'\'', 0x34, false}, {'`', 0x35, false}, {',', 0x36, false}, {'.', 0x37, false},
    {'/', 0x38, false}, {' ', 0x2C, false}, {'\n', 0x28, false}, {'\t', 0x2B, false}, {'\b', 0x2A, false}, {27, 0x29, false},
    {')', 0x27, true}, {'!', 0x1E, true}, {'@', 0x1F, true}, {'#', 0x20, true}, {'$', 0x21, true}, {'%', 0x22, true},
    {'^', 0x23, true}, {'&', 0x24, true}, {'*', 0x25, true}, {'(', 0x26, true}, {'_', 0x2D, true}, {'+', 0x2E, true},
    {'{', 0x2F, true}, {'}', 0x30, true}, {'|', 0x31, true}, {':', 0x33, true}, {'"', 0x34, true}, {'~', 0x35, true},
    {'<', 0x36, true}, {'>', 0x37, true}, {'?', 0x38, true},
  };
  for (auto& e : table) if (e.c == c) { usage = e.usage; mods = e.shift ? 0x02 : 0; return true; }
  return false;
}

static bool strieq(const char* a, const char* b) { return strcasecmp(a, b) == 0; }

// "GUI+SHIFT+H" / "ENTER" / "a" → mods, usage（只有修饰键时 usage=0）
static bool parseKeySpec(char* spec, uint8_t& mods, uint8_t& usage) {
  mods = 0; usage = 0;
  if (strlen(spec) == 1) { uint8_t m; if (!asciiToUsage(spec[0], m, usage)) return false; mods = m; return true; }
  char* save = nullptr;
  for (char* tok = strtok_r(spec, "+-", &save); tok; tok = strtok_r(nullptr, "+-", &save)) {
    bool found = false;
    for (auto& m : MOD_NAMES) if (strieq(tok, m.name)) { mods |= m.bit; found = true; break; }
    if (found) continue;
    for (auto& k : KEY_NAMES) if (strieq(tok, k.name)) { if (usage) return false; usage = k.usage; found = true; break; }
    if (found) continue;
    if (strlen(tok) == 1) {  // 组合键里的字母不区分大小写、不隐含 Shift：GUI+V 就是 ⌘V（0.2.3 及之前会发成 ⌘⇧V）；要 Shift 请显式写 SHIFT+
      char c = tok[0]; if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
      uint8_t m; if (!asciiToUsage(c, m, usage)) return false; mods |= m; continue;
    }
    return false;
  }
  return true;
}

static void keysAdd(uint8_t u) {
  if (!u) return;
  for (int i = 0; i < 6; i++) if (g_keys[i] == u) return;
  for (int i = 0; i < 6; i++) if (!g_keys[i]) { g_keys[i] = u; return; }
}
static void keysRemove(uint8_t u) {
  if (!u) return;
  for (int i = 0; i < 6; i++) if (g_keys[i] == u) g_keys[i] = 0;
}
static void releaseAll() {
  g_mods = 0; memset(g_keys, 0, 6); g_buttons = 0;
  if (hidReady()) { hidKeyboard(); hidMouse(0, 0, 0); }
}

// 大位移拆成 ±127 的报告
static void moveRel(long dx, long dy) {
  while (dx || dy) {
    int sx = dx > 127 ? 127 : (dx < -127 ? -127 : (int)dx);
    int sy = dy > 127 ? 127 : (dy < -127 ? -127 : (int)dy);
    hidMouse((int8_t)sx, (int8_t)sy, 0);
    dx -= sx; dy -= sy;
    if (dx || dy) delay(cfg.mvdelay);
  }
}

static uint8_t buttonBit(const char* s) {
  if (!s) return 1;
  switch (tolower(s[0])) { case 'l': return 1; case 'r': return 2; case 'm': return 4; default: return 0; }
}

// 把 "\n" "\t" "\\" 转义还原（原地）
static void unescape(char* s) {
  char* w = s;
  for (char* r = s; *r; r++) {
    if (*r == '\\' && r[1]) {
      r++;
      *w++ = (*r == 'n') ? '\n' : (*r == 't') ? '\t' : *r;
    } else *w++ = *r;
  }
  *w = 0;
}

// ------------------------------------------------------------------ 命令处理
static char lineBuf[300];
static size_t lineLen = 0;

static void reply(const char* s) {
  if (g_sink == SINK_BLE) bleReply(s); else CMD_SERIAL.println(s);
}
static void replyf(const char* fmt, ...) {
  char b[300];   // ST 一行现在接近 200 字节，别截断（BLE 回复按 MTU 分片，串口无所谓）
  va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
  reply(b);
}

static void handleLine(char* line) {
  // 去掉行尾空白
  size_t n = strlen(line);
  while (n && (line[n - 1] == '\r' || line[n - 1] == ' ')) line[--n] = 0;
  if (!n) { reply("ERR empty"); return; }
  // TX 需要整行剩余部分，先单独处理
  if ((line[0] == 'T' || line[0] == 't') && (line[1] == 'X' || line[1] == 'x') && (line[2] == ' ' || line[2] == 0)) {
    hidWake();
    if (!hidReady()) { reply("ERR not connected"); return; }
    char* text = line + (line[2] ? 3 : 2);
    unescape(text);
    int count = 0;
    for (char* p = text; *p; p++) {
      uint8_t m, u;
      if ((uint8_t)*p > 126 || !asciiToUsage(*p, m, u)) { releaseAll(); replyf("ERR non-ascii at %d", count); return; }
      g_mods = m; memset(g_keys, 0, 6); g_keys[0] = u; hidKeyboard(); delay(cfg.typedelay);
      g_mods = 0; g_keys[0] = 0; hidKeyboard(); delay(cfg.typedelay);
      count++;
    }
    replyf("OK tx %d", count);
    return;
  }
  char* save = nullptr;
  char* cmd = strtok_r(line, " ", &save);
  char* a1 = strtok_r(nullptr, " ", &save);
  char* a2 = strtok_r(nullptr, " ", &save);
  char* a3 = strtok_r(nullptr, " ", &save);
  if (strieq(cmd, "PING")) { reply("OK PONG"); return; }
  if (strieq(cmd, "ST")) {
    unsigned long idleS = (millis() - g_lastActMs) / 1000UL;
#if HID_BACKEND == 1
    int conns = g_server ? g_server->getConnectedCount() : 0; const char* advm = g_advMode;
#else
    int conns = g_connected ? 1 : 0; const char* advm = "-";
#endif
#if HID_BACKEND == 1
    if (g_sink == SINK_BLE) {
      replyf("OK conn=%d hid=%s hididle=%u idle=%lu conns=%d adv=%s backend=%s name=%s mvdelay=%u typedelay=%u txfail=%lu fw=%s via=ble hidpeer=%s blew=%lu bler=%lu blet=%lu", hidReady() ? 1 : 0, hidStateStr(), cfg.hididle, idleS, conns, advm, BACKEND_NAME, cfg.name, cfg.mvdelay, cfg.typedelay, (unsigned long)g_txfail, FW_VERSION, cfg.hidpeer[0] ? cfg.hidpeer : "-", (unsigned long)g_bleWrites, (unsigned long)g_bleRxLines, (unsigned long)g_bleTxReplies);
      return;
    }
#endif
    replyf("OK conn=%d hid=%s hididle=%u idle=%lu conns=%d adv=%s backend=%s name=%s mvdelay=%u typedelay=%u txfail=%lu fw=%s via=serial hidpeer=%s token=%s", hidReady() ? 1 : 0, hidStateStr(), cfg.hididle, idleS, conns, advm, BACKEND_NAME, cfg.name, cfg.mvdelay, cfg.typedelay, (unsigned long)g_txfail, FW_VERSION, cfg.hidpeer[0] ? cfg.hidpeer : "-", cfg.token);
    return;
  }
  if (strieq(cmd, "WAKE")) {    // 让板子向 iPhone 广播（iPhone 按绑定自动重连），并重置空闲计时；conn=1 表示已经连着
    hidWake(); replyf("OK wake conn=%d hid=%s", hidReady() ? 1 : 0, hidStateStr()); return;
  }
  if (strieq(cmd, "RESTART")) { reply("OK restart"); CMD_SERIAL.flush(); delay(50); esp_restart(); }   // 硬复位（等于断电重启，绑定/配置都在）
  if (strieq(cmd, "SLEEP")) {   // 立刻断开 iPhone 的 HID 连接并停止广播（屏幕键盘马上恢复）；hididle=0 时无效
    if (hidAlwaysOn()) { reply("ERR hididle=0 or pairing mode (appearance!=0)"); return; }
    hidGoIdle(); reply("OK sleep"); return;
  }
  if (strieq(cmd, "CFG")) {
    if (!a1 || !a2) { reply("ERR bad args"); return; }
    if (strieq(a1, "mvdelay")) cfg.mvdelay = atoi(a2);
    else if (strieq(a1, "typedelay")) cfg.typedelay = atoi(a2);
    else if (strieq(a1, "appearance")) cfg.appearance = (uint16_t)strtol(a2, nullptr, 0);
    else if (strieq(a1, "hididle")) cfg.hididle = (uint16_t)atoi(a2);
    else if (strieq(a1, "name")) { strncpy(cfg.name, a2, sizeof(cfg.name) - 1); cfg.name[sizeof(cfg.name) - 1] = 0; }
    else if (strieq(a1, "token")) { if (g_sink != SINK_SERIAL) { reply("ERR token only via serial"); return; } strncpy(cfg.token, a2, sizeof(cfg.token) - 1); cfg.token[sizeof(cfg.token) - 1] = 0; }
    else if (strieq(a1, "hidpeer")) { if (strieq(a2, "clear")) cfg.hidpeer[0] = 0; else { strncpy(cfg.hidpeer, a2, sizeof(cfg.hidpeer) - 1); cfg.hidpeer[sizeof(cfg.hidpeer) - 1] = 0; } }
#if HID_BACKEND == 1
    else if (strieq(a1, "unbond")) {   // CFG unbond others：删掉除 hidpeer(iPhone) 之外的绑定（比如 Mac 自动配对留下的）；CFG unbond all：全删
      int n = NimBLEDevice::getNumBonds(), removed = 0;
      for (int i = n - 1; i >= 0; i--) {
        NimBLEAddress a = NimBLEDevice::getBondedAddress(i);
        if (strieq(a2, "all") || strcasecmp(a.toString().c_str(), cfg.hidpeer) != 0) { NimBLEDevice::deleteBond(a); removed++; }
      }
      replyf("OK cfg unbond removed=%d left=%d", removed, NimBLEDevice::getNumBonds()); return;
    }
#endif
    else { replyf("ERR unknown cfg %s", a1); return; }
    saveConfig();
    if (strieq(a1, "hididle")) { if (hidAlwaysOn()) hidWake(); else g_evt_adv = true; }   // 改成 0 立刻恢复常连；否则按新时限计
    replyf("OK cfg %s%s", a1, (strieq(a1, "name") || strieq(a1, "appearance")) ? " (reboot to apply)" : "");
    return;
  }
  if (strieq(cmd, "RL")) { releaseAll(); reply("OK rl"); return; }
  // 往下都是需要 iPhone 连着的命令：算一次活动（重置空闲计时），空闲中就先唤醒（开始向 iPhone 广播）。
  // iPhone 连回来要 1~3 s，这期间回 ERR not connected，Mac 端守护进程会等 conn=1 再重发。
  hidWake();
  if (!hidReady()) { reply("ERR not connected"); return; }

  if (strieq(cmd, "MV")) {
    if (!a1 || !a2) { reply("ERR bad args"); return; }
    long dx = atol(a1), dy = atol(a2);
    moveRel(dx, dy);
    replyf("OK mv %ld %ld", dx, dy);
    return;
  }
  if (strieq(cmd, "BT")) {
    uint8_t b = buttonBit(a1);
    if (!b || !a2) { reply("ERR bad args"); return; }
    if (strieq(a2, "down")) g_buttons |= b; else if (strieq(a2, "up")) g_buttons &= ~b; else { reply("ERR bad args"); return; }
    hidMouse(0, 0, 0); reply("OK bt"); return;
  }
  if (strieq(cmd, "CK")) {
    uint8_t b = a1 ? buttonBit(a1) : 1;
    int times = a2 ? atoi(a2) : 1;
    if (!b || times < 1) { reply("ERR bad args"); return; }
    for (int i = 0; i < times; i++) {
      g_buttons |= b; hidMouse(0, 0, 0); delay(30);
      g_buttons &= ~b; hidMouse(0, 0, 0); if (i + 1 < times) delay(60);
    }
    replyf("OK ck %d", times); return;
  }
  if (strieq(cmd, "LP")) {
    if (!a1) { reply("ERR bad args"); return; }
    long ms = atol(a1); uint8_t b = a2 ? buttonBit(a2) : 1;
    if (!b) { reply("ERR bad args"); return; }
    g_buttons |= b; hidMouse(0, 0, 0); delay(ms);
    g_buttons &= ~b; hidMouse(0, 0, 0);
    replyf("OK lp %ld", ms); return;
  }
  if (strieq(cmd, "WH")) {
    if (!a1) { reply("ERR bad args"); return; }
    int w = atoi(a1); int step = w > 0 ? 1 : -1;
    for (int i = 0; i < abs(w); i++) { hidMouse(0, 0, (int8_t)step); delay(cfg.mvdelay); }
    replyf("OK wh %d", w); return;
  }
  if (strieq(cmd, "KP") || strieq(cmd, "KD") || strieq(cmd, "KU")) {
    if (!a1) { reply("ERR bad args"); return; }
    uint8_t mods, usage;
    if (!parseKeySpec(a1, mods, usage)) { reply("ERR bad key"); return; }
    bool down = !strieq(cmd, "KU"), up = !strieq(cmd, "KD");
    int times = (strieq(cmd, "KP") && a2) ? atoi(a2) : 1;
    if (times < 1) times = 1;
    for (int i = 0; i < times; i++) {
      if (down) { g_mods |= mods; keysAdd(usage); hidKeyboard(); }
      if (down && up) delay(cfg.typedelay);
      if (up) { g_mods &= ~mods; keysRemove(usage); hidKeyboard(); }
      if (i + 1 < times) delay(cfg.typedelay);
    }
    replyf("OK %s %d", strieq(cmd, "KP") ? "kp" : strieq(cmd, "KD") ? "kd" : "ku", times); return;
  }
  if (strieq(cmd, "CC")) {
    if (!a1) { reply("ERR bad args"); return; }
    for (auto& c : CONSUMER_NAMES) if (strieq(a1, c.name)) { hidConsumer(c.usage); reply("OK cc"); return; }
    uint16_t u = (uint16_t)strtol(a1, nullptr, 0);
    if (u) { hidConsumer(u); reply("OK cc"); return; }
    reply("ERR unknown consumer key"); return;
  }
  replyf("ERR unknown cmd %s", cmd);
}

// ------------------------------------------------------------------ 主流程
void setup() {
  CMD_SERIAL.begin(BAUD);
  loadConfig();
  hidBegin();
  delay(300);
  CMD_SERIAL.printf("# hid_bridge %s backend=%s name=%s token=%s\n", FW_VERSION, BACKEND_NAME, cfg.name, cfg.token);
}

#if HID_BACKEND == 1
static void handleBleLine(uint16_t conn, char* line) {
  g_sink = SINK_BLE; g_sinkConn = conn;
  // 先处理 AUTH：口令对了这条连接才算指令连接
  size_t n = strlen(line);
  while (n && (line[n - 1] == '\r' || line[n - 1] == ' ')) line[--n] = 0;
  if ((line[0] == 'A' || line[0] == 'a') && strncasecmp(line, "AUTH ", 5) == 0) {
    if (strcmp(line + 5, cfg.token) == 0) { g_cmdConn = conn; reply("OK auth"); }
    else { delay(300); reply("ERR auth"); }
  } else if (conn != g_cmdConn) {
    reply("ERR auth required");
  } else {
    handleLine(line);
  }
  g_sink = SINK_SERIAL; g_sinkConn = BLE_HS_CONN_HANDLE_NONE;
}
#endif

void loop() {
  if (g_evt_conn) { g_evt_conn = false; CMD_SERIAL.println("# connected"); }
  if (g_evt_disc) { g_evt_disc = false; CMD_SERIAL.println("# disconnected"); }
  if (g_evt_idle) { g_evt_idle = false; CMD_SERIAL.println("# hid idle"); }
  if (g_evt_wake) { g_evt_wake = false; CMD_SERIAL.println("# hid wake"); }
#if HID_BACKEND == 1
  LogMsg lm;
  while (g_logQueue && xQueueReceive(g_logQueue, &lm, 0) == pdTRUE) CMD_SERIAL.println(lm.s);
  BleMsg m;
  while (g_bleQueue && xQueueReceive(g_bleQueue, &m, 0) == pdTRUE) handleBleLine(m.conn, m.line);
  // 空闲计时：醒着且超过 hididle 秒没活动 → 断开 iPhone、停止向它广播
  if (!hidAlwaysOn() && g_hidAwake && (uint32_t)(millis() - g_lastActMs) > (uint32_t)cfg.hididle * 1000UL) hidGoIdle();
  if (g_evt_adv) { g_evt_adv = false; updateAdvertising(); }
#endif
  while (CMD_SERIAL.available()) {
    char c = (char)CMD_SERIAL.read();
    if (c == '\n') {
      lineBuf[lineLen] = 0;
      handleLine(lineBuf);
      lineLen = 0;
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0; reply("ERR line too long");
    }
  }
  delay(1);
}
