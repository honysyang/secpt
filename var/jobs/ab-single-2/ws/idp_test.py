import urllib.request, urllib.parse, http.cookiejar, sys

IDP="http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP="http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPRedirectHandler())

def get(u):
    try:
        r=op.open(u, timeout=20)
        return r.status, r.read().decode('utf-8','replace'), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), u

def post(u, data):
    d=urllib.parse.urlencode(data).encode()
    req=urllib.request.Request(u, data=d, headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        r=op.open(req, timeout=20)
        return r.status, r.read().decode('utf-8','replace'), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), u

print("== login page ==")
s,b,u=get(IDP+"/login")
print(s, u, b[:300])

print("== try admin login ==")
s,b,u=post(IDP+"/login", {"username":"admin","password":"RCTF2025"})
print(s, u)
print(b[:800])
