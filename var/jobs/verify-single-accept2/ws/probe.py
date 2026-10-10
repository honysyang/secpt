import urllib.request, urllib.parse, http.cookiejar

A = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
B = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

def get(base, path, data=None):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    d = urllib.parse.urlencode(data).encode() if data else None
    r = urllib.request.Request(base+path, data=d, headers={"User-Agent":"Mozilla/5.0"})
    try:
        resp = op.open(r, timeout=20)
        return resp.getcode(), dict(resp.headers), resp.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8','ignore')
    except Exception as e:
        return "ERR", {}, str(e)

for name,base in [("58595",A),("49263",B)]:
    for p in ["/", "/login", "/saml/metadata", "/health", "/flag", "/admin"]:
        c,h,b = get(base,p)
        print(f"--- {name} {p} -> {c} | {h.get('Content-Type','')} | {b[:200].strip()}")
