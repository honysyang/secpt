import urllib.request, urllib.parse, http.cookiejar, re, sys

IDP = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPRedirectHandler())

def req(url, data=None, headers=None):
    h = {"User-Agent":"Mozilla/5.0"}
    if headers: h.update(headers)
    d = urllib.parse.urlencode(data).encode() if data else None
    r = urllib.request.Request(url, data=d, headers=h)
    try:
        resp = op.open(r, timeout=25)
        return resp.getcode(), resp.geturl(), resp.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

print("== GET / ==")
c,u,b = req(IDP+"/"); print(c,u,b[:400])

print("== GET /login ==")
c,u,b = req(IDP+"/login"); print(c,u,b[:600])

print("== POST /login ==")
c,u,b = req(IDP+"/login", {"username":"admin","password":"RCTF2025"}); print(c,u,b[:600])

print("== GET /api/me ==")
c,u,b = req(IDP+"/api/me"); print(c,u,b[:400])
