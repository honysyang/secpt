【SAML 联邦 SSO 专项先验 · 白盒优先】

这类题通常给你 IdP（身份认证中心）+ SP（业务服务）的源码。先读源码，别黑盒瞎试。

1. 找 SP 的授权判断依据：管理后台/flag 页通常按 session 里的 email / role / userType 判断
   （如 session['email'] == 'admin@xx.team'）。判断依据来自 SAML 断言，而断言字段来自
   IdP 的注册/用户数据——所以**注册时能控制的字段**就是攻击面。
2. 找 IdP 的注册/邀请码逻辑，警惕 JS 类型强转坑：
   - parseInt("true") = NaN、parseInt("abc") = NaN，NaN === 0 恒为 false；
   - 如果校验是 `if (parseInt(type) === 0) { 需要邀请码 }`，那么 type=true / type=abc
     会绕过邀请码分支；
   - 再看数据库列类型：MySQL TINYINT 在非严格模式会把 "true" 强制转成 0（严格模式报错），
     于是"没走邀请码校验"却"存成了 0 权限账号"——两者不一致就是漏洞。
3. 配合数据库强转：注册时把目标邮箱/角色字段直接设成 SP 需要的值
   （如 email=admin@xx.team），验证校验器只查格式不查白名单。
4. 认证流程走一遍：login → /saml/sso?SAMLRequest=... → 页面自提交 SAMLResponse → SP 的
   /saml/acs 解析断言写 session → 访问受保护页。
5. 若校验器对 type 做了 `.equals('0')` 之类的条件必填（invitationCode），
   用 type=true 同样让条件为假、绕过必填。
6. 备用面：SAML 元数据泄露、Signature 解析只查第一段 Assertion（XSW/签名包装）、
   RelayState 注入、Replay 防护缺失（断言 ID 去重）。
7. 拿到管理员会话后，访问 SP 管理后台拿 FLAG。
