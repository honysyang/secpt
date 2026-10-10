import urllib.request, urllib.parse, http.cookiejar, sys

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                 urllib.request.HTTPRedirectHandler())

def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type":"application/x-www-form-urlencoded",
                 "User-Agent":"curl/8"})
    try:
        r = op.open(req, timeout=25)
        return r.status, r.geturl(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.geturl(), e.read()

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent":"curl/8"})
    try:
        r = op.open(req, timeout=25)
        return r.status, r.geturl(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.geturl(), e.read()

# 1) login as admin
st, url, body = post(IDP+"/login", {"username":"admin","password":"RCTF2025","samlRequest":"","relayState":""})
print("LOGIN", st, url)
print(body[:600])
print("cookies:", [c.name+"="+c.value for c in cj])
print("="*60)

# 2) check /portal and /me
st, url, body = get(IDP+"/portal")
print("PORTAL", st, url)
print(body[:400])
