import urllib.request, urllib.parse, http.cookiejar, sys

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None):
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
    r = op.open(url, data=data, timeout=30)
    return r.geturl(), r.status, r.read().decode('utf-8','replace'), r.headers

u,b = (lambda r:(r.geturl(), r.read().decode('utf-8','replace')))(op.open(IDP+"/login", data=urllib.parse.urlencode({"username":"admin","password":"RCTF2025"}).encode(), timeout=30))
print("login ->", u)

# IdP initiated SSO
url = IDP+"/saml/idp/Flag"
try:
    u, st, body, hdr = req(url)
    print("GET", url, "->", u, st)
    print("CONTENT-TYPE:", hdr.get('Content-Type'))
    print(body[:3000])
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, "url=", e.geturl())
    print(e.read().decode('utf-8','replace')[:3000])
