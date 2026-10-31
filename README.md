# ReTato

Windows 上的 Tatotalk 独立消息采集器。使用单独登录的会话接收云消息，写入本机加密队列，再推送到兼容 TG-forwarder 消息接入 v1 的 HTTP 服务。

无需打开对应聊天，也不依赖原客户端窗口。项目不包含账号、会话、接入 Token、真实消息、测试运行记录或原客户端安装文件。

## 安装与配置

开发验证环境为 Windows、Python 3.14。所有命令在项目根目录执行。

```powershell
python -m pip install --target .tools/probe -r requirements-probe.txt
Copy-Item config/client-profile.example.json config/client-profile.local.json
```

编辑 `config/client-profile.local.json`，填入自己有权使用、与目标服务兼容的应用参数。示例中的 `api_id`、`api_hash` 和 `app_version` 是占位值，不能直接登录。本项目不提供应用凭据。

1. 双击 `run-login.cmd`，在本机输入邮箱，手动完成图形校验，再输入验证码。
2. 指定自己的 HTTP 地址并配置消息接入来源的 `tgfi_` Token：

```powershell
.\configure-http.cmd --url "https://example.invalid/api/ingest/v1/messages"
```

将示例域名替换为实际地址。Token 在本机隐藏输入，由当前 Windows 用户的 DPAPI 加密保存。不要把 Token 写进源码或提交到仓库。

3. 双击 `run-collector.cmd` 持续采集。窗口可以最小化。
4. 使用 `show-status.cmd` 查看状态；按 Ctrl+C 或运行 `stop-collector.cmd` 停止。程序限制单实例运行。

## 按群组定向采集

双击 `configure-groups.cmd`，使用现有登录会话读取当前加入的群与频道。窗口支持按名称或 ID 搜索，勾选多个普通群、超级群或频道后保存。相同名称的群可通过类型和 ID 区分；搜索不会清空已勾选项。目录中暂不可见的已选项仍保留，可在窗口中明确取消。

名单保存在本机 `config/capture-filter.local.json`，只包含会话类型和 ID，不保存群名称或凭据。配置界面不会启动采集或推送消息；目录获取失败及取消不会覆盖原名单。需要重新全量采集时，在窗口显式关闭筛选。

- 启用筛选后，仅选中的群／频道消息会进入业务事件及 HTTP 队列，私聊不会进入名单。
- 启用但没有选中任何会话时，新增消息全部排除；配置损坏时停止匹配并在状态中报告，不回退全量。
- 名单按稳定 ID 匹配，群改名不影响选择。群迁移为超级群会改变身份，需要重新选择。
- 运行中自动读取新名单。此前未选会话的待推送消息暂留队列，重新选中才继续发送；已开始的单次 HTTP 请求无法撤回。
- 修改名单不回填此前被过滤的旧消息。`show-status.cmd` 中 `capture_filter` 显示模式、选中数和配置错误，`filter_held` 表示暂留的旧待发送消息。

全局和频道同步仍维护所有更新的进度；用于协议恢复的加密原始更新可能包含未选会话。本功能控制业务事件入队与 HTTP 投递，不改变服务端的全局同步机制。

## 接收与投递

- 接收私聊、普通群、超级群和频道更新，包括服务端同步给独立会话的已发送消息。首次启动建立当前进度，默认不批量推送旧历史。
- 全局及频道分别保存同步进度；支持自动重连、差量补拉和进程重启后的队列恢复。
- 原始更新、事件、载荷和同步进度均加密保存。事件与进度在同一 SQLite 事务中提交，未知结构保留原包等待适配。
- 稳定的会话/消息 ID 用于去重；首次入队冻结完整载荷，重试复用相同内容。
- 超时、429、5xx 退避重试；401/403 暂停推送，更新配置后恢复；不可重试的拒绝保留供检查。
- 禁止自动跟随 HTTP 跳转，保持 HTTPS 证书验证。202 或有效的重复200回执表示服务已接收入队，不代表下游发布完成。

目标 v1 接口只接受文字 JSON：推送正文或媒体说明文字。纯媒体和系统事件保留在本机，不自动下载或上传附件。超级群使用稳定的 `channel` 身份命名空间。

## 当前限制

部分自定义媒体、网页即时预览 `Page` 和私密聊天加密动作尚未完全适配。服务端返回 `DifferenceTooLong` 时保留原始数据和未解决缺口，暂不提供完整历史重建。不能保证所有特殊消息结构均已覆盖。

图形校验必须人工完成；网页式验证码及两步验证密码尚未适配。会话和队列受 Windows 当前用户 DPAPI 保护，不能直接搬到另一个用户使用。

## 本机文件

| 相对位置 | 用途 | 是否提交 |
| --- | --- | --- |
| `config/*.example.json` | 无凭据配置模板 | 是 |
| `config/*.local.json`、`config/*.dpapi` | 本机配置和加密 Token | 否 |
| `sessions/` | 此采集器自己的登录会话及恢复检查点 | 否 |
| `.data/` | 加密消息队列、状态和运行记录 | 否 |
| `.tools/` | 本机安装的依赖 | 否 |
| `research/*-schema.tl` | 运行所需协议声明 | 是 |
| `research/android-static/embedded-public-0.pem` | 固定服务端 RSA **公钥** | 是 |

不要删除 `.data/` 来排查投递问题，否则会丢失本地恢复依据。重新配置 HTTP 时保留原队列。登录中断后默认恢复本机加密检查点；只有确实放弃该尝试时才运行 `run-login.cmd --restart-login`。

## 验证与协议

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) '.tools\probe'
$env:PYTHONUTF8 = '1'
python -m unittest discover -s tests -v
```

测试代码仅使用合成账号、合成消息和临时文件；网络异常测试使用本机受控服务，不把合成消息发送到外部接入服务。Windows 回环、asyncio 和 DPAPI 需要正常本机权限。

底层使用固定版本 Telethon 的 MTProto sender，业务 RPC 使用已恢复的 Tatotalk layer 123，未实例化 TelegramClient。协议范围见 [docs/protocol.md](docs/protocol.md)。

接口参考：[TG-forwarder 消息接入 API](https://github.com/Akiyy-dev/TG-forwarder/blob/main/docs/api.md#4-%E6%B6%88%E6%81%AF%E6%8E%A5%E5%85%A5-api)。
