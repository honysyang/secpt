import urllib.request, urllib.parse, http.cookiejar, sys

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

def get(url):
    r = op.open(url, timeout=30)
    return r.geturl(), r.read().decode('utf-8','replace')

def post(url, data):
    d = urllib.parse.urlencode(data).encode()
    r = op.open(url, data=d, timeout=30)
    return r.geturl(), r.read().decode('utf-8','replace')

try:
    u, body = post(IDP+"/login", {"username":"admin","password":"RCTF2025"})
    print("LOGIN URL:", u)
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, e.read().decode('utf-8','replace'))
    sys.exit()
print("=== after login body (first 1500) ===")
print(body[:1500])
print("=== cookies ===", [c.name for c in cj])

for path in ["/portal", "/dashboard", "/"]:
    try:
        u, b = get(IDP+path)
        print("\n#### GET", path, "->", u)
        print(b[:2500])
    except Exception as e:
        print("err", path, e)
