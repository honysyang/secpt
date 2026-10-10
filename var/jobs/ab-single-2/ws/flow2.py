import urllib.request, urllib.parse, http.cookiejar

IDP="http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP="http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj=http.cookiejar.CookieJar()
class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedir())

def req(u, data=None):
    if data is not None:
        d=urllib.parse.urlencode(data).encode()
        r=urllib.request.Request(u, data=d, headers={'Content-Type':'application/x-www-form-urlencoded'})
    else:
        r=urllib.request.Request(u)
    try:
        resp=op.open(r, timeout=20)
        return resp.status, resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), dict(e.headers)

s,b,h=req(SP+"/saml/login")
print("SP /saml/login:", s)
print("Location:", h.get('Location'))
