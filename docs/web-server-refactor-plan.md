# web_server.py 瘦身与文件下发域抽取计划

> 2026-09-07 调研成文。行号以当日快照（1254 行）为准，执行时以符号名为准。
> 沿用 [warserver-refactor-plan.md](warserver-refactor-plan.md) 的重构原则：纯移动不改行为、不留兼容层、小步提交、行为修正独立 commit。
> 背景：原计划阶段 4 曾判定 web_server"维持现状（当时 1121 行，风险小收益也小）"；此后新增 hngsync 清单 CAS 命名、`.sgn` 动态签名（2026-09-06）、`/api/import` 外服迁移（2026-09-06），现 1254 行且静态下发域复杂度大涨——该判定的前提已失效，本计划重启此文件的拆分（仅此文件，不恢复原阶段 4 的 wardb / player_service 两项）。

## 执行记录

| 阶段 | 内容 | commit | 阶段回归 |
| --- | --- | --- | --- |
| 0 | 基线固化（快照测试 + 引用清点 + 响应头存档） | （本次提交） | 14 subjects + 5 断言 PASS |
| 1 | file_server.py 纯移动抽取（~520 行） | | |
| 2 | 等价去重（路由归一化 / 下发样板 / manifest 表驱动 / 校验去重） | | |
| 3a | 修复 `_localization_body` NameError（行为修正） | | |
| 3b | 补发 3 处 Content-Length（行为修正） | | |
| 3c | LOCALIZATION_EXTRA 结构化（等价重构，diff 大独立提交） | | |

## 〇、下载服务要不要单独抽取：结论

**抽。新建 `server/file_server.py` 承载静态文件下发域（约 520 行，占 42%），`web_server.py` 保留 HTTP 入口 + Web API + SPA（约 600 行，阶段 2 去重后 ~520）。**

依据：

1. **协议域自洽**：CAS 命名（sha256 前缀校验）、bz2 变体缓存、`.sgn` Authenticode 动态签名、三类清单 wire 格式，全部只服务于"文件下发"一件事，逆向结论集中在 hng-client-file-integrity.md；与注册/验证码/在线统计无任何逻辑交叠。
2. **两个消费者共享一条回源链**：hngsync.exe（manifest / CAS / .sgn）与 swf/Flash（`/WarDatabase/<tbl>` 表、flashres 军衔图标、`/HnG_Strategy_*.xml` 本地化）最终都走 `_cas_source_body` 的五级回源（cas/ 目录 → 内存清单 → wardb 表 → texts → flashres）。**按消费者拆会把这条链劈成两半互相 import；按数据流拆（一切以文件名/CAS 名下发的静态内容归 file_server）边界唯一干净。**
3. **可测试性**：清单生成（texts 扫描哈希、flashres 扫描、CAS 名推导、UNIQUE_FILE_XML 组装）目前 import 即执行且零测试覆盖；抽出后补字节级快照测试，改 wardb 表 / 加 cas 文件时有回归防线。
4. **反方观点与回应**：拆后 web_server 仍有 ~600 行（其中约 115 行是 LOCALIZATION_EXTRA 文本常量），行数收益中等；且 HttpHandler 路由 dispatch 需要跨模块单入口。但本文件的跨域混杂（hngsync 逆向协议域 vs 公网 Web API 域）远比原计划判定"不拆"的 economy/social 等单域文件严重，拆分收益成立。

## 一、现状诊断（按域）

| 域 | 符号 | 约行数 | 归属 |
| --- | --- | --- | --- |
| CAS 压缩缓存 | `CAS_DIR` `_CAS_BZ2_CACHE` `_cas_bz2_body` | 15 | file_server |
| .sgn 动态签名 | `_CODESIGN_PFX` `_SGN_KEY` `_SGN_CACHE` `_sgn_body` | 43 | file_server |
| 本地化注入 | `LOCALIZATION_EXTRA` `_LOCALIZATION_CACHE` `SUPPORTED_LOCALES` `_localization_body` | 161 | file_server |
| settings.js | `SETTINGS_JS`（含 `UNIQUE_VERSION_FILENAME` 回填 L507-509） | 30 | **web_server**（混合 warserver/irc/statserver 运行时地址，属客户端入口配置；仅 `UNIQUE_VERSION_FILENAME` 一项从 file_server import） |
| 清单生成 | `_NOP_CAS` `GCE64_UNIQUE(_BZ2)` `_generate_texts_sync` `_generate_playerdll_sync` `WARDB_SYNC_FULL(_BZ2)` `_generate_flashres_unique` `FLASHRES_*` `WARDB_UNIQUE_BZ2` `_cas_name` `MANIFEST_BODIES` `MANIFEST_CAS_MAP` `UNIQUE_FILE_XML(_BZ2)` `UNIQUE_VERSION_FILENAME` | 200 | file_server |
| CAS 回源 | `_cas_source_body` | 21 | file_server |
| 武器变体图标回退 | `_WEAPON_VARIANT_IMG_RE` `_serve_weapon_variant_fallback` | 33 | file_server |
| 下载路由（do_GET 内） | `/hng_unique_v173293(.bz2)` `/wardb_unique(.bz2)` `/wardb_sync(.bz2)` `/WarDatabase/<tbl>` `/flashres_unique(.bz2)` `/gce64_unique(.bz2)` CAS `/<sha>-<size>[_flag](.bz2)` `.sgn` `/undefined` `/HnG_Strategy_<lang>.xml` | 180 | file_server |
| SPA 下发 | `_WEB_DIST` `_serve_file` `/register` `/import` `/assets/` `/icons/` | 60 | web_server |
| Web API | `_handle_captcha_*` `_handle_register_api` `_handle_import_api` `_foreign_server_addr` `_json_reply` `_client_ip` | 230 | web_server |
| 在线统计 | `_online_snapshot` `_handle_online_api` | 50 | web_server |
| 静态防护/回退 | `_STATIC_DENY_EXTS` `_deny_static` `super().do_GET()` HEAD | 30 | web_server |
| HTTP 骨架 | `HttpHandler` `start_http` `init` `_MAIN_LOOP` webp MIME 注册 | 220 | web_server（webp 注册移 file_server 顶部，两域共用且 import 顺序不变） |

依赖方向（无环）：`web_server → file_server → {state, wardb}`。

**顺带发现的缺陷**（阶段 3 处理，独立 commit）：

- `_localization_body` L275-277：except 分支 `_LOCALIZATION_CACHE = body`，若 `open()` 本身失败 `body` 未绑定 → UnboundLocalError 打穿请求线程；
- `/settings.js`、`/undefined`、`/WarDatabase/<tbl>` 三处未下发 `Content-Length`——hngsync 的 Http_FileDownload 显式依赖该头（L1056/L1154 注释），当前靠 HTTP/1.0 短连接 close 定界侥幸工作。

**冗余清单**（阶段 2 等价去重）：

1. `//` 前缀双写约 20 处（`path == '/x' or path == '//x'` / `startswith` 双写）；且已漂移：`/undefined` 没写 `//` 变体、`/admin` 的 `== '/admin'` 被 `startswith` 完全覆盖属死条件；
2. bytes 下发五连（send_response → Content-Type → Content-Length → end_headers → write）重复约 13 处；
3. `/wardb_unique` `/wardb_sync` `/flashres_unique` `/gce64_unique` 四路由完全同构（~60 行）；
4. `_handle_register_api` 与 `_handle_import_api` 校验链逐行雷同约 40 行（Content-Length 上限 / JSON 解析 / 蜜罐 / 令牌 / 限速 / 用户名正则 / 保留前缀 / 密码长度 / DB ready），错误文案已开始漂移；
5. `do_POST` 内 `X-Requested-With` 检查重复 3 次；
6. 零碎：L989 `import time as _ping_t` 冗余（模块级已 import）；`_handle_online_api` 未复用 `_client_ip`；`/register` 与 `/import` 两个 HTML 路由结构重复。

## 二、模块划分

```
server/file_server.py        # 静态文件下发域（新，~460 行）
  # 生成（import 即执行，搬运时保持原序）
  _cas_bz2_body / CAS_DIR / _CAS_BZ2_CACHE          # bz2 变体缓存
  _sgn_body / _CODESIGN_PFX / _SGN_KEY / _SGN_CACHE # .sgn 动态签名
  _cas_name / MANIFEST_BODIES / MANIFEST_CAS_MAP /
    UNIQUE_FILE_XML(_BZ2) / UNIQUE_VERSION_FILENAME # hngsync 清单
  _generate_texts_sync / _generate_playerdll_sync /
    _generate_flashres_unique / GCE64_UNIQUE /
    WARDB_SYNC_FULL(_BZ2) / FLASHRES_*              # 子清单体
  _localization_body / LOCALIZATION_EXTRA / SUPPORTED_LOCALES
  # 服务
  resolve_cas(name)          # 原 _cas_source_body，唯一改名的跨模块符号
  try_serve(handler, path)   # do_GET 下载路由单入口（返回 True=已处理）
  send_bytes(handler, body, ctype, cache=...)        # 阶段 2 抽出，web_server 复用

server/web_server.py         # 保留（~520 行）
  init / start_http / HttpHandler                    # HTTP 骨架与 dispatch
  SETTINGS_JS（import file_server.UNIQUE_VERSION_FILENAME 回填）
  _serve_file + SPA 路由 /assets/ /icons/ /register /import
  _handle_captcha_* / _handle_register_api / _handle_import_api / _json_reply
  _online_snapshot / _handle_online_api
  _deny_static + 静态回退（super().do_GET，directory=GAME_DIR 不变）
```

- `try_serve(handler, path)` 是 file_server 对外的路由边界：web_server 的 `do_GET` 在 SPA/`/admin`/`/`/`/status`/`/api`/`/online.json`/`/settings.js` 判断之后、`_deny_static` 之前调用一次，命中即 return。路由判断逻辑整体随处理体搬运。
- `SUPPORTED_LOCALES` 归 file_server（语言文件域），web_server 组装 `SETTINGS_JS` 的 `locale_*` 族时 import 使用。
- 日志前缀 `[init]`/`[HTTP]` 保持原样，仅日志所在模块变化，不影响 grep 习惯。

## 三、分阶段计划

### 阶段 0：基线固化（2026-09-07 已完成）

- 引用清点（2026-09-07 已确认）：外部仅 `realtest_server.py` L319-320 使用 `init`/`start_http`；`tests/` 50 个文件零引用 web_server 内部符号；admin 后台经 HTTP 消费 `/online.json` 而非 import——拆分无调用方负担；
- 新增 `tests/test_file_server_snapshot.py`（pytest 兼容 + `--update` 生成基线，独立脚本模式，对齐 tests 惯例）：对模块级产物逐项 sha256——`SETTINGS_JS`、`UNIQUE_FILE_XML(_BZ2)`、`MANIFEST_CAS_MAP`（名→sha 排序元组）、`WARDB_SYNC_FULL(_BZ2)`、`FLASHRES_UNIQUE(_BZ2)`、`WARDB_UNIQUE_BZ2`、`GCE64_UNIQUE(_BZ2)`、`TEXTS_CAS_MAP`、`FLASHRES_CAS_MAP` 共 14 项，基线固化于 `tests/file_server_snapshot_baseline.json`；另有 4 项恒真结构断言（locale 族完整 / settings.js 指向主清单 CAS 名 / MANIFEST_CAS_MAP 命名自洽 / UNIQUE_VERSION_FILENAME 自洽）——直跑与 pytest 均 PASS；
- 响应头存档 `docs/http-headers-baseline-20260907.txt`（GET + HEAD 各 16-18 条路由的状态码/Content-Type/Content-Length/Cache-Control；tmp/ 被 gitignore 故入 docs）；
- **阶段 0 实测发现的两个基线事实**：
  1. **HEAD 与 GET 行为分叉**：`do_HEAD` 只有 `_deny_static` + 静态回退，全部动态路由（/settings.js、各 manifest、CAS、.sgn、本地化）对 HEAD 一律 404——阶段 2 的路径归一化只在 do_GET/do_POST 内做，**不得让 do_HEAD 复用动态路由**（否则 HEAD 从 404 变 200 属行为变化）；HEAD 对 GAME_DIR 静态文件（如 /WarDatabase/accesspoint）走 super() 返回 200 属既有行为；
  2. **GET 无 Content-Length 实为 4 处**（比初判多 /status 的 404 分支）：/settings.js、/undefined、/WarDatabase/<tbl>、/status(404)；其余 14 条动态路由均有 CL。/HnG_Strategy_en.xml 路由实测有 CL（L1137 已发）；
- git 仓库根 = `server/`，基线以**当前工作区状态**为准（web_server.py 及外服迁移相关文件存在未提交改动，属用户在途工作，不纳入本次提交）。

### 阶段 1：file_server.py 纯移动抽取（1 commit）

- 按第二节划分搬运，只搬不改；唯一符号改动 `_cas_source_body → resolve_cas`（跨模块调用改公开名，其余私有名原样）；
- `web_server.do_GET` 下载路由段（含 `/undefined`、`/HnG_Strategy_*.xml`、CAS/.sgn、`/WarDatabase/`、manifest 5 组、武器变体回退）替换为 `if file_server.try_serve(self, path): return`；
- `SETTINGS_JS` 组装留在 web_server，`UNIQUE_VERSION_FILENAME`/`SUPPORTED_LOCALES` 从 file_server import；
- import 即执行的生成逻辑保持原有顺序（state → wardb 符号 → texts 扫描 → flashres 扫描 → manifest 组装 → settings.js 回填）；
- 验收：快照测试 byte 一致；`.venv\Scripts\python.exe -c "import realtest_server"` 冒烟；重启 realtest_server 后客户端登录 hngsync 同步正常（downloadfiles.log 无 Checksum failure、无空同步）。

### 阶段 2：等价去重（1-2 commit）

1. `do_GET`/`do_POST` 入口路径归一化：`path = '/' + self.path.split('?')[0].lstrip('/')`（仅局部变量，`self.path` 原样传给静态回退），删除全部 `//` 双写；`/admin` 死条件 `== '/admin'` 一并清理；
2. `send_bytes(handler, body, ctype, cache=None)` 统一下发（file_server 定义导出），替换约 13 处五连；
3. manifest/文件路由表驱动：`{'/wardb_unique': (body, bz2_body), ...}` 一张表 + 循环，覆盖 5 组清单路由；
4. `_handle_register_api` / `_handle_import_api` 公共段抽 `_parse_api_body(handler, max_len)` + `_validate_new_account(handler, data, action_label)`（含蜜罐/令牌/限速/用户名/密码/DB ready）；`do_POST` 的 `X-Requested-With` 检查统一在 `/api/` 前缀拦一次；
5. 零碎：删 L989 局部 `import time`；`_handle_online_api` 复用 `_client_ip`；`/register` `/import` 共用 SPA html helper；
- 验收：快照测试一致；对照阶段 0 响应头存档逐路由核对（200/404/Content-Type/Content-Length 不变，`//` 前缀路径的接受性只增不减）；全量离线测试跑一遍。

### 阶段 3：行为修正与结构化（3 个独立 commit）

- **3a** 修 `_localization_body` NameError：except 分支不再引用未绑定的 `body`，失败时缓存空 bytes 并记日志（后续请求短路，不再打穿线程）；
- **3b** 补发 `Content-Length` ×4（/settings.js、/undefined、/WarDatabase/<tbl>、/status 的 404 分支——第 4 处为阶段 0 实测新增），经 `send_bytes` 统一获得；
- **3c** `LOCALIZATION_EXTRA` 结构化：124 行自解析常量改为 `dict[name] -> value`，`_localization_body` 直接查 dict 注入缺失 key，删除正则/rfind 自解析段（等价重构，diff 大故独立提交）；
- 验收：客户端登录全流程 + 语言切换（12 locale）+ hngsync 同步 + 注册页/验证码//import 冒烟。

## 四、每阶段验收清单

- [ ] `tests/test_file_server_snapshot.py` 全部断言通过（byte 级一致）；
- [ ] `.venv\Scripts\python.exe -c "import realtest_server"` 无警告无循环 import；
- [ ] 全量离线测试与基线逐项一致（对照 `docs/baseline-2026-09.md`）；
- [ ] 服务器重启（`.un_realtest.ps1`）后：hngsync 同步、登录、注册页、/import、/online.json 实测正常；
- [ ] 独立 commit；阶段 1 注明"纯移动无行为变更"，3a/3b 注明行为修正内容。

## 五、风险与对策

| 风险 | 对策 |
| --- | --- |
| import 即执行的生成逻辑搬移后顺序/字节变化 | 原序搬运 + 阶段 0 快照测试 byte 级兜底 |
| `HttpHandler(directory=GAME_DIR)` 静态回退语义被误改 | 回退链留 web_server 不动；`try_serve` 只接管显式下载路由 |
| pfx 密钥加载失败路径（日志/None 语义） | 搬运时保持原样（print 前缀 `[web_server]` 改 `[file_server]` 属日志文案，随 3b 之外的等价变化接受） |
| admin 后台跨进程拉 /online.json | 路由不动，HTTP 接口签名不变 |

## 六、明确不做的事

- 不把 HttpHandler 迁到任何 Web 框架（保持 http.server，对齐原计划"不引入过度设计"）；
- 不动 register_captcha.py / foreign_import.py / admin 后台 web/server；
- 不动 `warserver-refactor-plan.md` 阶段 4 的其余两项（wardb / player_service）；
- 不做路由正则化/中间件化等超出本清单的重构；
- 阶段 2 之外不主动改任何日志文案与响应字节。
