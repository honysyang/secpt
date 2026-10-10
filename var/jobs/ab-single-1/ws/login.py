import urllib.request, urllib.parse, http.cookiejar, re, sys
IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]
def get(u): return op.open(u, timeout=20).read().decode('utf8','replace')
def post(u, data):
    d = urllib.parse.urlencode(data).encode()
    return op.open(urllib.request.Request(u, data=d), timeout=20).read().decode('utf8','replace')
print("[*] GET /login"); print(get(IDP+"/login")[:200])
print("[*] POST /login admin/RCTF2025")
r = post(IDP+"/login", {"username":"admin","password":"RCTF2025"})
print("len", len(r), "invalid:", "Invalid username or password" in r)
print(r[:300])
for c in cj: print("cookie", c.name, c.value[:40])
