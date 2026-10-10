import urllib.request, urllib.parse, http.cookiejar, re, html

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
    u=re.sub(r'http://127\.0\.0\.1(:\d+)?', IDP, u)
    u=re.sub(r'http://idp(:80)?', IDP, u)
    u=re.sub(r'http://localhost(:\d+)?', IDP, u)
    return u

# 1. start fresh login on SP
s,b,h=req(SP+"/saml/login")
sso=fix(h.get('Location'))
print("[1] SSO:", sso[:80])

# 2. GET SSO -> 302 to /login?...  (not logged)
s,b,h=req(sso)
loc=h.get('Location')
login_url=IDP+loc if loc.startswith('/') else fix(loc)
print("[2] got login url")

# 3. GET login page to capture hidden fields
s,b,h=req(login_url)
samlRequest=re.search(r'name="samlRequest"[^>]*value="([^"]*)"', b)
relayState=re.search(r'name="relayState"[^>]*value="([^"]*)"', b)
SR=html.unescape(samlRequest.group(1)) if samlRequest else ''
RS=html.unescape(relayState.group(1)) if relayState else ''
print("[3] samlRequest len:", len(SR), "relayState:", RS)

# 4. POST login with samlRequest
s,b,h=req(IDP+"/login", data={"username":"admin","password":"RCTF2025","samlRequest":SR,"relayState":RS})
print("[4] login status:", s, "loc:", (h.get('Location') or '')[:150])
loc=h.get('Location')
flow_url = (IDP+loc) if loc and loc.startswith('/') else (fix(loc) if loc else None)
print("    flow_url:", flow_url[:120] if flow_url else None)
