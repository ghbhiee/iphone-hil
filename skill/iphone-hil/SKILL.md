---
name: iphone-hil
description: 看用户 iPhone 的屏幕并替用户操作它：通过 USB 取 iPhone 画面、经 ESP32 蓝牙 HID 点击/输入。当用户说「看一下我手机」「打开微信/某个 App 帮我查…」「读一下手机上的报告」「在手机上点…」「用 iphone-hil」「取帧」时使用。工具在本项目目录，服务按需拉起、空闲 10 分钟自动退出。
---

# iphone-hil：看 iPhone 屏幕、替用户操作

项目：你克隆 iphone-hil 的目录，下面用 `~/iphone-hil` 代指（装到别处就把路径改掉；细节看它的 README.md，坑看 HANDOFF.md）。所有命令都在项目目录下用 `.venv/bin/python -m hil …` 执行。

## 开始前确认（缺一不可）
- iPhone 用数据线接在 Mac 上、已解锁、自动锁定为「永不」。锁屏会让画面消失。
- ESP32（HIL-Bridge）通着电；iPhone 的辅助触控已开、已和它配对；**iPhone 自动锁定设为「永不」**（锁屏会让画面消失）。
- **连接是「软件开侦听 + 用户手工点」**：不用这个工具时，板子处于正常模式、空闲 2 分钟会主动踢开 iPhone，让手机屏幕键盘恢复（用户能正常用手机）。要用工具时，`hil up` 会把板子切到「侦听模式」（以蓝牙键盘身份广播），然后**由用户在 iPhone 设置→蓝牙点一下 HIL-Bridge 手工连上**（iOS 不会自动重连，这是正常流程、不是故障）。连上后在整个使用期间板子不会再踢它。
- 用户没有在同时操作手机（会和截图对不上）。

## 开始工作
```bash
cd ~/iphone-hil && .venv/bin/python -m hil up
```
它会拉起两个服务（取帧 HIL Capture.app、链路 HIL Link.app）；如果 iPhone 没连，会**把板子切到侦听模式并提示用户去 iPhone 设置→蓝牙点一下 HIL-Bridge**，最多等 60 s。最后报告：画面分辨率、固件版本、iPhone HID 是否已连。第一次用时系统会弹相机/蓝牙授权框，让用户点允许。
两个服务空闲 `idle_quit_min`（默认 10）分钟后自动退出，退出时会把板子切回正常模式（iPhone 松开、屏幕键盘恢复）。做完想立刻停：`python -m hil stop`（同样会松开 iPhone、让键盘恢复）。

**开工前必须确认 iPhone HID 已连（这一步不能跳）**：看 `hil up` 最后一行的「iPhone HID」。
- 显示「已连」→ 直接进工作循环。
- 显示「未连」→ `hil up` 已经把板子切到侦听模式了，**原话告诉用户：请解锁 iPhone、到「设置 → 蓝牙」点一下 HIL-Bridge**（iOS 不会自动重连，必须手工点一下，这是正常流程）；用户点完会自动连上，或再 `python -m hil up` 一次确认「已连」。连上之前不要发 tap/type 等任何操作命令，也不要自己换方式去连。

## 工作循环（每步一条命令）
```bash
python -m hil grab                 # 截图到 captures/frame.png（默认缩放 0.5），用 Read 工具看图
python -m hil tap X Y              # 坐标按缩放后的图；指针自动逼近后点击，误差 2–5 px
python -m hil long X Y 800         # 长按
python -m hil drag X0 Y0 X1 Y1     # 拖动（惯性大，适合回顶部；精细翻页用 wheel）
python -m hil wheel 6              # 滚轮往下翻约 8 行；负方向目前无效，往上用 drag
python -m hil type "ascii"         # 只能 ASCII，先确认 iPhone 当前是英文键盘（中文拼音会把数字当候选）
python -m hil paste "中文也行"      # 经通用剪贴板粘贴，默认复制后等 5 s；粘贴后截图确认
python -m hil key GUI+H            # 组合键；GUI+SPACE 打开 Spotlight（偶尔第一下不响应，截图确认）
python -m hil home                 # 回主屏
python -m hil status               # 两个服务 + 固件状态
```
节奏：tap 之后等 1.5 s 再 grab；网页类页面加载等 3–4 s。滚轮事件落在指针下方的元素上，列表不滚就先 `goto X Y` 把指针挪到列表中间。

## 已知限制
- 中文等非 ASCII 用 `python -m hil paste "文本"`：Mac 复制 → 通用剪贴板 → iPhone 上 ⌘V，两端都要开「接力」。复制后默认等 5 s 再按 ⌘V（等 2 s 常落空）；粘贴后**必须截图确认**，没出来就隔 3 s `python -m hil key GUI+v` 再发一次（别盲发，成功时会粘两遍）；若截图发现回到了主屏，先别重发，再打开那个输入框看内容多半已经在了。能走列表、索引栏、菜单就别输入。
- 截图里的状态栏是 iOS 伪造的（固定 9:41、满电），别信。
- 手机被拿起会横竖屏切换，帧尺寸随之变，上一帧的坐标作废，必须重新 grab。
- 点开新页面后指针可能正好停在某个控件上，连续点击前先看截图。
- 板子连着时 iPhone 的屏幕键盘弹不出来（iOS 把它当硬件键盘）。**做完任务一定要 `python -m hil stop` 或 `python -m hil normal`**，板子切回正常模式、松开手机、屏幕键盘恢复；否则在侦听模式下它会一直连着、键盘一直不出来。空闲 10 分钟服务自动退出时也会切回正常模式。下次要用再 `hil up`，按提示让用户去设置→蓝牙点一下 HIL-Bridge 连上。
- **为什么要手工连**：iOS 的蓝牙 HID 是「手机决定连谁」，板子（外设）没法强迫手机来连；板子被踢开后 iOS 不会自动重连、软件也叫不回来。所以流程设计成：板子以键盘身份广播（侦听模式）→ 用户手工点一下连上。详见 `docs/IOS_RECONNECT_HANDOFF.md`。

## 安全
- 不输入证件号、密码、支付信息；页面要这些时停下来问用户。
- 不发送消息、不付款、不改设置，除非用户明确要求这一步。
- 截图含用户个人数据，任务结束删掉 `captures/*.png`。
