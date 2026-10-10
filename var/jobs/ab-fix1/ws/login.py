import urllib.request, urllib.parse, http.cookiejar, re, sys

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPRedirectHandler())

def req(url, data=None, method=None):
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header('Content-Type','application/x-www-form-urlencoded')
    try:
        resp = op.open(r, timeout=20)
        return resp.geturl(), resp.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return url, e.read().decode('utf-8','replace')

# login admin
u, body = req(IDP+"/login", {"username":"admin","password":"RCTF2025"})
print("LOGIN ->", u)
print(body[:500])
