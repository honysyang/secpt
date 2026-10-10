import urllib.request, urllib.parse, http.cookiejar, re, sys, http.client

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(url, data=None):
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data)
    if data: r.add_header('Content-Type','application/x-www-form-urlencoded')
    try:
        resp = op.open(r, timeout=25)
        return resp.geturl(), resp.read().decode('utf-8','replace'), resp
    except urllib.error.HTTPError as e:
        return url, e.read().decode('utf-8','replace'), e

# 1. login admin on IdP
u,b,_ = req(IDP+"/login", {"username":"admin","password":"RCTF2025"})
print("[1] login ->", u)

# 2. start SP login to get IdP redirect (SAMLRequest)
u,b,_ = req(SP+"/saml/login")
print("[2] SP /saml/login ->", u)
m = re.search(r'Location:\s*(.+)', b)
print("   body head:", b[:300])
