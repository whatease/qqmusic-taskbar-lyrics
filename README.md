# QQ音乐底栏歌词

在 **Windows 任务栏**"最后一个图标 ↔ 系统托盘"的空档处,实时显示 QQ音乐正在播放的歌曲歌词,卡拉OK式逐字推进:

- **已播放 = 粉色**,**未播放 = 橘色**(柔边过渡)
- 位置随任务栏 **图标数量变化自动调整**,绝不挤占任何图标;鼠标完全穿透
- 深浅色任务栏都清晰(暗色描边 + 柔和辉光),换行交叉淡入淡出
- 长歌词先自动缩字号,仍放不下则跟随光标平滑滚动
- 全屏应用/游戏时自动隐藏;暂停时定格当前进度
- 歌词比歌声**提前约0.5秒**出现,方便跟唱(`lyric_lead_sec` 可调)
- 歌词与歌曲来源:系统 SMTC(媒体传输控制)+ QQ音乐公开接口(自动搜索匹配,本地缓存)
- **MiMo 余额挂件**:任务栏右侧(紧靠托盘)常显小米 MiMo 账户余额、今日 Token 与消费(`mimo_widget` 开关)
- 托盘图标:右键菜单(显示/隐藏、开机自启、登录 MiMo、退出);`config.json` 支持热修改

## 快速开始

```bat
:: 1. 首次:安装依赖(已装可跳过)
安装依赖.bat

:: 2. 启动(打开QQ音乐播放歌曲即自动显示)
启动QQ音乐底栏歌词.bat
```

确认 QQ音乐"桌面歌词/媒体信息"能被系统识别即可(新版QQ音乐默认支持 SMTC)。

## 文件结构

| 文件 | 说明 |
|---|---|
| `taskbar_lyrics.py` | 主程序(窗口/托盘/自启/渲染编排) |
| `media_watcher.py` | SMTC 媒体监听(WinRT,备用 PowerShell 后端) |
| `lyrics_client.py` | QQ音乐搜索 + LRC 下载 + 解析 + 缓存 |
| `taskbar_geometry.py` | UIA 遍历任务栏,计算图标与托盘间的空档 |
| `karaoke_renderer.py` | Pillow 卡拉OK渲染(双色/辉光/滚动) |
| `layered_window.py` | 分层窗口公共设施(UpdateLayeredWindow) |
| `mimo_client.py` | MiMo 控制台取数(纯HTTP + SSO 续期 + 一次性登录) |
| `mimo_widget.py` | MiMo 余额/今日用量挂件渲染 |
| `app_config.py` | 配置加载/热重载 |
| `config.json` | 首次运行自动生成,可手动修改(保存即生效) |
| `tools/diagnose.py` | 诊断:环境/几何/媒体/歌词 一键排查 |

## 配置项(config.json,保存即热生效)

| 键 | 默认 | 说明 |
|---|---|---|
| `played_color` | `#FF7EB3` | 已播放颜色(粉) |
| `unplayed_color` | `#FFA347` | 未播放颜色(橘) |
| `title_color` | `#FFC7E0` | 无歌词时标题颜色 |
| `font_family` | 雅黑系列 | 依次尝试的字体 |
| `font_scale` / `min_font_scale` | 0.60 / 0.42 | 字号=任务栏高×系数 / 缩放底线 |
| `glow_strength` / `outline_width` | 0.85 / 1.3 | 辉光与描边强度 |
| `margin_x` / `soft_edge_px` | 12 / 6 | 与图标留白 / 粉橘柔边宽度 |
| `min_gap_width` | 150 | 可用空隙最小宽度(逻辑像素) |
| `show_when_paused` | true | 暂停时是否定格显示 |
| `media_grace_sec` | 8.0 | 媒体会话瞬时中断时保留画面秒数(防系统忙时闪隐) |
| `lyric_lead_sec` | 0.5 | 歌词比歌声提前出现的秒数(0=同步;跟唱建议0.3~0.8,保存即生效) |
| `hide_in_fullscreen` | true | 全屏时隐藏 |
| `allow_other_media` | true | 无QQ音乐会话时是否用其他播放器 |
| `mimo_widget` | true | 显示 MiMo 余额挂件(首次使用需托盘登录一次) |
| `mimo_width` | 160 | 挂件宽度(逻辑像素;空隙不足自动收窄) |
| `mimo_refresh_sec` | 180 | 余额/用量刷新间隔(秒) |

## 常见问题

- **没有显示**:任务栏太满(空隙 < `min_gap_width`)会自动隐藏;运行 `python tools/diagnose.py` 查看空隙与媒体会话状态。
- **歌词不匹配**:程序按"歌名+歌手"搜索QQ音乐接口,冷门歌曲可能匹配失败(会显示标题模式),缓存 10 分钟后自动重试。
- **挂件显示"MiMo 未登录"**:右键托盘 → "登录 MiMo 账号",在弹出的浏览器里登录一次即可;之后平台会话每 24 小时自动续期,账号会话失效才需要重新登录。
- **想退出**:右键托盘音符图标 → 退出。
- **演示模式**:`python taskbar_lyrics.py --demo 晴天 周杰伦`

## 卸载

托盘菜单关闭"开机自启"并退出即可;缓存目录 `lyrics_cache/` 可直接删除。

## 分享与开源

- **发给同学**:双击 `打包分享.bat`,在上一级目录生成 `QQ音乐底栏歌词-分享版.zip`(不含日志/缓存/个人配置),对方解压后先跑 `安装依赖.bat` 再启动。
- **开源托管**:本目录已是独立 git 仓库,可直接推送到 GitHub / Gitee:
  ```bash
  git remote add origin <你的仓库地址>
  git push -u origin main
  ```
- 许可协议:[MIT](LICENSE)。本项目与腾讯公司无关,仅供学习交流,请勿用于商业用途。

